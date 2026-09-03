"""Tests for coldchain.compliance + coldchain.reports (sqlite :memory:)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from coldchain import ingest as ingest_module
from coldchain.compliance import (
    ComplianceError,
    append_audit,
    approve_report,
    lock_period,
    period_locked,
    verify_audit,
)
from coldchain.db import create_app_engine, create_session_factory, init_db
from coldchain.models import (
    AuditLog,
    Device,
    Excursion,
    Organization,
    Reading,
    Shipment,
    ShipmentDevice,
    TempProfile,
)
from coldchain.reports import shipment_certificate

MIN = 60_000
BASE = 1_712_000_000_000


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_app_engine("sqlite:///:memory:")
    init_db(engine)
    session = create_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _covered_calibration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ingest.calibration_covering: every reading is covered by default."""

    def _always_covered(db: Any, device_id: str, ts_ms: int) -> bool:
        return True

    monkeypatch.setattr(ingest_module, "calibration_covering", _always_covered, raising=False)


def make_org(db: Session, name: str = "Acme") -> Organization:
    org = Organization(name=name, api_key_hash=f"hash-{name}")
    db.add(org)
    db.flush()
    return org


def make_device(db: Session, org_id: str, serial: str = "DEV-1") -> Device:
    device = Device(org_id=org_id, serial=serial, name=serial)
    db.add(device)
    db.flush()
    return device


def make_profile(
    db: Session,
    org_id: str,
    name: str = "P2-8",
    tmin: int = 2000,
    tmax: int = 8000,
    budget: int = 30,
    mkt_limit: int | None = None,
) -> TempProfile:
    profile = TempProfile(
        org_id=org_id,
        name=name,
        temp_min_mc=tmin,
        temp_max_mc=tmax,
        max_excursion_minutes=budget,
        mkt_limit_mc=mkt_limit,
    )
    db.add(profile)
    db.flush()
    return profile


def make_shipment(
    db: Session,
    org_id: str,
    profile_id: str,
    ref: str = "S-1",
    status: str = "in_transit",
    started_ms: int = BASE,
    ended_ms: int = 0,
    device_ids: list[str] | None = None,
) -> Shipment:
    shipment = Shipment(
        org_id=org_id,
        reference=ref,
        profile_id=profile_id,
        status=status,
        started_at_ms=started_ms,
        ended_at_ms=ended_ms,
    )
    db.add(shipment)
    db.flush()
    for device_id in device_ids or []:
        db.add(ShipmentDevice(shipment_id=shipment.id, device_id=device_id))
    db.flush()
    return shipment


def add_reading(
    db: Session,
    org_id: str,
    device_id: str,
    seq: int,
    ts_ms: int,
    temp_mc: int,
    status: str = "accepted",
) -> Reading:
    reading = Reading(
        org_id=org_id,
        device_id=device_id,
        seq=seq,
        ts_ms=ts_ms,
        temp_mc=temp_mc,
        status=status,
    )
    db.add(reading)
    db.flush()
    return reading


def add_excursion(
    db: Session,
    org_id: str,
    shipment_id: str,
    device_id: str,
    profile_id: str,
    started_ms: int,
    ended_ms: int,
) -> Excursion:
    excursion = Excursion(
        org_id=org_id,
        shipment_id=shipment_id,
        device_id=device_id,
        profile_id=profile_id,
        started_ts_ms=started_ms,
        ended_ts_ms=ended_ms,
        peak_mc=9000,
        trough_mc=9000,
        status="closed",
    )
    db.add(excursion)
    db.flush()
    return excursion


# --- audit chain -----------------------------------------------------------


def test_audit_happy_path(db: Session) -> None:
    org = make_org(db)
    first = append_audit(db, org.id, "alice", "shipment.create", "S-1", "ok")
    second = append_audit(db, org.id, "alice", "reading.quarantine", "R-1")
    third = append_audit(db, org.id, "bob", "report.approve", "S-1", "signed")

    assert (first.seq, second.seq, third.seq) == (1, 2, 3)
    assert first.prev_hash == "genesis"
    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash

    result = verify_audit(db, org.id)
    assert result == {"ok": True, "checked": 3, "error": None}


