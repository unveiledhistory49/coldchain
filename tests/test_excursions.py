"""Tests for coldchain.excursions (sqlite :memory:, rows built via models)."""

from __future__ import annotations

import math
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from coldchain import webhooks as webhooks_module
from coldchain.db import create_app_engine, create_session_factory, init_db
from coldchain.excursions import (
    ExcursionEvent,
    compute_mkt_mc,
    evaluate_reading,
    shipment_excursion_minutes,
    shipment_stats,
)
from coldchain.models import (
    Device,
    Excursion,
    Organization,
    Reading,
    Shipment,
    ShipmentDevice,
    TempProfile,
)

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
def webhook_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record webhook enqueue calls instead of hitting the real dispatcher."""
    calls: list[dict[str, Any]] = []

    def _fake(db: Any, *, org_id: str, event: str, payload: dict[str, Any]) -> None:
        calls.append({"org_id": org_id, "event": event, "payload": payload})

    monkeypatch.setattr(webhooks_module, "enqueue", _fake, raising=False)
    return calls


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
    started_ms: int = BASE - 60 * MIN,
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


def open_excursion(db: Session, shipment_id: str, device_id: str) -> Excursion | None:
    from sqlalchemy import select

    return db.scalars(
        select(Excursion).where(
            Excursion.shipment_id == shipment_id,
            Excursion.device_id == device_id,
            Excursion.status == "open",
        )
    ).first()


def test_open_on_out_of_range(db: Session, webhook_calls: list[dict[str, Any]]) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])
    reading = add_reading(db, org.id, dev.id, 1, BASE, 9000)

    events = evaluate_reading(db, org.id, reading)

    assert len(events) == 1
    assert isinstance(events[0], ExcursionEvent)
    assert events[0].kind == "opened"
    assert events[0].shipment_id == ship.id
    exc = open_excursion(db, ship.id, dev.id)
    assert exc is not None
    assert exc.started_ts_ms == BASE
    assert exc.peak_mc == 9000 and exc.trough_mc == 9000
    assert [call["event"] for call in webhook_calls] == ["excursion.opened"]


def test_boundary_temps_inclusive(db: Session, webhook_calls: list[dict[str, Any]]) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])

    assert evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 1, BASE, 2000)) == []
    assert evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 2, BASE + MIN, 8000)) == []
    assert open_excursion(db, ship.id, dev.id) is None

    trip_low = evaluate_reading(
        db, org.id, add_reading(db, org.id, dev.id, 3, BASE + 2 * MIN, 1999)
    )
    assert [event.kind for event in trip_low] == ["opened"]
    trip_high = evaluate_reading(
        db, org.id, add_reading(db, org.id, dev.id, 4, BASE + 3 * MIN, 8001)
    )
    assert trip_high == []  # extends the open excursion, no new event
    exc = open_excursion(db, ship.id, dev.id)
    assert exc is not None
    assert exc.peak_mc == 8001 and exc.trough_mc == 1999
    assert webhook_calls[-1]["event"] == "excursion.opened"


def test_peak_trough_tracking(db: Session) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])

    evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 1, BASE, 9000))
    assert evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 2, BASE + MIN, 9500)) == []
    assert (
        evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 3, BASE + 2 * MIN, 8500)) == []
    )
    exc = open_excursion(db, ship.id, dev.id)
    assert exc is not None
    assert exc.peak_mc == 9500
    assert exc.trough_mc == 8500
    assert exc.status == "open"


def test_close_on_return_in_bounds(db: Session, webhook_calls: list[dict[str, Any]]) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])

    evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 1, BASE, 9000))
    events = evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 2, BASE + 5 * MIN, 5000))

    assert [event.kind for event in events] == ["closed"]
    assert open_excursion(db, ship.id, dev.id) is None
    from sqlalchemy import select

    closed = db.scalars(
        select(Excursion).where(Excursion.shipment_id == ship.id, Excursion.status == "closed")
    ).one()
    assert closed.ended_ts_ms == BASE + 5 * MIN
    assert [call["event"] for call in webhook_calls] == [
        "excursion.opened",
        "excursion.closed",
    ]
    assert shipment_excursion_minutes(db, org.id, ship.id, BASE + 5 * MIN) == 5


def test_late_data_clamp(db: Session) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])

    evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 1, BASE + 10 * MIN, 9000))
    # Out-of-order in-bounds reading older than the excursion start.
    events = evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 2, BASE, 5000))

    assert [event.kind for event in events] == ["closed"]
    from sqlalchemy import select

    closed = db.scalars(
        select(Excursion).where(Excursion.shipment_id == ship.id, Excursion.status == "closed")
    ).one()
    assert closed.ended_ts_ms == BASE + 10 * MIN  # clamped to started, never before it
    assert shipment_excursion_minutes(db, org.id, ship.id, BASE + 10 * MIN) == 0


def test_budget_breach_exactly_once(db: Session, webhook_calls: list[dict[str, Any]]) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id, budget=30)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])

    # Excursion 1: 20 of 30 budget minutes — no breach.
    evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 1, BASE, 9000))
    events = evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 2, BASE + 20 * MIN, 5000))
    assert [event.kind for event in events] == ["closed"]

    # Excursion 2: +15 minutes, cumulative 35 > 30 — breach fires once.
    evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 3, BASE + 30 * MIN, 9000))
    events = evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 4, BASE + 45 * MIN, 5000))
    assert [event.kind for event in events] == ["closed", "budget_breached"]
    assert events[1].shipment_id == ship.id
    breached_payloads = [
        call for call in webhook_calls if call["event"] == "excursion.budget_breached"
    ]
    assert len(breached_payloads) == 1
    assert breached_payloads[0]["payload"]["cumulative_minutes"] == pytest.approx(35.0)

    # Excursion 3: cumulative grows but the crossing already happened — silent.
    evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 5, BASE + 60 * MIN, 9000))
    events = evaluate_reading(db, org.id, add_reading(db, org.id, dev.id, 6, BASE + 70 * MIN, 5000))
    assert [event.kind for event in events] == ["closed"]
    assert len([c for c in webhook_calls if c["event"] == "excursion.budget_breached"]) == 1
    assert shipment_excursion_minutes(db, org.id, ship.id, BASE + 70 * MIN) == 45


def test_skips(db: Session, webhook_calls: list[dict[str, Any]]) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id)
    ship = make_shipment(db, org.id, prof.id, device_ids=[dev.id])
    other_org = make_org(db, name="Other")

    quarantined = add_reading(db, org.id, dev.id, 1, BASE, 9000, status="quarantined")
    assert evaluate_reading(db, org.id, quarantined) == []

    foreign = add_reading(db, other_org.id, dev.id, 2, BASE, 9000)
    assert evaluate_reading(db, org.id, foreign) == []

    lone = make_device(db, org.id, serial="LONE")
    unattached = add_reading(db, org.id, lone.id, 1, BASE, 9000)
    assert evaluate_reading(db, org.id, unattached) == []

    ship.status = "complete"
    db.flush()
    done_reading = add_reading(db, org.id, dev.id, 3, BASE, 9000)
    assert evaluate_reading(db, org.id, done_reading) == []

    assert open_excursion(db, ship.id, dev.id) is None
    assert webhook_calls == []


def _mkt_reference_mc(temps_mc: list[int]) -> int:
    """Independent calculator-style MKT (plain math, same USP <1079> formula)."""
    dh_r = 83144.0 / 8.314
    terms = [math.exp(-dh_r / (t / 1000.0 + 273.15)) for t in temps_mc]
    mkt_k = dh_r / (-math.log(sum(terms) / len(terms)))
    return int((Decimal(mkt_k - 273.15) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def test_mkt_constant() -> None:
    assert compute_mkt_mc([5000] * 10) == 5000


def test_mkt_two_temps_matches_reference() -> None:
    expected = _mkt_reference_mc([2000, 8000])
    assert compute_mkt_mc([2000, 8000]) == expected
    assert 5000 < expected < 8000  # Arrhenius weighting pulls MKT above the mean


def test_mkt_invalid() -> None:
    with pytest.raises(ValueError):
        compute_mkt_mc([])
    with pytest.raises(ValueError):
        compute_mkt_mc([5000, -273150])
    with pytest.raises(ValueError):
        compute_mkt_mc([-300000])


def test_shipment_stats(db: Session) -> None:
    org = make_org(db)
    dev = make_device(db, org.id)
    prof = make_profile(db, org.id, mkt_limit=9000)
    ship = make_shipment(
        db, org.id, prof.id, started_ms=BASE, ended_ms=BASE + 60 * MIN, device_ids=[dev.id]
    )
    add_reading(db, org.id, dev.id, 1, BASE + 10 * MIN, 4000)
    add_reading(db, org.id, dev.id, 2, BASE + 20 * MIN, 6000)
    add_reading(db, org.id, dev.id, 3, BASE + 30 * MIN, 5000, status="superseded")
    add_reading(db, org.id, dev.id, 4, BASE + 40 * MIN, 5000, status="quarantined")
    add_reading(db, org.id, dev.id, 5, BASE - MIN, 9999)  # outside window: ignored

    stats = shipment_stats(db, org.id, ship.id, BASE + 60 * MIN)

    assert stats["shipment_id"] == ship.id
    assert stats["readings_total"] == 3  # accepted + superseded in window
    assert stats["quarantined"] == 1
    assert stats["excursions_open"] == 0
    assert stats["excursions_closed"] == 0
    assert stats["excursion_minutes"] == 0
    assert stats["budget_minutes"] == 30
    assert stats["breached"] is False
    assert stats["mkt_mc"] == compute_mkt_mc([4000, 6000])
    assert stats["temp_min_mc"] == 4000
    assert stats["temp_max_mc"] == 6000
    assert stats["mkt_limit_mc"] == 9000
