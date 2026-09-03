"""Reading ingestion routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain import compliance as comp_mod
from coldchain import excursions as exc_mod
from coldchain import ingest as ingest_mod
from coldchain.config import settings
from coldchain.deps import (
    compute_request_hash,
    get_db,
    idem_lookup,
    idem_replay_check,
    idem_store,
    require_org,
    validate_idempotency_key,
)
from coldchain.models import Organization, Reading
from coldchain.schemas import BatchCreate, ReadingCreate, SupersedeCreate
from coldchain.temp import TempError, parse_temp_to_mc

router = APIRouter(tags=["readings"])


def _humidity_to_bp(v: float | None) -> int:
    if v is None:
        return 0
    return int(round(float(v) * 100))


def _parse_item(item: ReadingCreate) -> tuple[int, int, int]:
    try:
        temp_mc = parse_temp_to_mc(item.temperature, item.unit)
    except TempError as e:
        raise ingest_mod.InvalidReading(str(e)) from e
    humidity_bp = _humidity_to_bp(item.humidity_pct)
    battery_mv = int(item.battery_mv) if item.battery_mv is not None else 0
    return (temp_mc, humidity_bp, battery_mv)


def _reading_to_dict(r: Reading) -> dict[str, Any]:
    return {
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


def _ingest_one(db: Session, org_id: str, item: ReadingCreate) -> tuple[dict[str, Any], int]:
    temp_mc, humidity_bp, battery_mv = _parse_item(item)
    reading, outcome, reason = ingest_mod.ingest_reading(
        db,
        org_id,
        device_id=item.device_id,
        seq=item.seq,
        ts_ms=item.ts_ms,
        temp_mc=temp_mc,
        humidity_bp=humidity_bp,
        battery_mv=battery_mv,
    )
    if outcome in ("accepted", "quarantined"):
        try:
            exc_mod.evaluate_reading(db, org_id, reading)
        except Exception:  # noqa: S110 -- best-effort side effect
            pass
        try:
            comp_mod.append_audit(db, org_id, "api", "reading.ingest", reading.id, outcome)
        except Exception:  # noqa: S110 -- best-effort audit
            pass
    body = _reading_to_dict(reading)
    if outcome == "duplicate":
        body["duplicate"] = True
        return (body, 200)
    if outcome == "quarantined":
        body["quarantined"] = True
        body["reason"] = reason
        return (body, 201)
    return (body, 201)


@router.post("/v1/readings", response_model=None)
def create_reading(
    payload: ReadingCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> Any:
    body, status = _ingest_one(db, org.id, payload)
    return JSONResponse(status_code=status, content=body)


@router.post("/v1/readings/batch", response_model=None)
def create_batch(  # noqa: C901
    payload: BatchCreate,
    request: Request,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    if len(payload.items) > settings.batch_max_items:
        raise HTTPException(status_code=422, detail="batch too large")
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
    results: list[dict[str, Any]] = []
    for idx, item in enumerate(payload.items):
        try:
            body, _ = _ingest_one(db, org.id, item)
            entry: dict[str, Any] = {"index": idx, "ok": True, "id": body["id"]}
            if body.get("duplicate"):
                entry["duplicate"] = True
            if body.get("quarantined"):
                entry["quarantined"] = True
                entry["reason"] = body.get("reason", "")
            results.append(entry)
        except (ingest_mod.IngestError, TempError, ValueError) as e:
            # Map to per-item error; HTTP status stays 200.
            if isinstance(e, ingest_mod.UnknownDevice):
                msg = f"unknown device: {e}"
            else:
                msg = str(e) or e.__class__.__name__
            results.append({"index": idx, "ok": False, "error": msg})
        except HTTPException as e:
            results.append({"index": idx, "ok": False, "error": str(e.detail)})
    out = {"items": results}
    if key is not None:
        idem_store(db, org.id, key, "POST", str(request.url.path), req_hash, 200, json.dumps(out))
    return JSONResponse(status_code=200, content=out)


@router.post("/v1/readings/{reading_id}/supersede")
def supersede_route(
    reading_id: str,
    payload: SupersedeCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    try:
        temp_mc = parse_temp_to_mc(payload.temperature, payload.unit)
    except TempError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    new_row = ingest_mod.supersede_reading(
        db, org.id, reading_id, temp_mc=temp_mc, reason=payload.reason
    )
    try:
        exc_mod.evaluate_reading(db, org.id, new_row)
    except Exception:  # noqa: S110 -- best-effort side effect
        pass
    try:
        comp_mod.append_audit(db, org.id, "api", "reading.supersede", new_row.id, payload.reason)
    except Exception:  # noqa: S110 -- best-effort audit
        pass
    # Fetch fresh to include any excursion side effects? Return row dict.
    row = db.execute(select(Reading).where(Reading.id == new_row.id)).scalar_one()
    return _reading_to_dict(row)
