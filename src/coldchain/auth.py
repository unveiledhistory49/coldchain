"""API-key issuance and authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain.config import settings
from coldchain.models import Organization


def hash_key(raw: str) -> str:
    material = f"{settings.api_key_pepper}::{raw}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    raw = "cc_live_" + secrets.token_hex(16)
    digest = hash_key(raw)
    prefix = raw[:12]
    return (raw, digest, prefix)


def authenticate(db: Session, raw: str) -> Organization | None:
    if not raw or not raw.startswith("cc_live_"):
        return None
    want = hash_key(raw)
    orgs = list(db.execute(select(Organization)).scalars().all())
    for org in orgs:
        if hmac.compare_digest(org.api_key_hash, want):
            return org
    return None
