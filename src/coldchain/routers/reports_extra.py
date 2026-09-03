"""Report certificate + approval routes."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from coldchain import compliance as comp_mod
from coldchain import reports as reports_mod
from coldchain.deps import get_db, require_org
from coldchain.models import Organization
from coldchain.schemas import ApproveCreate

router = APIRouter(tags=["reports"])


def _hash_current(cert: dict[str, Any]) -> str:
    return str(cert["statement_hash"])


@router.get("/v1/reports/shipments/{shipment_id}/certificate")
def get_certificate(
    shipment_id: str,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    try:
        return reports_mod.shipment_certificate(db, org.id, shipment_id, now_ms)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/v1/reports/shipments/{shipment_id}/approve", status_code=201)
def approve_certificate(
    shipment_id: str,
    payload: ApproveCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    try:
        cert = reports_mod.shipment_certificate(db, org.id, shipment_id, now_ms)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    # Only final records can be signed. An in-transit certificate is evidence
    # in progress, not a statement of compliance.
    if cert.get("verdict") == "incomplete":
        raise HTTPException(status_code=422, detail="cannot approve an incomplete shipment")
    current = _hash_current(cert)
    provided = payload.statement_hash
    # Exact match only: the statement hash binds the signature to the precise
    # content approved. Anything else (new reading, new excursion) is stale.
    if provided is not None and provided != current:
        raise HTTPException(status_code=409, detail={"message": "stale", "stale": True})
    effective = current
    ap = comp_mod.approve_report(
        db,
        org.id,
        reports_mod.REPORT_TYPE_SHIPMENT_CERTIFICATE,
        shipment_id,
        effective,
        payload.signer_name,
        payload.signer_email,
        payload.meaning,
    )
    return {
        "id": ap.id,
        "report_type": ap.report_type,
        "report_ref": ap.report_ref,
        "statement_hash": ap.statement_hash,
        "signer_name": ap.signer_name,
        "signer_email": ap.signer_email,
        "meaning": ap.meaning,
    }
