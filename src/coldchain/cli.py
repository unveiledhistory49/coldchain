"""ColdChain CLI (argparse, not click)."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections.abc import Sequence

from sqlalchemy.orm import Session

from coldchain import compliance as comp_mod
from coldchain import excursions as exc_mod
from coldchain import ingest as ingest_mod
from coldchain import reports as reports_mod
from coldchain.auth import new_api_key
from coldchain.config import settings
from coldchain.db import create_app_engine, create_session_factory, init_db
from coldchain.models import Organization


def _open_session(db_url: str) -> Session:
    engine = create_app_engine(db_url)
    init_db(engine)
    factory = create_session_factory(engine)
    return factory()


def _parse_excursion(spec: str | None) -> tuple[int, int, float] | None:
    if spec is None:
        return None
    parts = spec.split(",")
    if len(parts) != 3:
        raise ValueError("--excursion want start_min,duration_min,offset_c")
    return (int(parts[0]), int(parts[1]), float(parts[2]))


def run_simulation(  # noqa: C901
    db: Session,
    org_id: str,
    device_serials: list[str] | None = None,
    device_ids: list[str] | None = None,
    minutes: int = 120,
    interval_s: int = 60,
    seed: int = 7,
    base_c: float = 5.0,
    excursion: tuple[int, int, float] | None = None,
    dup_rate: float = 0.0,
    late_rate: float = 0.0,
    start_ms: int | None = None,
) -> dict[str, int]:
    """Stream synthetic readings via ingest + excursion evaluation.

    Returns {"sent": int, "accepted": int, "duplicates": int, "quarantined": int}.
    Deterministic for a given seed and device order.
    """
    from sqlalchemy import select

    from coldchain.models import Device

    targets: list[tuple[str, int]] = []  # (device_id, start_seq)
    if device_ids is not None:
        for did in device_ids:
            dev = ingest_mod.get_device(db, org_id, did)
            targets.append((dev.id, ingest_mod.device_max_seq(db, dev.id) + 1))
    elif device_serials is not None:
        for serial in device_serials:
            row = db.execute(
                select(Device).where(Device.org_id == org_id, Device.serial == serial)
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"unknown device serial {serial!r}")
            targets.append((row.id, ingest_mod.device_max_seq(db, row.id) + 1))
    else:
        raise ValueError("device_serials or device_ids is required")

    steps = max(1, int(minutes * 60 // max(interval_s, 1)))
    if start_ms is None:
        start_ms = int(time.time() * 1000) - minutes * 60 * 1000
    base_start: int = int(start_ms)

    rng = random.Random(seed)  # noqa: S311 -- deterministic simulation, not crypto
    sent = 0
    accepted = 0
    duplicates = 0
    quarantined = 0

    exc_start, exc_dur, exc_off = excursion if excursion is not None else (0, 0, 0.0)

    for t in range(steps):
        ts_base = base_start + t * interval_s * 1000
        minute = (t * interval_s) / 60.0
        in_bump = excursion is not None and (exc_start <= minute < exc_start + exc_dur)
        for device_id, start_seq in targets:
            seq = start_seq + t
            noise = rng.gauss(0.0, 0.5)
            temp_c = base_c + noise + (exc_off if in_bump else 0.0)
            temp_mc = int(round(temp_c * 1000))
            ts_ms = ts_base
            if late_rate > 0 and rng.random() < late_rate:
                back = rng.randint(1, 10) * interval_s * 1000
                cand = ts_base - back
                if cand > 0:
                    ts_ms = cand
            reading, outcome, _ = ingest_mod.ingest_reading(
                db,
                org_id,
                device_id=device_id,
                seq=seq,
                ts_ms=ts_ms,
                temp_mc=temp_mc,
            )
            sent += 1
            if outcome == "accepted":
                accepted += 1
                exc_mod.evaluate_reading(db, org_id, reading)
            elif outcome == "duplicate":
                duplicates += 1
            elif outcome == "quarantined":
                quarantined += 1
            if dup_rate > 0 and rng.random() < dup_rate:
                # Resend identical payload: same seq/ts/temp.
                _, o2, _ = ingest_mod.ingest_reading(
                    db,
                    org_id,
                    device_id=device_id,
                    seq=seq,
                    ts_ms=ts_ms,
                    temp_mc=temp_mc,
                )
                sent += 1
                if o2 == "duplicate":
                    duplicates += 1
                elif o2 == "accepted":
                    accepted += 1
                elif o2 == "quarantined":
                    quarantined += 1
    db.commit()
    return {
        "sent": sent,
        "accepted": accepted,
        "duplicates": duplicates,
        "quarantined": quarantined,
    }


def _cmd_provision_org(args: argparse.Namespace) -> int:
    db = _open_session(args.db)
    try:
        raw, digest, prefix = new_api_key()
        org = Organization(name=args.name, api_key_hash=digest, key_prefix=prefix)
        db.add(org)
        db.commit()
        db.refresh(org)
        print(json.dumps({"org_id": org.id, "api_key": raw, "key_prefix": prefix}))
        return 0
    finally:
        db.close()


def _cmd_register_device(args: argparse.Namespace) -> int:
    db = _open_session(args.db)
    try:
        dev = ingest_mod.create_device(db, args.org_id, args.serial, args.name, args.model)
        db.commit()
        print(
            json.dumps({"id": dev.id, "serial": dev.serial, "name": dev.name, "model": dev.model})
        )
        return 0
    finally:
        db.close()


def _cmd_serve(args: argparse.Namespace) -> int:
    settings.database_url = args.db
    import uvicorn

    from coldchain.api import create_app

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    db = _open_session(args.db)
    try:
        serials = [s.strip() for s in str(args.devices).split(",") if s.strip()]
        bump = _parse_excursion(args.excursion)
        summary = run_simulation(
            db,
            args.org_id,
            device_serials=serials,
            minutes=args.minutes,
            interval_s=args.interval_s,
            seed=args.seed,
            base_c=args.base_c,
            excursion=bump,
            dup_rate=args.dup_rate,
            late_rate=args.late_rate,
            start_ms=args.start_ms,
        )
        print(json.dumps(summary))
        return 0
    finally:
        db.close()


def _cmd_report(args: argparse.Namespace) -> int:
    db = _open_session(args.db)
    try:
        now_ms = int(time.time() * 1000)
        cert = reports_mod.shipment_certificate(db, args.org_id, args.shipment_id, now_ms)
        print(json.dumps(cert, default=str))
        return 0
    finally:
        db.close()


def _cmd_lock_period(args: argparse.Namespace) -> int:
    db = _open_session(args.db)
    try:
        lock = comp_mod.lock_period(db, args.org_id, args.period)
        db.commit()
        print(json.dumps({"id": lock.id, "period": lock.period}))
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coldchain")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("provision-org")
    pp.add_argument("--name", required=True)
    pp.add_argument("--db", default=settings.database_url)
    pp.set_defaults(_fn=_cmd_provision_org)

    pr = sub.add_parser("register-device")
    pr.add_argument("--db", default=settings.database_url)
    pr.add_argument("--org-id", required=True)
    pr.add_argument("--serial", required=True)
    pr.add_argument("--name", default="")
    pr.add_argument("--model", default="")
    pr.set_defaults(_fn=_cmd_register_device)

    ps = sub.add_parser("serve")
    ps.add_argument("--db", default=settings.database_url)
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8000)
    ps.set_defaults(_fn=_cmd_serve)

    pm = sub.add_parser("simulate")
    pm.add_argument("--db", default=settings.database_url)
    pm.add_argument("--org-id", required=True)
    pm.add_argument("--devices", required=True, help="comma-separated serials")
    pm.add_argument("--minutes", type=int, default=120)
    pm.add_argument("--interval-s", type=int, default=60)
    pm.add_argument("--seed", type=int, default=7)
    pm.add_argument("--base-c", type=float, default=5.0)
    pm.add_argument("--excursion", default=None, help="start_min,duration_min,offset_c")
    pm.add_argument("--dup-rate", type=float, default=0.0)
    pm.add_argument("--late-rate", type=float, default=0.0)
    pm.add_argument("--start-ms", type=int, default=None)
    pm.set_defaults(_fn=_cmd_simulate)

    rp = sub.add_parser("report")
    rp.add_argument("--db", default=settings.database_url)
    rp.add_argument("--org-id", required=True)
    rp.add_argument("--shipment-id", required=True)
    rp.set_defaults(_fn=_cmd_report)

    lp = sub.add_parser("lock-period")
    lp.add_argument("--db", default=settings.database_url)
    lp.add_argument("--org-id", required=True)
    lp.add_argument("--period", required=True)
    lp.set_defaults(_fn=_cmd_lock_period)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    fn = getattr(args, "_fn", None)
    if fn is None:
        parser.print_help()
        return 2
    return int(fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
