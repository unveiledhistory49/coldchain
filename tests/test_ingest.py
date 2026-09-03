"""Tests for the ColdChain ingestion layer (test-session stubs only)."""

from __future__ import annotations

import sys
import time
import types
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

_EXC_PATH = Path(__file__).resolve().parents[1] / "src" / "coldchain" / "excursions.py"
if not _EXC_PATH.exists():  # test-session stub so coldchain/__init__ imports cleanly
    _exc_stub = types.ModuleType("coldchain.excursions")

    def _compute_mkt_mc(*args: object, **kwargs: object) -> int:
        return 0

    _exc_stub.compute_mkt_mc = _compute_mkt_mc  # type: ignore[attr-defined]
    sys.modules["coldchain.excursions"] = _exc_stub

import coldchain  # noqa: E402
from coldchain import ingest  # noqa: E402
from coldchain.db import create_app_engine, create_session_factory, init_db  # noqa: E402
from coldchain.models import Calibration, Device, Organization, Reading  # noqa: E402
from coldchain.temp import MAX_FUTURE_SKEW_MS, PHYS_MAX_MC, PHYS_MIN_MC  # noqa: E402

_COMP_PATH = Path(__file__).resolve().parents[1] / "src" / "coldchain" / "compliance.py"
if "coldchain.compliance" not in sys.modules:
    try:
        __import__("coldchain.compliance")
    except ModuleNotFoundError:
        if not _COMP_PATH.exists():  # test-session stub only; never shipped in src

            def _period_locked(db: object, org_id: str, ts_ms: int) -> bool:
                return False

            _comp_stub = types.ModuleType("coldchain.compliance")
            _comp_stub.period_locked = _period_locked  # type: ignore[attr-defined]
            sys.modules["coldchain.compliance"] = _comp_stub
            coldchain.compliance = _comp_stub

from coldchain import compliance  # noqa: E402


def _make_session() -> Session:
    engine = create_app_engine("sqlite:///:memory:")
    init_db(engine)
    factory = create_session_factory(engine)
    return factory()


def _make_org(db: Session, name: str = "org") -> Organization:
    org = Organization(name=name, api_key_hash=uuid.uuid4().hex, key_prefix="t")
    db.add(org)
    db.flush()
    return org


def _make_device(db: Session, org_id: str, serial: str = "SN-1") -> Device:
    return ingest.create_device(db, org_id, serial)


def _wide_cal(db: Session, org_id: str, device_id: str) -> Calibration:
    return ingest.add_calibration(db, org_id, device_id, 0, 9_000_000_000_000, "CERT")


def _locked_true(db: object, org_id: str, ts_ms: int) -> bool:
    return True


def _locked_false(db: object, org_id: str, ts_ms: int) -> bool:
    return False


def test_happy_path() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    reading, outcome, reason = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=4000
    )
    assert outcome == "accepted"
    assert reason == ""
    assert reading.flags == ""
    assert reading.status == "accepted"
    db.close()


def test_duplicate_same_payload() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    first, outcome1, _ = ingest.ingest_reading(
        db,
        org.id,
        device_id=dev.id,
        seq=0,
        ts_ms=1_000_000,
        temp_mc=4000,
        humidity_bp=100,
        battery_mv=3000,
    )
    assert outcome1 == "accepted"
    second, outcome2, reason2 = ingest.ingest_reading(
        db,
        org.id,
        device_id=dev.id,
        seq=0,
        ts_ms=1_000_000,
        temp_mc=4000,
        humidity_bp=100,
        battery_mv=3000,
    )
    assert outcome2 == "duplicate"
    assert reason2 == ""
    assert second.id == first.id
    count = len(db.query(Reading).filter(Reading.device_id == dev.id, Reading.seq == 0).all())
    assert count == 1
    db.close()


def test_seq_conflict() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=4000)
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=5000)
    db.close()


def test_unknown_device() -> None:
    db = _make_session()
    org = _make_org(db)
    with pytest.raises(ingest.UnknownDevice):
        ingest.ingest_reading(
            db, org.id, device_id="no-such-device", seq=0, ts_ms=1_000_000, temp_mc=0
        )
    with pytest.raises(ingest.UnknownDevice):
        ingest.get_device(db, org.id, "no-such-device")
    db.close()


def test_wrong_org_unknown() -> None:
    db = _make_session()
    org_a = _make_org(db, "a")
    org_b = _make_org(db, "b")
    dev_b = _make_device(db, org_b.id, "SN-B")
    with pytest.raises(ingest.UnknownDevice):
        ingest.get_device(db, org_a.id, dev_b.id)
    db.close()


def test_retired_device() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    ingest.retire_device(db, org.id, dev.id)
    with pytest.raises(ingest.RetiredDevice):
        ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=0)
    with pytest.raises(ingest.InvalidReading):
        ingest.retire_device(db, org.id, dev.id)
    db.close()


def test_bad_inputs() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(db, org.id, device_id=dev.id, seq=-1, ts_ms=1_000_000, temp_mc=0)
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=0, temp_mc=0)
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(
            db, org.id, device_id=dev.id, seq=1, ts_ms=1_000_000, temp_mc=PHYS_MAX_MC + 1
        )
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(
            db, org.id, device_id=dev.id, seq=2, ts_ms=1_000_000, temp_mc=PHYS_MIN_MC - 1
        )
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(
            db, org.id, device_id=dev.id, seq=3, ts_ms=1_000_000, temp_mc=0, humidity_bp=-1
        )
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(
            db, org.id, device_id=dev.id, seq=3, ts_ms=1_000_000, temp_mc=0, humidity_bp=10001
        )
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(
            db, org.id, device_id=dev.id, seq=4, ts_ms=1_000_000, temp_mc=0, battery_mv=-1
        )
    with pytest.raises(ingest.InvalidReading):
        ingest.ingest_reading(
            db, org.id, device_id=dev.id, seq=4, ts_ms=1_000_000, temp_mc=0, battery_mv=10001
        )
    with pytest.raises(ingest.InvalidReading):
        ingest.create_device(db, org.id, "   ")
    db.close()


