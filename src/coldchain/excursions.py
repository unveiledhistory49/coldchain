"""Excursion detection, MKT, and shipment excursion statistics.

Determinism notes
-----------------
* Open excursions accrue time only up to the ``ts_ms`` of the reading being
  evaluated (``now_ms == reading.ts_ms``). Replaying the same readings in the
  same order therefore always yields the same cumulative minutes.
* ``budget_breached`` is emitted at most once per crossing: after a close, the
  cumulative minutes are computed twice — with and without the just-closed
  excursion's contribution — and the event fires only when
  ``minutes_without <= budget < minutes_with``.
* At most one ``open`` excursion per ``(shipment, device)`` pair is
  maintained; a late (out-of-order) in-bounds reading clamps ``ended_ts_ms``
  to ``max(reading.ts, started)`` so spans can never go negative.

All helpers stage rows with ``flush()``; the caller owns the commit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from coldchain.models import Excursion, Reading, Shipment, ShipmentDevice, TempProfile

MS_PER_MINUTE = 60_000
_MKT_DH_J_PER_MOL = 83144.0
_MKT_R_J_PER_MOL_K = 8.314
_ABS_ZERO_MC = -273150


class ExcursionError(Exception):
    """Raised when excursion evaluation inputs are inconsistent."""


@dataclass
class ExcursionEvent:
    """One state transition observed while evaluating a reading."""

    kind: str  # "opened" | "closed" | "budget_breached"
    excursion_id: str
    shipment_id: str


def _excursion_minutes_float(excursions: list[Excursion], now_ms: int) -> float:
    """Sum excursion durations in fractional minutes.

    Closed excursions contribute their full ``(ended - started)`` span; open
    excursions count up to ``now_ms``. Negative spans (which cannot occur via
    :func:`evaluate_reading` because closes clamp ``ended >= started``) count
    as zero.
    """
    total = 0.0
    for excursion in excursions:
        if excursion.status == "open":
            total += max(now_ms - excursion.started_ts_ms, 0) / MS_PER_MINUTE
        else:
            total += max(excursion.ended_ts_ms - excursion.started_ts_ms, 0) / MS_PER_MINUTE
    return total


def _open_excursion(db: Session, shipment_id: str, device_id: str) -> Excursion | None:
    """Return the open excursion for ``(shipment, device)``, if any."""
    return db.scalars(
        select(Excursion)
        .where(
            Excursion.shipment_id == shipment_id,
            Excursion.device_id == device_id,
            Excursion.status == "open",
        )
        .order_by(Excursion.started_ts_ms)
    ).first()


def _shipment_excursions(db: Session, org_id: str, shipment_id: str) -> list[Excursion]:
    """Return every excursion row for a shipment in an org."""
    return list(
        db.scalars(
            select(Excursion).where(
                Excursion.org_id == org_id,
                Excursion.shipment_id == shipment_id,
            )
        ).all()
    )


def evaluate_reading(db: Session, org_id: str, reading: Reading) -> list[ExcursionEvent]:
    """Evaluate one reading against in-transit shipments on its device.

    Skips (returns ``[]``) unless ``reading.status == "accepted"`` and the
    reading belongs to ``org_id``. For each in-transit shipment the device is
    attached to, an out-of-range temperature (outside the profile bounds,
    boundary inclusive) opens or extends an excursion; an in-bounds
    temperature closes the open excursion. Webhook rows for
    ``excursion.opened`` / ``excursion.closed`` / ``excursion.budget_breached``
    are staged via ``coldchain.webhooks.enqueue``. Flushes; never commits.
    """
    from coldchain.webhooks import enqueue as enqueue_webhook

    events: list[ExcursionEvent] = []
    if reading.status != "accepted":
        return events
    if reading.org_id != org_id:
        return events
    shipments = db.scalars(
        select(Shipment)
        .join(ShipmentDevice, ShipmentDevice.shipment_id == Shipment.id)
        .where(
            Shipment.org_id == org_id,
            Shipment.status == "in_transit",
            ShipmentDevice.device_id == reading.device_id,
        )
    ).all()
    for shipment in shipments:
        profile = db.get(TempProfile, shipment.profile_id)
        if profile is None:
            raise ExcursionError(
                f"shipment {shipment.id} references missing profile {shipment.profile_id}"
            )
        if profile.temp_min_mc <= reading.temp_mc <= profile.temp_max_mc:
            excursion = _open_excursion(db, shipment.id, reading.device_id)
            if excursion is None:
                continue
            excursion.status = "closed"
            excursion.ended_ts_ms = max(reading.ts_ms, excursion.started_ts_ms)
            db.flush()
            events.append(
                ExcursionEvent(
                    kind="closed", excursion_id=excursion.id, shipment_id=str(shipment.id)
                )
            )
            enqueue_webhook(
                db,
                org_id=org_id,
                event="excursion.closed",
                payload={
                    "shipment_id": shipment.id,
                    "excursion_id": excursion.id,
                    "device_id": reading.device_id,
                    "started_ts_ms": excursion.started_ts_ms,
                    "ended_ts_ms": excursion.ended_ts_ms,
                    "duration_minutes": (
                        (excursion.ended_ts_ms - excursion.started_ts_ms) / MS_PER_MINUTE
                    ),
                },
            )
            all_excursions = _shipment_excursions(db, org_id, str(shipment.id))
            total = _excursion_minutes_float(all_excursions, reading.ts_ms)
            just_closed = max(excursion.ended_ts_ms - excursion.started_ts_ms, 0) / MS_PER_MINUTE
            before = total - just_closed
            budget = float(profile.max_excursion_minutes)
            if before <= budget < total:
                events.append(
                    ExcursionEvent(
                        kind="budget_breached",
                        excursion_id=excursion.id,
                        shipment_id=str(shipment.id),
                    )
                )
                enqueue_webhook(
                    db,
                    org_id=org_id,
                    event="excursion.budget_breached",
                    payload={
                        "shipment_id": shipment.id,
                        "excursion_id": excursion.id,
                        "cumulative_minutes": total,
                        "budget_minutes": profile.max_excursion_minutes,
                    },
                )
        else:
            excursion = _open_excursion(db, shipment.id, reading.device_id)
            if excursion is None:
                excursion = Excursion(
                    org_id=org_id,
                    shipment_id=shipment.id,
                    device_id=reading.device_id,
                    profile_id=profile.id,
                    started_ts_ms=reading.ts_ms,
                    ended_ts_ms=0,
                    peak_mc=reading.temp_mc,
                    trough_mc=reading.temp_mc,
                    status="open",
                )
                db.add(excursion)
                db.flush()
                events.append(
                    ExcursionEvent(
                        kind="opened",
                        excursion_id=excursion.id,
                        shipment_id=str(shipment.id),
                    )
                )
                enqueue_webhook(
                    db,
                    org_id=org_id,
                    event="excursion.opened",
                    payload={
                        "shipment_id": shipment.id,
                        "excursion_id": excursion.id,
                        "device_id": reading.device_id,
                        "temp_mc": reading.temp_mc,
                        "started_ts_ms": excursion.started_ts_ms,
                        "temp_min_mc": profile.temp_min_mc,
                        "temp_max_mc": profile.temp_max_mc,
                    },
                )
            else:
                excursion.peak_mc = max(excursion.peak_mc, reading.temp_mc)
                excursion.trough_mc = min(excursion.trough_mc, reading.temp_mc)
                db.flush()
    db.flush()
    return events


def compute_mkt_mc(temps_mc: list[int]) -> int:
    """Compute USP <1079> Mean Kinetic Temperature, in millidegree Celsius.

    ``MKT_K = (dH/R) / (-ln(mean(exp(-dH/(R*T)))))`` with ``dH = 83144 J/mol``,
    ``R = 8.314 J/mol/K`` and ``T`` in kelvin. The result is converted back to
    millidegree Celsius with round-half-up. Stdlib ``math`` only.
    """
    if not temps_mc:
        raise ValueError("temps_mc must not be empty")
    for temp in temps_mc:
        if temp <= _ABS_ZERO_MC:
            raise ValueError(f"temperature {temp} mc is at or below absolute zero")
    dh_r = _MKT_DH_J_PER_MOL / _MKT_R_J_PER_MOL_K
    total = 0.0
    for temp in temps_mc:
        t_k = temp / 1000.0 + 273.15
        total += math.exp(-dh_r / t_k)
    mean = total / len(temps_mc)
    mkt_k = dh_r / (-math.log(mean))
    mkt_c = mkt_k - 273.15
    return int((Decimal(str(mkt_c)) * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def shipment_excursion_minutes(db: Session, org_id: str, shipment_id: str, now_ms: int) -> int:
    """Total excursion minutes for a shipment (closed full + open up to now)."""
    excursions = _shipment_excursions(db, org_id, shipment_id)
    return int(_excursion_minutes_float(excursions, now_ms))


def shipment_stats(db: Session, org_id: str, shipment_id: str, now_ms: int) -> dict[str, Any]:
    """Aggregate excursion/reading statistics for a shipment.

    Readings are scoped to attached devices inside
    ``[started_at_ms, ended_at_ms or now_ms]``. ``readings_total`` counts
    ``accepted`` + ``superseded`` rows; ``quarantined`` counts ``quarantined``
    rows. MKT/min/max run over ``accepted`` temperatures only.
    """
    shipment = db.get(Shipment, shipment_id)
    if shipment is None or shipment.org_id != org_id:
        raise ExcursionError(f"unknown shipment {shipment_id!r} in org {org_id!r}")
    profile = db.get(TempProfile, shipment.profile_id)
    if profile is None:
        raise ExcursionError(
            f"shipment {shipment_id!r} references missing profile {shipment.profile_id!r}"
        )
    device_ids = list(
        db.scalars(
            select(ShipmentDevice.device_id).where(ShipmentDevice.shipment_id == shipment_id)
        ).all()
    )
    end_ms = shipment.ended_at_ms if shipment.ended_at_ms else now_ms
    in_window: list[Reading] = []
    if device_ids:
        in_window = list(
            db.scalars(
                select(Reading).where(
                    Reading.org_id == org_id,
                    Reading.device_id.in_(device_ids),
                    Reading.ts_ms >= shipment.started_at_ms,
                    Reading.ts_ms <= end_ms,
                )
            ).all()
        )
    accepted_temps = [row.temp_mc for row in in_window if row.status == "accepted"]
    readings_total = sum(1 for row in in_window if row.status in ("accepted", "superseded"))
    quarantined = sum(1 for row in in_window if row.status == "quarantined")
    excursions = _shipment_excursions(db, org_id, shipment_id)
    excursions_open = sum(1 for excursion in excursions if excursion.status == "open")
    excursions_closed = sum(1 for excursion in excursions if excursion.status == "closed")
    minutes = int(_excursion_minutes_float(excursions, now_ms))
    budget = profile.max_excursion_minutes
    return {
        "shipment_id": shipment_id,
        "readings_total": readings_total,
        "quarantined": quarantined,
        "excursions_open": excursions_open,
        "excursions_closed": excursions_closed,
        "excursion_minutes": minutes,
        "budget_minutes": budget,
        "breached": minutes > budget,
        "mkt_mc": compute_mkt_mc(accepted_temps) if accepted_temps else None,
        "temp_min_mc": min(accepted_temps) if accepted_temps else None,
        "temp_max_mc": max(accepted_temps) if accepted_temps else None,
        "mkt_limit_mc": profile.mkt_limit_mc,
    }
