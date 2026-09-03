"""Period lock + webhook routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from coldchain import compliance as comp_mod
from coldchain import webhooks as wh_mod
from coldchain.deps import get_db, require_org
from coldchain.models import Organization, WebhookEndpoint
from coldchain.schemas import LockCreate, WebhookCreate

router = APIRouter(tags=["periods-webhooks"])


@router.post("/v1/periods/lock", status_code=201)
def lock_period_route(
    payload: LockCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    lock = comp_mod.lock_period(db, org.id, payload.period)
    return {
        "id": lock.id,
        "period": lock.period,
        "readings_hash": lock.readings_hash,
    }


def _url_allowed(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme == "https" and p.hostname:
        return True
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"):
        return True
    return False


@router.post("/v1/webhook-endpoints", status_code=201)
def create_endpoint(
    payload: WebhookCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    if not _url_allowed(payload.url):
        raise HTTPException(status_code=422, detail="url must be https or localhost")
    ep = WebhookEndpoint(org_id=org.id, url=payload.url, secret=payload.secret, active=1)
    db.add(ep)
    db.flush()
    return {"id": ep.id, "url": ep.url}


@router.post("/v1/webhooks/dispatch")
def dispatch_webhooks(
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    _ = org
    return wh_mod.dispatch_due(db, limit=25)
