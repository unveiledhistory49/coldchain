"""Shipment routes."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coldchain import excursions as exc_mod
from coldchain import ingest as ingest_mod
from coldchain.deps import (
    compute_request_hash,
    get_db,
    idem_lookup,
    idem_replay_check,
    idem_store,
    require_org,
    validate_idempotency_key,
)
from coldchain.models import Organization, Shipment, ShipmentDevice, TempProfile
from coldchain.schemas import CompleteCreate, ShipmentCreate

router = APIRouter(tags=["shipments"])


def _shipment_to_dict(s: Shipment, device_ids: list[str]) -> dict[str, Any]:
    return {
        "id": s.id,
        "reference": s.reference,
        "profile_id": s.profile_id,
        "status": s.status,
        "origin": s.origin,
        "destination": s.destination,
        "started_at_ms": s.started_at_ms,
        "ended_at_ms": s.ended_at_ms,
        "devices": device_ids,
    }


def _device_ids(db: Session, shipment_id: str) -> list[str]:
    rows = (
        db.execute(
            select(ShipmentDevice.device_id).where(ShipmentDevice.shipment_id == shipment_id)
        )
        .scalars()
        .all()
    )
    return [str(v) for v in rows]


@router.post("/v1/shipments", status_code=201, response_model=None)
def create_shipment(
    payload: ShipmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    key = validate_idempotency_key(idempotency_key)
    req_hash = ""
    if key is not None:
        raw = json.dumps(payload.model_dump(), sort_keys=True).encode("utf-8")
        req_hash = compute_request_hash("POST", str(request.url.path), raw)
        hit = idem_replay_check(idem_lookup(db, org.id, key), req_hash)
        if hit is not None:
            return JSONResponse(
                status_code=hit.response_status, content=json.loads(hit.response_body)
            )
    prof = db.execute(
        select(TempProfile).where(
            TempProfile.id == payload.profile_id, TempProfile.org_id == org.id
        )
    ).scalar_one_or_none()
    if prof is None:
        raise HTTPException(status_code=404, detail="profile not found")
    for did in payload.device_ids:
        try:
            dev = ingest_mod.get_device(db, org.id, did)
        except ingest_mod.UnknownDevice as e:
            raise HTTPException(status_code=404, detail="device not found") from e
        if dev.status == "retired":
            raise HTTPException(status_code=409, detail="device retired")
    ship = Shipment(
        org_id=org.id,
        reference=payload.reference,
        profile_id=payload.profile_id,
        status="in_transit",
        origin=payload.origin,
        destination=payload.destination,
        started_at_ms=payload.started_at_ms,
        ended_at_ms=0,
    )
    db.add(ship)
    try:
        db.flush()
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="shipment reference conflict") from e
    for did in payload.device_ids:
        db.add(ShipmentDevice(shipment_id=ship.id, device_id=did))
    db.flush()
    body = _shipment_to_dict(ship, list(payload.device_ids))
    if key is not None:
        idem_store(db, org.id, key, "POST", str(request.url.path), req_hash, 201, json.dumps(body))
    return JSONResponse(status_code=201, content=body)


@router.get("/v1/shipments")
def list_shipments(
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    rows = db.execute(select(Shipment).where(Shipment.org_id == org.id)).scalars().all()
    items = [_shipment_to_dict(s, _device_ids(db, s.id)) for s in rows]
    return {"items": items}


@router.get("/v1/shipments/{shipment_id}")
def get_shipment(
    shipment_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    ship = db.execute(
        select(Shipment).where(Shipment.id == shipment_id, Shipment.org_id == org.id)
    ).scalar_one_or_none()
    if ship is None:
        raise HTTPException(status_code=404, detail="shipment not found")
    now_ms = int(time.time() * 1000)
    try:
        stats = exc_mod.shipment_stats(db, org.id, shipment_id, now_ms)
    except exc_mod.ExcursionError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    body = _shipment_to_dict(ship, _device_ids(db, ship.id))
    body["stats"] = stats
    return body


@router.post("/v1/shipments/{shipment_id}/complete")
def complete_shipment(
    shipment_id: str,
    payload: CompleteCreate | None = None,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    ship = db.execute(
        select(Shipment).where(Shipment.id == shipment_id, Shipment.org_id == org.id)
    ).scalar_one_or_none()
    if ship is None:
        raise HTTPException(status_code=404, detail="shipment not found")
    if ship.status != "in_transit":
        raise HTTPException(status_code=409, detail="shipment not in_transit")
    ended = payload.ended_at_ms if payload is not None else 0
    if not ended:
        ended = int(time.time() * 1000)
    ship.ended_at_ms = ended
    ship.status = "complete"
    db.flush()
    return _shipment_to_dict(ship, _device_ids(db, ship.id))
