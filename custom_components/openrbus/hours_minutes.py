"""Source-backed conversion for the vendor HoursMinutes wire unit."""

from __future__ import annotations

from datetime import time

# These objects encode positions on a daily clock in ten-minute increments.
# CP820 (3475) uses the same wire unit for a duration, so it is deliberately
# excluded from clock formatting and TimeEntity projection.
_CLOCK_TIME_CODES = frozenset(
    {
        "HP094",
        "HP095",
        "SP069",
        "SP079",
        "SP292",
        "SP293",
        "CP390",
        "DP440",
        "NP080",
        "NP090",
    }
)
MAX_HOURS_MINUTES_VALUE = 143


def is_clock_hours_minutes(register: object) -> bool:
    """Return true only for source-identified daily clock values."""

    return (
        getattr(register, "unit", None) == "HoursMinutes"
        and getattr(register, "datatype", None) == "UINT8"
        and getattr(register, "internal_code", None) in _CLOCK_TIME_CODES
    )


def is_readonly_clock_hours_minutes(register: object) -> bool:
    """Return true when the unit is a clock but not a writable time control."""

    return is_clock_hours_minutes(register) and not bool(
        getattr(register, "writable", False)
    )


def hours_minutes_to_text(value: object) -> str | None:
    """Format a ten-minute index as HH:MM, rejecting invalid wire values."""

    if type(value) is not int or not 0 <= value <= MAX_HOURS_MINUTES_VALUE:
        return None
    hours, ten_minute_ticks = divmod(value, 6)
    return f"{hours:02d}:{ten_minute_ticks * 10:02d}"


def hours_minutes_to_time(value: object) -> time | None:
    """Decode a valid ten-minute index as an HA-native time value."""

    display = hours_minutes_to_text(value)
    if display is None:
        return None
    hours, minutes = (int(part) for part in display.split(":"))
    return time(hours, minutes)


def time_to_hours_minutes(value: object) -> int:
    """Encode an HA time only when exactly representable in ten-minute ticks."""

    if not isinstance(value, time) or value.tzinfo is not None:
        raise ValueError("a timezone-free time value is required")
    if value.second or value.microsecond or value.minute % 10:
        raise ValueError("HoursMinutes values require a ten-minute boundary")
    return value.hour * 6 + value.minute // 10