def test_audit_empty_ok(db: Session) -> None:
    org = make_org(db)
    assert verify_audit(db, org.id) == {"ok": True, "checked": 0, "error": None}


def test_audit_tamper_fails(db: Session) -> None:
    org = make_org(db)
    append_audit(db, org.id, "alice", "a1")
    append_audit(db, org.id, "alice", "a2")
    append_audit(db, org.id, "alice", "a3")

    db.execute(
        update(AuditLog).where(AuditLog.org_id == org.id, AuditLog.seq == 2).values(hash="0" * 64)
    )
    db.flush()

    result = verify_audit(db, org.id)
    assert result["ok"] is False
    assert result["error"] is not None


def test_audit_gap_fails(db: Session) -> None:
    org = make_org(db)
    append_audit(db, org.id, "alice", "a1")
    append_audit(db, org.id, "alice", "a2")
    append_audit(db, org.id, "alice", "a3")

    db.execute(delete(AuditLog).where(AuditLog.org_id == org.id, AuditLog.seq == 2))
    db.flush()

    result = verify_audit(db, org.id)
    assert result["ok"] is False
    assert result["error"] is not None


# --- approvals -------------------------------------------------------------


def good_statement() -> dict[str, str]:
    return {
        "report_type": "shipment_certificate",
        "report_ref": "S-1",
        "statement_hash": "ab" * 32,
        "signer_name": "Ada",
        "signer_email": "ada@example.com",
        "meaning": "approved",
    }


def test_approve_report_ok(db: Session) -> None:
    org = make_org(db)
    approval = approve_report(db, org.id, **good_statement())
    assert approval.signer_email == "ada@example.com"
    assert approval.meaning == "approved"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_type", ""),
        ("report_ref", ""),
        ("statement_hash", ""),
        ("signer_name", ""),
        ("signer_email", ""),
        ("meaning", ""),
        ("signer_name", "   "),
        ("signer_email", "not-an-email"),
        ("signer_email", "a@b"),  # missing dot
        ("signer_email", "a.b"),  # missing at
        ("statement_hash", "xyz"),
        ("statement_hash", "ab" * 31),  # 62 chars
        ("statement_hash", "zz" * 32),  # non-hex
        ("meaning", "signed"),
        ("meaning", "APPROVED"),
    ],
)
def test_approve_report_validation(db: Session, field: str, value: str) -> None:
    org = make_org(db)
    kwargs = good_statement()
    kwargs[field] = value
    with pytest.raises(ComplianceError):
        approve_report(db, org.id, **kwargs)


# --- period locks ----------------------------------------------------------


def ms_of(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def test_lock_and_period_locked(db: Session) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    add_reading(db, org.id, dev.id, 1, ms_of(2026, 3, 10), 5000)
    add_reading(db, org.id, dev.id, 2, ms_of(2026, 3, 20), 6000)
    add_reading(db, org.id, dev.id, 3, ms_of(2026, 4, 2), 7000)

    lock = lock_period(db, org.id, "2026-03")
    assert lock.period == "2026-03"
    assert len(lock.readings_hash) == 64
    assert period_locked(db, org.id, ms_of(2026, 3, 1)) is True
    assert period_locked(db, org.id, ms_of(2026, 3, 31)) is True
    assert period_locked(db, org.id, ms_of(2026, 4, 1)) is False

    april = lock_period(db, org.id, "2026-04")
    assert april.readings_hash != lock.readings_hash  # month-scoped snapshot

    empty = lock_period(db, org.id, "2026-05")
    assert empty.readings_hash == hashlib.sha256(b"").hexdigest()

    with pytest.raises(ComplianceError):
        lock_period(db, org.id, "2026-03")  # already locked


@pytest.mark.parametrize("period", ["2026-13", "2026-00", "26-03", "2026-3", "2026/03", ""])
def test_lock_bad_period(db: Session, period: str) -> None:
    org = make_org(db)
    with pytest.raises(ComplianceError):
        lock_period(db, org.id, period)


# --- certificates ----------------------------------------------------------


def clean_complete_setup(
    db: Session, budget: int = 30, mkt_limit: int | None = None
) -> tuple[Organization, Shipment]:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id, budget=budget, mkt_limit=mkt_limit)
    ship = make_shipment(
        db,
        org.id,
        prof.id,
        status="complete",
        started_ms=BASE,
        ended_ms=BASE + 60 * MIN,
        device_ids=[dev.id],
    )
    add_reading(db, org.id, dev.id, 1, BASE + 10 * MIN, 4000)
    add_reading(db, org.id, dev.id, 2, BASE + 20 * MIN, 5000)
    add_reading(db, org.id, dev.id, 3, BASE + 30 * MIN, 6000)
    return org, ship


