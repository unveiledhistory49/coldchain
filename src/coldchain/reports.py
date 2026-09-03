"""Shipment compliance certificates (read-only compliance projection).

A certificate aggregates the shipment, its profile, attached devices,
in-window readings, excursion stats, calibration coverage, approvals, and a
pass/fail/incomplete verdict into one dict. ``statement_hash`` is the sha256
of the canonical JSON (``sort_keys=True``, compact separators) of the
certificate without ``statement_hash`` itself, so approvers sign a stable
digest via :func:`coldchain.compliance.approve_report`.

Calibration coverage is resolved through
``coldchain.ingest.calibration_covering(db, device_id, ts_ms)`` (built in a
parallel workstream); a falsy return marks the reading as a gap. This module
issues no writes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain.excursions import shipment_stats
from coldchain.models import Approval, Reading, Shipment, ShipmentDevice, TempProfile

REPORT_TYPE_SHIPMENT_CERTIFICATE = "shipment_certificate"


def _verdict(
    status: str, stats: dict[str, Any], quarantined: int, gaps: int
) -> tuple[str, list[str]]:
    """Decide the certificate verdict and its human-readable reasons."""
    if status != "complete":
        return "incomplete", [f"shipment status is {status!r}; certificate requires 'complete'"]
    reasons: list[str] = []
    if bool(stats["breached"]):
        reasons.append(
            "excursion budget breached: "
            f"{stats['excursion_minutes']} min used of {stats['budget_minutes']} min"
        )
    mkt = stats["mkt_mc"]
    limit = stats["mkt_limit_mc"]
    if mkt is not None and limit is not None and int(mkt) > int(limit):
        reasons.append(f"MKT {mkt} mc exceeds limit {limit} mc")
    if quarantined > 0:
        reasons.append(f"{quarantined} quarantined reading(s) in window")
    if gaps > 0:
        reasons.append(f"{gaps} accepted reading(s) without calibration coverage")
    if reasons:
        return "fail", reasons
    return "pass", []


def shipment_certificate(db: Session, org_id: str, shipment_id: str, now_ms: int) -> dict[str, Any]:
    """Build the compliance certificate dict for a shipment."""
    from coldchain.ingest import calibration_covering

    shipment = db.get(Shipment, shipment_id)
    if shipment is None or shipment.org_id != org_id:
        raise ValueError(f"unknown shipment {shipment_id!r} in org {org_id!r}")
    profile = db.get(TempProfile, shipment.profile_id)
    if profile is None:
        raise ValueError(
            f"shipment {shipment_id!r} references missing profile {shipment.profile_id!r}"
        )
    device_ids = list(
        db.scalars(
            select(ShipmentDevice.device_id).where(ShipmentDevice.shipment_id == shipment_id)
        ).all()
    )
    end_ms = shipment.ended_at_ms if shipment.ended_at_ms else now_ms
    rows: list[Reading] = []
    if device_ids:
        rows = list(
            db.scalars(
                select(Reading).where(
                    Reading.org_id == org_id,
                    Reading.device_id.in_(device_ids),
                    Reading.ts_ms >= shipment.started_at_ms,
                    Reading.ts_ms <= end_ms,
                )
            ).all()
        )
    accepted = [row for row in rows if row.status == "accepted"]
    quarantined = sum(1 for row in rows if row.status == "quarantined")
    gaps = sum(1 for row in accepted if not calibration_covering(db, row.device_id, row.ts_ms))
    stats = shipment_stats(db, org_id, shipment_id, now_ms)
    verdict, reasons = _verdict(shipment.status, stats, quarantined, gaps)
    approvals = list(
        db.scalars(
            select(Approval)
            .where(
                Approval.org_id == org_id,
                Approval.report_type == REPORT_TYPE_SHIPMENT_CERTIFICATE,
                Approval.report_ref == shipment_id,
            )
            .order_by(Approval.created_at)
        ).all()
    )
    # The statement covers CONTENT only. generated_at_ms and the approvals
    # ledger are metadata: including them would make the hash unstable
    # (different on every read, different after each countersignature),
    # which would make exact-match approval impossible. Approvers sign the
    # content hash; their signatures are recorded alongside, not inside.
    statement = {
        "report_type": REPORT_TYPE_SHIPMENT_CERTIFICATE,
        "report_ref": shipment_id,
        "shipment_id": shipment_id,
        "reference": shipment.reference,
        "status": shipment.status,
        "profile": {
            "name": profile.name,
            "temp_min_mc": profile.temp_min_mc,
            "temp_max_mc": profile.temp_max_mc,
            "max_excursion_minutes": profile.max_excursion_minutes,
            "mkt_limit_mc": profile.mkt_limit_mc,
        },
        "devices": sorted(device_ids),
        "window_ms": {"from_ms": shipment.started_at_ms, "to_ms": end_ms},
        "readings_total": stats["readings_total"],
        "quarantined": quarantined,
        "calibration_gaps": gaps,
        "excursions_open": stats["excursions_open"],
        "excursions_closed": stats["excursions_closed"],
        "excursion_minutes": stats["excursion_minutes"],
        "budget_minutes": stats["budget_minutes"],
        "breached": stats["breached"],
        "mkt_mc": stats["mkt_mc"],
        "mkt_limit_mc": stats["mkt_limit_mc"],
        "temp_min_mc": stats["temp_min_mc"],
        "temp_max_mc": stats["temp_max_mc"],
        "verdict": verdict,
        "reasons": reasons,
    }
    canonical = json.dumps(statement, sort_keys=True, separators=(",", ":"), default=str)
    certificate: dict[str, Any] = dict(statement)
    certificate["statement_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    certificate["generated_at_ms"] = now_ms
    certificate["approvals"] = [
        {
            "signer_name": approval.signer_name,
            "signer_email": approval.signer_email,
            "meaning": approval.meaning,
            "statement_hash": approval.statement_hash,
            "created_at": approval.created_at.isoformat(),
        }
        for approval in approvals
    ]
    return certificate