def test_uncalibrated_quarantined() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    reading, outcome, reason = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=1000
    )
    assert outcome == "quarantined"
    assert reason == "uncalibrated"
    assert reading.status == "quarantined"
    assert reading.flags == "uncalibrated"
    db.close()


def test_future_skew_quarantined() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    future_ts = int(time.time() * 1000) + MAX_FUTURE_SKEW_MS + 60_000
    reading, outcome, reason = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=0, ts_ms=future_ts, temp_mc=1000
    )
    assert outcome == "quarantined"
    assert "future-skew" in reason
    assert "future-skew" in reading.flags
    assert reading.status == "quarantined"
    db.close()


def test_late_accepted() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=2_000_000, temp_mc=1000)
    reading, outcome, _ = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=1, ts_ms=1_000_000, temp_mc=1000
    )
    assert outcome == "accepted"
    assert reading.status == "accepted"
    assert "late" in reading.flags
    assert reading.flags.startswith("late")
    db.close()


def test_supersede_happy() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    original, _, _ = ingest.ingest_reading(
        db,
        org.id,
        device_id=dev.id,
        seq=0,
        ts_ms=1_000_000,
        temp_mc=1000,
        humidity_bp=500,
        battery_mv=3000,
    )
    orig_id = original.id
    orig_temp = original.temp_mc
    orig_ts = original.ts_ms
    fixed = ingest.supersede_reading(db, org.id, orig_id, temp_mc=2000, reason="probe offset")
    assert fixed.id != orig_id
    assert fixed.supersedes_id == orig_id
    assert fixed.ts_ms == orig_ts
    assert fixed.humidity_bp == 500
    assert fixed.battery_mv == 3000
    assert fixed.flags == f"supersedes:{orig_id}"
    assert fixed.status == "accepted"
    assert fixed.temp_mc == 2000
    db.expire_all()
    reloaded = db.get(Reading, orig_id)
    assert reloaded is not None
    assert reloaded.status == "superseded"
    assert reloaded.temp_mc == orig_temp
    assert reloaded.ts_ms == orig_ts
    db.close()


def test_supersede_quarantined_rejected() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    quarantined, outcome, _ = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=1000
    )
    assert outcome == "quarantined"
    with pytest.raises(ingest.InvalidReading):
        ingest.supersede_reading(db, org.id, quarantined.id, temp_mc=2000, reason="fix")
    db.close()


def test_double_supersede_rejected() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    original, _, _ = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=1000
    )
    ingest.supersede_reading(db, org.id, original.id, temp_mc=2000, reason="first fix")
    with pytest.raises(ingest.InvalidReading):
        ingest.supersede_reading(db, org.id, original.id, temp_mc=3000, reason="second fix")
    db.close()


def test_cross_org_leak() -> None:
    db = _make_session()
    org_a = _make_org(db, "a")
    org_b = _make_org(db, "b")
    dev_b = _make_device(db, org_b.id, "SN-B")
    ingest.add_calibration(db, org_b.id, dev_b.id, 0, 9_000_000_000_000, "C")
    with pytest.raises(ingest.UnknownDevice):
        ingest.ingest_reading(db, org_a.id, device_id=dev_b.id, seq=0, ts_ms=1_000_000, temp_mc=0)
    db.close()


def test_locked_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _make_session()
    org = _make_org(db)
    dev = _make_device(db, org.id)
    _wide_cal(db, org.id, dev.id)
    monkeypatch.setattr(compliance, "period_locked", _locked_true)
    with pytest.raises(ingest.LockedPeriod):
        ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=0)
    monkeypatch.setattr(compliance, "period_locked", _locked_false)
    reading, outcome, _ = ingest.ingest_reading(
        db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=0
    )
    assert outcome == "accepted"
    monkeypatch.setattr(compliance, "period_locked", _locked_true)
    with pytest.raises(ingest.LockedPeriod):
        ingest.supersede_reading(db, org.id, reading.id, temp_mc=10, reason="fix")
    db.close()


def test_device_helpers() -> None:
    db = _make_session()
    org = _make_org(db)
    dev = ingest.create_device(db, org.id, "  SN-9 ", " Name ", " M ")
    assert dev.serial == "SN-9"
    assert dev.status == "active"
    with pytest.raises(ingest.InvalidReading) as exc:
        ingest.create_device(db, org.id, "SN-9")
    assert "conflict" in str(exc.value).lower()
    got = ingest.get_device(db, org.id, dev.id)
    assert got.id == dev.id
    assert ingest.device_max_ts(db, dev.id) == 0
    assert ingest.device_max_seq(db, dev.id) == -1
    _wide_cal(db, org.id, dev.id)
    ingest.ingest_reading(db, org.id, device_id=dev.id, seq=0, ts_ms=1_000_000, temp_mc=0)
    assert ingest.device_max_ts(db, dev.id) == 1_000_000
    assert ingest.device_max_seq(db, dev.id) == 0
    rows = ingest.device_readings(db, org.id, dev.id)
    assert len(rows) == 1
    cal = ingest.calibration_covering(db, dev.id, 1_000_000)
    assert cal is not None
    assert ingest.calibration_covering(db, dev.id, 9_000_000_000_001) is None
    db.close()