def test_certificate_pass(db: Session) -> None:
    org, ship = clean_complete_setup(db, mkt_limit=9000)
    cert = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)

    assert cert["verdict"] == "pass"
    assert cert["reasons"] == []
    assert cert["quarantined"] == 0
    assert cert["calibration_gaps"] == 0
    assert cert["breached"] is False
    assert len(cert["statement_hash"]) == 64
    assert cert["approvals"] == []

    approval = approve_report(
        db,
        org.id,
        "shipment_certificate",
        ship.id,
        cert["statement_hash"],
        "Ada",
        "ada@example.com",
        "approved",
    )
    assert approval.report_ref == ship.id
    cert2 = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)
    assert len(cert2["approvals"]) == 1
    assert cert2["approvals"][0]["signer_email"] == "ada@example.com"


def test_certificate_fail_breach(db: Session) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id, budget=5)
    ship = make_shipment(
        db,
        org.id,
        prof.id,
        status="complete",
        started_ms=BASE,
        ended_ms=BASE + 60 * MIN,
        device_ids=[dev.id],
    )
    add_reading(db, org.id, dev.id, 1, BASE + 10 * MIN, 5000)
    add_excursion(db, org.id, ship.id, dev.id, prof.id, BASE + 20 * MIN, BASE + 30 * MIN)

    cert = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)
    assert cert["verdict"] == "fail"
    assert cert["breached"] is True
    assert any("budget" in reason for reason in cert["reasons"])


def test_certificate_fail_quarantine(db: Session) -> None:
    org, ship = clean_complete_setup(db)
    dev_id = db.scalars(
        select(ShipmentDevice.device_id).where(ShipmentDevice.shipment_id == ship.id)
    ).one()
    add_reading(db, org.id, dev_id, 4, BASE + 40 * MIN, 5000, status="quarantined")

    cert = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)
    assert cert["verdict"] == "fail"
    assert cert["quarantined"] == 1
    assert any("quarantined" in reason for reason in cert["reasons"])


def test_certificate_fail_mkt_limit(db: Session) -> None:
    org, ship = clean_complete_setup(db, mkt_limit=4000)
    cert = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)
    assert cert["verdict"] == "fail"
    assert cert["mkt_mc"] is not None and cert["mkt_mc"] > 4000
    assert any("MKT" in reason for reason in cert["reasons"])


def test_certificate_fail_calibration_gap(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    org, ship = clean_complete_setup(db)

    def _gap(db: Any, device_id: str, ts_ms: int) -> bool:
        return False

    monkeypatch.setattr(ingest_module, "calibration_covering", _gap, raising=False)
    cert = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)
    assert cert["verdict"] == "fail"
    assert cert["calibration_gaps"] == 3
    assert any("calibration" in reason for reason in cert["reasons"])


def test_certificate_incomplete(db: Session) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(
        db, org.id, prof.id, status="in_transit", started_ms=BASE, device_ids=[dev.id]
    )
    add_reading(db, org.id, dev.id, 1, BASE + 10 * MIN, 5000)

    cert = shipment_certificate(db, org.id, ship.id, BASE + 60 * MIN)
    assert cert["verdict"] == "incomplete"
    assert cert["reasons"] != []


def test_certificate_unknown_shipment(db: Session) -> None:
    org = make_org(db)
    with pytest.raises(ValueError):
        shipment_certificate(db, org.id, "no-such-shipment", BASE)
