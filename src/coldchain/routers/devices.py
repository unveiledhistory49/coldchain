"""Device routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from coldchain.models import Device, Organization
from coldchain.schemas import CalibrationCreate, DeviceCreate

router = APIRouter(tags=["devices"])


def _device_to_dict(d: Device) -> dict[str, Any]:
    return {
        "id": d.id,
        "org_id": d.org_id,
        "serial": d.serial,
        "name": d.name,
        "model": d.model,
        "status": d.status,
    }


@router.post("/v1/devices", status_code=201, response_model=None)
def create_device(
    payload: DeviceCreate,
    request: Request,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    key = validate_idempotency_key(idempotency_key)
    raw_body: bytes = b""
    req_hash = ""
    existing = None
    if key is not None:
        # Use canonical payload for hash (body may not be re-readable reliably here).
        raw_body = json.dumps(payload.model_dump(), sort_keys=True).encode("utf-8")
        req_hash = compute_request_hash("POST", str(request.url.path), raw_body)
        existing = idem_lookup(db, org.id, key)
        hit = idem_replay_check(existing, req_hash)
        if hit is not None:
            return JSONResponse(
                status_code=hit.response_status, content=json.loads(hit.response_body)
            )
    dev = ingest_mod.create_device(db, org.id, payload.serial, payload.name, payload.model)
    body = _device_to_dict(dev)
    if key is not None:
        idem_store(db, org.id, key, "POST", str(request.url.path), req_hash, 201, json.dumps(body))
    return JSONResponse(status_code=201, content=body)


@router.get("/v1/devices")
def list_devices(
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    rows = db.execute(select(Device).where(Device.org_id == org.id)).scalars().all()
    return {"items": [_device_to_dict(d) for d in rows]}


@router.get("/v1/devices/{device_id}")
def get_device_route(
    device_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    dev = ingest_mod.get_device(db, org.id, device_id)
    return _device_to_dict(dev)


@router.post("/v1/devices/{device_id}/retire")
def retire_device_route(
    device_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    dev = ingest_mod.retire_device(db, org.id, device_id)
    return _device_to_dict(dev)


@router.post("/v1/devices/{device_id}/calibrations", status_code=201)
def add_calibration_route(
    device_id: str,
    payload: CalibrationCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    cal = ingest_mod.add_calibration(
        db,
        org.id,
        device_id,
        payload.valid_from_ms,
        payload.valid_until_ms,
        payload.certificate_ref,
    )
    return {
        "id": cal.id,
        "device_id": cal.device_id,
        "valid_from_ms": cal.valid_from_ms,
        "valid_until_ms": cal.valid_until_ms,
        "certificate_ref": cal.certificate_ref,
    }


@router.get("/v1/devices/{device_id}/readings")
def list_readings(
    device_id: str,
    since_ms: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    ingest_mod.get_device(db, org.id, device_id)
    rows = ingest_mod.device_readings(db, org.id, device_id, since_ms=since_ms, limit=limit)
    items = [
        {
            "id": r.id,
            "device_id": r.device_id,
            "seq": r.seq,
            "ts_ms": r.ts_ms,
            "temp_mc": r.temp_mc,
            "humidity_bp": r.humidity_bp,
            "battery_mv": r.battery_mv,
            "flags": r.flags,
            "status": r.status,
        }
        for r in rows
    ]
    return {"items": items}
