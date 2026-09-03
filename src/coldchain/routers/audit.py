"""Audit routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain import compliance as comp_mod
from coldchain.deps import get_db, require_org
from coldchain.models import AuditLog, Organization

router = APIRouter(tags=["audit"])


@router.get("/v1/audit")
def list_audit(
    since_seq: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    lim = max(1, min(limit, 200))
    rows = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.org_id == org.id, AuditLog.seq > since_seq)
            .order_by(AuditLog.seq.asc())
            .limit(lim)
        )
        .scalars()
        .all()
    )
    items = [
        {
            "seq": r.seq,
            "actor": r.actor,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "prev_hash": r.prev_hash,
            "hash": r.hash,
        }
        for r in rows
    ]
    return {"items": items}


@router.get("/v1/audit/verify")
def verify_audit_route(
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    result = comp_mod.verify_audit(db, org.id)
    return {k: v for k, v in result.items()}
