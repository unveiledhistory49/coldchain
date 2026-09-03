"""Excursion listing routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain.deps import get_db, require_org
from coldchain.models import Excursion, Organization

router = APIRouter(tags=["excursions"])


@router.get("/v1/excursions")
def list_excursions(
    shipment_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    lim = max(1, min(limit, 200))
    stmt = select(Excursion).where(Excursion.org_id == org.id)
    if shipment_id:
        stmt = stmt.where(Excursion.shipment_id == shipment_id)
    if status:
        stmt = stmt.where(Excursion.status == status)
    stmt = stmt.order_by(Excursion.started_ts_ms.asc()).limit(lim)
    rows = db.execute(stmt).scalars().all()
    items = [
        {
            "id": e.id,
            "shipment_id": e.shipment_id,
            "device_id": e.device_id,
            "profile_id": e.profile_id,
            "started_ts_ms": e.started_ts_ms,
            "ended_ts_ms": e.ended_ts_ms,
            "peak_mc": e.peak_mc,
            "trough_mc": e.trough_mc,
            "status": e.status,
        }
        for e in rows
    ]
    return {"items": items}
