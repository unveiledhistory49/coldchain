"""Webhook enqueue + dispatch with HMAC sha256 and backoff."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain.config import settings
from coldchain.models import WebhookDelivery, WebhookEndpoint


def enqueue(db: Session, org_id: str, event: str, payload: dict[str, Any]) -> int:
    endpoints = (
        db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.org_id == org_id, WebhookEndpoint.active == 1
            )
        )
        .scalars()
        .all()
    )
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    count = 0
    for ep in endpoints:
        row = WebhookDelivery(
            endpoint_id=ep.id,
            org_id=org_id,
            event=event,
            payload=body,
            status="pending",
            attempts=0,
        )
        db.add(row)
        count += 1
    if count:
        db.flush()
    return count


def _sign(secret: str, body: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def dispatch_due(db: Session, limit: int = 25) -> dict[str, int]:
    import httpx

    now = datetime.utcnow()
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.status == "pending", WebhookDelivery.next_retry_at <= now)
        .order_by(WebhookDelivery.created_at.asc())
        .limit(limit)
    )
    deliveries = list(db.execute(stmt).scalars().all())
    dispatched = 0
    failed = 0
    for d in deliveries:
        ep = db.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.id == d.endpoint_id)
        ).scalar_one_or_none()
        if ep is None or not ep.active:
            d.status = "failed"
            db.flush()
            failed += 1
            continue
        body = d.payload.encode("utf-8")
        sig = _sign(ep.secret, body)
        d.attempts += 1
        try:
            with httpx.Client(timeout=settings.webhook_timeout_seconds) as client:
                resp = client.post(
                    ep.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-ColdChain-Event": d.event,
                        "X-ColdChain-Signature": sig,
                    },
                )
            if 200 <= resp.status_code < 300:
                d.status = "delivered"
                dispatched += 1
            else:
                raise RuntimeError(f"status {resp.status_code}")
        except Exception:
            if d.attempts >= settings.webhook_max_attempts:
                d.status = "failed"
                failed += 1
            else:
                d.status = "pending"
                backoff_min = min(2 ** max(d.attempts - 1, 0), 60)
                d.next_retry_at = datetime.utcnow() + timedelta(minutes=backoff_min)
        db.flush()
    pending = list(
        db.execute(select(WebhookDelivery).where(WebhookDelivery.status == "pending"))
        .scalars()
        .all()
    )
    return {"dispatched": dispatched, "failed": failed, "pending": len(pending)}
