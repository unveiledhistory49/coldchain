"""Simulator determinism + injection tests."""

from __future__ import annotations

from sqlalchemy import select

from coldchain import ingest as ingest_mod
from coldchain.cli import run_simulation
from coldchain.db import create_app_engine, create_session_factory, init_db
from coldchain.models import Excursion, Organization

BASE = 1_712_000_000_000
MIN = 60_000


def _fresh_db() -> Any:

    engine = create_app_engine("sqlite:///:memory:")
    init_db(engine)
    return create_session_factory(engine)


def _setup_org_devices(n: int = 2) -> tuple[Any, str, list[str]]:
    factory = _fresh_db()
    db = factory()
    import uuid

    org = Organization(name="o", api_key_hash=uuid.uuid4().hex, key_prefix="p")
    db.add(org)
    db.flush()
    serials: list[str] = []
    for i in range(n):
        s = f"S{i}"
        serials.append(s)
        dev = ingest_mod.create_device(db, str(org.id), s)
        ingest_mod.add_calibration(db, str(org.id), dev.id, 0, 9_000_000_000_000, "C")
    db.commit()
    oid = str(org.id)
    db.close()
    return (factory, oid, serials)


def _read_stream(factory: Any, org_id: str, serial: str) -> list[tuple[int, int, int]]:
    from coldchain.models import Device

    db = factory()
    try:
        dev_id = db.execute(
            select(Device.id).where(Device.org_id == org_id, Device.serial == serial)
        ).scalar_one()
        rows = ingest_mod.device_readings(db, org_id, str(dev_id), limit=500)
        return [(r.temp_mc, r.seq, r.ts_ms) for r in rows]
    finally:
        db.close()


def test_same_seed_identical() -> None:
    f1, o1, serials = _setup_org_devices(2)
    db1 = f1()
    s1 = run_simulation(
        db1,
        o1,
        device_serials=serials,
        minutes=30,
        interval_s=60,
        seed=7,
        base_c=5.0,
        start_ms=BASE,
    )
    db1.close()
    stream1 = [_read_stream(f1, o1, s) for s in serials]

    f2, o2, serials2 = _setup_org_devices(2)
    db2 = f2()
    s2 = run_simulation(
        db2,
        o2,
        device_serials=serials2,
        minutes=30,
        interval_s=60,
        seed=7,
        base_c=5.0,
        start_ms=BASE,
    )
    db2.close()
    stream2 = [_read_stream(f2, o2, s) for s in serials2]

    assert s1 == s2
    assert stream1 == stream2
    assert len(stream1[0]) == 30


def test_dup_injection() -> None:
    f1, o1, serials = _setup_org_devices(1)
    db = f1()
    summary = run_simulation(
        db,
        o1,
        device_serials=serials,
        minutes=10,
        interval_s=60,
        seed=3,
        base_c=5.0,
        dup_rate=0.5,
        start_ms=BASE,
    )
    db.close()
    assert summary["duplicates"] > 0
    assert summary["sent"] > summary["accepted"]


def test_excursion_bump_creates_excursion() -> None:
    from sqlalchemy import select

    from coldchain.models import Device, Shipment, ShipmentDevice, TempProfile

    factory, org_id, serials = _setup_org_devices(1)
    db = factory()
    # profile 2..8C, shipment covering device
    prof = TempProfile(
        org_id=org_id,
        name="P",
        temp_min_mc=2000,
        temp_max_mc=8000,
        max_excursion_minutes=30,
    )
    db.add(prof)
    db.flush()
    dev_id = db.execute(
        select(Device.id).where(Device.org_id == org_id, Device.serial == serials[0])
    ).scalar_one()
    ship = Shipment(
        org_id=org_id,
        reference="S",
        profile_id=prof.id,
        status="in_transit",
        started_at_ms=BASE - MIN,
    )
    db.add(ship)
    db.flush()
    db.add(ShipmentDevice(shipment_id=ship.id, device_id=str(dev_id)))
    db.commit()
    summary = run_simulation(
        db,
        org_id,
        device_serials=serials,
        minutes=30,
        interval_s=60,
        seed=11,
        base_c=5.0,
        excursion=(5, 10, 10.0),
        start_ms=BASE,
    )
    assert summary["accepted"] > 0
    excs = db.execute(select(Excursion).where(Excursion.org_id == org_id)).scalars().all()
    db.close()
    assert len(excs) >= 1


from typing import Any  # noqa: E402  (kept at bottom for fixture helper ordering)
