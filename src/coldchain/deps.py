"""FastAPI dependencies: engine/session, auth, idempotency."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Generator

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from coldchain.auth import authenticate
from coldchain.config import settings
from coldchain.db import create_app_engine, create_session_factory, init_db
from coldchain.models import IdempotencyRecord, Organization

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_app_engine(settings.database_url)
        init_db(_engine)
    return _engine


def get_db() -> Generator[Session, None, None]:
    factory = create_session_factory(get_engine())
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def require_org(
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Organization:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")
    org = authenticate(db, x_api_key)
    if org is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    return org


def validate_idempotency_key(key: str | None) -> str | None:
    if key is None:
        return None
    k = key.strip()
    if len(k) < 8 or len(k) > 64:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be 8..64 chars")
    return k


def compute_request_hash(method: str, path: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.encode("utf-8"))
    h.update(b"|")
    h.update(path.encode("utf-8"))
    h.update(b"|")
    h.update(body or b"")
    return h.hexdigest()


def idem_lookup(db: Session, org_id: str, key: str) -> IdempotencyRecord | None:
    stmt = select(IdempotencyRecord).where(
        IdempotencyRecord.org_id == org_id, IdempotencyRecord.key == key
    )
    return db.execute(stmt).scalar_one_or_none()


def idem_replay_check(
    record: IdempotencyRecord | None, request_hash: str
) -> IdempotencyRecord | None:
    if record is None:
        return None
    if not hmac.compare_digest(record.request_hash, request_hash):
        raise HTTPException(status_code=409, detail="idempotency key already used")
    return record


def idem_store(
    db: Session,
    org_id: str,
    key: str,
    method: str,
    path: str,
    request_hash: str,
    response_status: int,
    response_body: str,
) -> IdempotencyRecord:
    rec = IdempotencyRecord(
        org_id=org_id,
        key=key,
        method=method,
        path=path,
        request_hash=request_hash,
        response_status=response_status,
        response_body=response_body,
    )
    # Upsert semantics: merge in case of race.
    db.merge(rec)
    db.flush()
    return rec
