"""Ingestion layer: devices, calibrations, and write-once readings."""

from __future__ import annotations

import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from coldchain.models import Calibration, Device, Reading
from coldchain.temp import MAX_FUTURE_SKEW_MS, PHYS_MAX_MC, PHYS_MIN_MC


class IngestError(Exception):
    """Base error for ingestion failures."""


class UnknownDevice(IngestError):
    """Device is missing or belongs to a different organization."""


class RetiredDevice(IngestError):
    """Device exists but has been retired."""


class InvalidReading(IngestError):
    """Reading payload or state transition is invalid."""


class LockedPeriod(IngestError):
    """Target timestamp falls inside a locked compliance period."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def create_device(db: Session, org_id: str, serial: str, name: str = "", model: str = "") -> Device:
    """Create an active device scoped to an organization."""
    serial_c = serial.strip()
    name_c = name.strip()
    model_c = model.strip()
    if not serial_c:
        raise InvalidReading("serial must be non-empty")
    existing = db.scalar(
        select(Device).where(Device.org_id == org_id, Device.serial == serial_c),
    )
    if existing is not None:
        raise InvalidReading(f"device conflict: (org, serial) already exists: {serial_c!r}")
    device = Device(org_id=org_id, serial=serial_c, name=name_c, model=model_c, status="active")
    db.add(device)
    db.flush()
    return device


def get_device(db: Session, org_id: str, device_id: str) -> Device:
    """Return a device or raise UnknownDevice when missing or cross-org."""
    device = db.get(Device, device_id)
    if device is None or device.org_id != org_id:
        raise UnknownDevice(f"unknown device {device_id!r}")
    return device


def retire_device(db: Session, org_id: str, device_id: str) -> Device:
    """Mark a device retired; retiring twice is invalid."""
    device = get_device(db, org_id, device_id)
    if device.status == "retired":
        raise InvalidReading(f"device already retired: {device_id!r}")
    device.status = "retired"
    db.flush()
    return device


def add_calibration(
    db: Session,
    org_id: str,
    device_id: str,
    valid_from_ms: int,
    valid_until_ms: int,
    certificate_ref: str = "",
) -> Calibration:
    """Add a calibration window for a device in the same organization."""
    get_device(db, org_id, device_id)
    if not _is_int(valid_from_ms) or not _is_int(valid_until_ms):
        raise InvalidReading("calibration bounds must be integers")
    if valid_from_ms < 0 or valid_until_ms < 0:
        raise InvalidReading("calibration bounds must be non-negative")
    if not valid_from_ms < valid_until_ms:
        raise InvalidReading("valid_from_ms must be < valid_until_ms")
    cal = Calibration(
        org_id=org_id,
        device_id=device_id,
        valid_from_ms=valid_from_ms,
        valid_until_ms=valid_until_ms,
        certificate_ref=certificate_ref.strip(),
    )
    db.add(cal)
    db.flush()
    return cal


def calibration_covering(db: Session, device_id: str, ts_ms: int) -> Calibration | None:
    """Return the covering window for ts, latest valid_from wins, else None."""
    return db.scalar(
        select(Calibration)
        .where(
            Calibration.device_id == device_id,
            Calibration.valid_from_ms <= ts_ms,
            Calibration.valid_until_ms > ts_ms,
        )
        .order_by(Calibration.valid_from_ms.desc())
        .limit(1),
    )


def device_max_ts(db: Session, device_id: str) -> int:
    """Return max ts_ms for a device, or 0 when no readings exist."""
    stmt = select(func.max(Reading.ts_ms)).where(Reading.device_id == device_id)
    value: int | None = db.scalar(stmt)
    return int(value) if value is not None else 0


def device_max_seq(db: Session, device_id: str) -> int:
    """Return max seq for a device, or -1 when none (0-based sequence)."""
    stmt = select(func.max(Reading.seq)).where(Reading.device_id == device_id)
    value = db.scalar(stmt)
    return int(value) if value is not None else -1


def device_readings(
    db: Session, org_id: str, device_id: str, since_ms: int = 0, limit: int = 50
) -> list[Reading]:
    """List readings for an org/device ordered by ts_ms with limit clamped 1..500."""
    capped = max(1, min(500, limit))
    rows = db.scalars(
        select(Reading)
        .where(Reading.org_id == org_id, Reading.device_id == device_id, Reading.ts_ms >= since_ms)
        .order_by(Reading.ts_ms.asc())
        .limit(capped),
    ).all()
    return list(rows)


def _validate_reading_fields(
    seq: int, ts_ms: int, temp_mc: int, humidity_bp: int, battery_mv: int
) -> None:
    if not _is_int(seq) or seq < 0:
        raise InvalidReading("seq must be an int >= 0")
    if not _is_int(ts_ms) or ts_ms <= 0:
        raise InvalidReading("ts_ms must be an int > 0")
    if not _is_int(temp_mc) or temp_mc < PHYS_MIN_MC or temp_mc > PHYS_MAX_MC:
        raise InvalidReading("temp_mc outside physical range")
    if not _is_int(humidity_bp) or humidity_bp < 0 or humidity_bp > 10000:
        raise InvalidReading("humidity_bp must be in 0..10000")
    if not _is_int(battery_mv) or battery_mv < 0 or battery_mv > 10000:
        raise InvalidReading("battery_mv must be in 0..10000")


def _validate_temp(temp_mc: int) -> None:
    if not _is_int(temp_mc) or temp_mc < PHYS_MIN_MC or temp_mc > PHYS_MAX_MC:
        raise InvalidReading("temp_mc outside physical range")


def ingest_reading(
    db: Session,
    org_id: str,
    *,
    device_id: str,
    seq: int,
    ts_ms: int,
    temp_mc: int,
    humidity_bp: int = 0,
    battery_mv: int = 0,
) -> tuple[Reading, str, str]:
    """Ingest one reading, returning (reading, outcome, reason).

    Outcomes are "accepted", "duplicate", or "quarantined"; reason mirrors
    the persisted comma-joined flags ("" when clean or duplicate).
    """
    device = get_device(db, org_id, device_id)
    if device.status == "retired":
        raise RetiredDevice(f"device retired: {device_id!r}")
    _validate_reading_fields(seq, ts_ms, temp_mc, humidity_bp, battery_mv)

    existing = db.scalar(select(Reading).where(Reading.device_id == device_id, Reading.seq == seq))
    if existing is not None:
        if (
            existing.ts_ms == ts_ms
            and existing.temp_mc == temp_mc
            and existing.humidity_bp == humidity_bp
            and existing.battery_mv == battery_mv
        ):
            return (existing, "duplicate", "")
        raise InvalidReading(f"seq conflict: payload differs under same seq {seq}")

    from coldchain import compliance

    if bool(compliance.period_locked(db, org_id, ts_ms)):
        raise LockedPeriod(f"period locked for ts {ts_ms}")

    now_ms = int(time.time() * 1000)
    is_future = ts_ms > now_ms + MAX_FUTURE_SKEW_MS
    is_uncalibrated = calibration_covering(db, device_id, ts_ms) is None
    is_late = ts_ms < device_max_ts(db, device_id)

    flags: list[str] = []
    if is_late:
        flags.append("late")
    if is_future:
        flags.append("future-skew")
    if is_uncalibrated:
        flags.append("uncalibrated")
    flags_str = ",".join(flags)
    if is_future or is_uncalibrated:
        status = "quarantined"
        outcome = "quarantined"
    else:
        status = "accepted"
        outcome = "accepted"
    reading = Reading(
        org_id=org_id,
        device_id=device_id,
        seq=seq,
        ts_ms=ts_ms,
        temp_mc=temp_mc,
        humidity_bp=humidity_bp,
        battery_mv=battery_mv,
        flags=flags_str,
        status=status,
    )
    db.add(reading)
    db.flush()
    return (reading, outcome, flags_str)


def supersede_reading(
    db: Session, org_id: str, reading_id: str, *, temp_mc: int, reason: str
) -> Reading:
    """Create a superseding correction row for an accepted reading.

    Only the old row's ``status`` column changes (to "superseded");
    measurement columns (device/seq/ts/temp) are write-once and never
    updated in place.
    """
    original = db.get(Reading, reading_id)
    if original is None or original.org_id != org_id:
        raise InvalidReading(f"unknown reading {reading_id!r}")
    if original.status != "accepted":
        raise InvalidReading("only accepted readings can be superseded")
    _validate_temp(temp_mc)
    if not reason.strip():
        raise InvalidReading("reason must be non-empty")

    from coldchain import compliance

    if bool(compliance.period_locked(db, org_id, original.ts_ms)):
        raise LockedPeriod(f"period locked for ts {original.ts_ms}")

    new_seq = device_max_seq(db, original.device_id) + 1
    correction = Reading(
        org_id=org_id,
        device_id=original.device_id,
        seq=new_seq,
        ts_ms=original.ts_ms,
        temp_mc=temp_mc,
        humidity_bp=original.humidity_bp,
        battery_mv=original.battery_mv,
        flags=f"supersedes:{original.id}",
        status="accepted",
        supersedes_id=original.id,
    )
    db.add(correction)
    original.status = "superseded"
    db.flush()
    return correction
