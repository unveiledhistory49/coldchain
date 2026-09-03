"""Temperature helpers. All readings are integers in millidegree Celsius — never float."""

from __future__ import annotations

ABS_ZERO_MC = -273150
PHYS_MIN_MC = -100_000
PHYS_MAX_MC = 100_000
MAX_FUTURE_SKEW_MS = 5 * 60 * 1000


class TempError(ValueError):
    pass


def parse_temp_to_mc(value: str | int | float, unit: str) -> int:
    """Parse a temperature into integer millidegree Celsius. Rejects NaN/inf,
    out-of-physical-range values, and unknown units. Float input is rounded
    half-away via Decimal to avoid binary-float artifacts."""
    from decimal import Decimal, InvalidOperation

    u = unit.strip().upper()
    if u not in ("C", "F", "K"):
        raise TempError(f"unknown unit {unit!r}: want C, F, or K")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as e:
        raise TempError(f"invalid temperature {value!r}: {e}") from e
    if not d.is_finite():
        raise TempError(f"invalid temperature {value!r}: not finite")
    if u == "F":
        c = (d - 32) * Decimal(5) / Decimal(9)
    elif u == "K":
        c = d - Decimal("273.15")
    else:
        c = d
    mc = int((c * 1000).to_integral_value(rounding="ROUND_HALF_UP"))
    if mc < PHYS_MIN_MC or mc > PHYS_MAX_MC:
        raise TempError(f"temperature {value!r}{u} outside physical range")
    return mc


def format_mc(mc: int, unit: str = "C") -> str:
    from decimal import Decimal

    u = unit.strip().upper()
    c = Decimal(mc) / 1000
    if u == "F":
        return f"{c * Decimal(9) / Decimal(5) + 32:.2f} F"
    if u == "K":
        return f"{c + Decimal('273.15'):.2f} K"
    return f"{c:.3f} C"
