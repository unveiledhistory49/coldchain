"""Part-11-style compliance primitives: hash-chained audit, approvals, locks.

The audit log is a per-org hash chain: each row stores ``seq`` (1-based,
contiguous within the org), ``prev_hash`` (``"genesis"`` for the first row,
else the previous row's hash) and ``hash`` = sha256 over
``"\\n".join([prev, org, actor, action, target, detail, str(seq)])``.

``seq`` uniqueness relies on the surrounding transaction (read-max-then-insert
under serialized writes); all mutating helpers stage rows with ``flush()``
and never commit.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coldchain.models import Approval, AuditLog, PeriodLock, Reading

_MEANINGS = {"reviewed", "approved", "verified"}
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_STATEMENT_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ComplianceError(Exception):
    """Raised when a compliance operation violates its invariants."""


def _audit_hash(
    prev: str, org_id: str, actor: str, action: str, target: str, detail: str, seq: int
) -> str:
    """Compute the chained hash for one audit row."""
    body = "\n".join([prev, org_id, actor, action, target, detail, str(seq)])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def append_audit(
    db: Session,
    org_id: str,
    actor: str,
    action: str,
    target: str = "",
    detail: str = "",
) -> AuditLog:
    """Append one row to the org's audit chain and return it (flushed)."""
    max_seq = db.scalar(select(func.max(AuditLog.seq)).where(AuditLog.org_id == org_id))
    seq = int(max_seq or 0) + 1
    last = db.scalars(
        select(AuditLog).where(AuditLog.org_id == org_id).order_by(AuditLog.seq.desc()).limit(1)
    ).first()
    prev = last.hash if last is not None else "genesis"
    row = AuditLog(
        org_id=org_id,
        seq=seq,
        actor=actor,
        action=action,
        target=target,
        detail=detail,
        prev_hash=prev,
        hash=_audit_hash(prev, org_id, actor, action, target, detail, seq),
    )
    db.add(row)
    db.flush()
    return row


def verify_audit(db: Session, org_id: str) -> dict[str, Any]:
    """Verify the org's audit chain.

    Returns ``{"ok": bool, "checked": int, "error": str | None}`` where
    ``checked`` is the number of rows that verified cleanly before any
    failure. Checks: rows ordered by ``seq``; first ``prev_hash`` is
    ``"genesis"``; ``seq`` strictly +1 from the first row; each ``prev_hash``
    links to the previous row's hash; each hash recomputes. An empty chain
    verifies cleanly with ``checked == 0``.
    """
    rows = list(
        db.scalars(select(AuditLog).where(AuditLog.org_id == org_id).order_by(AuditLog.seq)).all()
    )
    if not rows:
        return {"ok": True, "checked": 0, "error": None}
    checked = 0
    for index, row in enumerate(rows):
        want_seq = rows[0].seq + index
        if row.seq != want_seq:
            return {
                "ok": False,
                "checked": checked,
                "error": f"seq break at position {index}: got {row.seq}, want {want_seq}",
            }
        if index == 0:
            if row.prev_hash != "genesis":
                return {
                    "ok": False,
                    "checked": checked,
                    "error": f"first entry seq={row.seq} has prev_hash != 'genesis'",
                }
        elif row.prev_hash != rows[index - 1].hash:
            return {
                "ok": False,
                "checked": checked,
                "error": f"chain break at seq={row.seq}: prev_hash mismatch",
            }
        recomputed = _audit_hash(
            row.prev_hash,
            row.org_id,
            row.actor,
            row.action,
            row.target,
            row.detail,
            row.seq,
        )
        if recomputed != row.hash:
            return {
                "ok": False,
                "checked": checked,
                "error": f"hash mismatch at seq={row.seq}",
            }
        checked += 1
    return {"ok": True, "checked": checked, "error": None}


def approve_report(
    db: Session,
    org_id: str,
    report_type: str,
    report_ref: str,
    statement_hash: str,
    signer_name: str,
    signer_email: str,
    meaning: str,
) -> Approval:
    """Record an electronic signature over a report statement hash."""
    fields = {
        "report_type": report_type,
        "report_ref": report_ref,
        "statement_hash": statement_hash,
        "signer_name": signer_name,
        "signer_email": signer_email,
        "meaning": meaning,
    }
    for name, value in fields.items():
        if not value or not value.strip():
            raise ComplianceError(f"{name} must be non-empty")
    if "@" not in signer_email or "." not in signer_email:
        raise ComplianceError("signer_email must contain '@' and '.'")
    if _STATEMENT_HASH_RE.match(statement_hash) is None:
        raise ComplianceError("statement_hash must be 64 hex characters")
    if meaning not in _MEANINGS:
        raise ComplianceError(f"meaning must be one of {sorted(_MEANINGS)}")
    row = Approval(
        org_id=org_id,
        report_type=report_type,
        report_ref=report_ref,
        statement_hash=statement_hash,
        signer_name=signer_name,
        signer_email=signer_email,
        meaning=meaning,
    )
    db.add(row)
    db.flush()
    return row


def _month_bounds_ms(period: str) -> tuple[int, int]:
    """Return ``(start_ms, end_ms)`` for a ``YYYY-MM`` period (UTC, end open)."""
    year = int(period[:4])
    month = int(period[5:7])
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def lock_period(db: Session, org_id: str, period: str) -> PeriodLock:
    """Freeze a calendar month: snapshot its readings hash into a lock row.

    ``readings_hash`` is the sha256 of ``"\\n".join(f"{id}:{ts}:{temp}")``
    over the org's readings with ``ts`` in the month, ordered by
    ``(id, ts, temp)``. Re-locking an existing period raises.
    """
    if _PERIOD_RE.match(period) is None:
        raise ComplianceError(f"invalid period {period!r}: want YYYY-MM")
    existing = db.scalars(
        select(PeriodLock).where(PeriodLock.org_id == org_id, PeriodLock.period == period)
    ).first()
    if existing is not None:
        raise ComplianceError(f"period {period!r} is already locked")
    start_ms, end_ms = _month_bounds_ms(period)
    rows = db.scalars(
        select(Reading)
        .where(
            Reading.org_id == org_id,
            Reading.ts_ms >= start_ms,
            Reading.ts_ms < end_ms,
        )
        .order_by(Reading.id, Reading.ts_ms, Reading.temp_mc)
    ).all()
    payload = "\n".join(f"{row.id}:{row.ts_ms}:{row.temp_mc}" for row in rows)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    lock = PeriodLock(org_id=org_id, period=period, readings_hash=digest)
    db.add(lock)
    db.flush()
    return lock


def period_locked(db: Session, org_id: str, ts_ms: int) -> bool:
    """Return True when the UTC calendar month containing ``ts_ms`` is locked."""
    moment = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    period = f"{moment.year:04d}-{moment.month:02d}"
    row = db.scalars(
        select(PeriodLock).where(PeriodLock.org_id == org_id, PeriodLock.period == period)
    ).first()
    return row is not None
