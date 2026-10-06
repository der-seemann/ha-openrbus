"""Register-value validity and the invalid-value lifecycle.

The bus uses a few perfectly representable wire values as "not available".
They must not be confused with a failed transport read: initially we retain
the entity and keep polling it, while its HA state is unavailable.  Only a
continuous, configured period of such values retires the projection.
Deterministic object-local unsupported-access and malformed visible-string
failures may also be quarantined for one identity/access/transport epoch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .bridge import GenericRead


def _decimal(value: object) -> Decimal | None:
    """Convert an engineering value without turning floats into broad ranges."""

    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def invalid_value_reason(result: GenericRead, register: object | None) -> str | None:
    """Return a known sentinel reason, or ``None`` for a publishable value.

    Checks are intentionally exact and metadata-scoped.  In particular,
    ``6553.5 %`` is only the documented pump-modulation sentinel, never a
    blanket rule for percentage registers that may legitimately use UINT16.
    """

    value = result.value
    if isinstance(value, float) and not math.isfinite(value):
        return "non_finite"
    decimal_value = _decimal(value)
    if decimal_value is None:
        return None
    unit = str(getattr(register, "unit", "") or "")
    storage = str(getattr(register, "storage", "") or "").upper()
    scale = _decimal(getattr(register, "scale", None))
    semantic = " ".join(
        str(getattr(register, field, "") or "")
        for field in ("internal_code", "name_de", "name_en")
    ).casefold()

    # INT16 0x8000 with a 0.01 engineering gain.  The storage/gain guard
    # prevents valid -327.68 readings on arbitrary temperature definitions
    # from being made unavailable.
    if (
        decimal_value == Decimal("-327.68")
        and unit in {"°C", "C", "°c"}
        and storage == "INT16"
        and scale == Decimal("0.01")
    ):
        return "temperature_not_available"

    # UINT16 0xffff scaled by 0.1 is used by the documented "Modulation
    # Pumpe AE" value.  Both pump and modulation wording are required so a
    # generic percentage/counter value cannot be falsely retired.
    if (
        decimal_value == Decimal("6553.5")
        and unit == "%"
        and storage == "UINT16"
        and scale == Decimal("0.1")
        # German source catalogues use "Pumpe"; English uses "pump".
        and ("pump" in semantic or "pumpe" in semantic)
        and "modulation" in semantic
    ):
        return "pump_modulation_not_available"
    return None


@dataclass(slots=True)
class InvalidValueState:
    """Continuous invalid interval for one stable node/object identity."""

    since: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class ValidityObservation:
    """The outcome used by the polling and entity layers."""

    valid: bool
    reason: str | None = None
    invalid_since: datetime | None = None
    expired: bool = False
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class QuarantinedObject:
    """One deterministic failure scoped to the current device/session."""

    scope: object
    error_class: str
    subtype: str


class RegisterValidityTracker:
    """Track sentinel validity without treating transport errors as sentinels."""

    def __init__(self, invalid_for: timedelta) -> None:
        self.invalid_for = invalid_for
        self._invalid: dict[object, InvalidValueState] = {}
        # Abort and otherwise unclassified item errors have their own timer;
        # they are unavailable poll results, not register sentinel values.
        self._item_errors: dict[object, InvalidValueState] = {}
        self._expired: set[object] = set()
        self._quarantined: dict[object, QuarantinedObject] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def observe(
        self,
        key: object,
        result: GenericRead | Exception,
        register: object | None,
        *,
        now: datetime | None = None,
    ) -> ValidityObservation:
        """Record one read; valid values immediately reset a sentinel timer."""

        now = now or self._now()
        # Transport/object errors are unavailable states too, but are not a
        # known sentinel. Only stable object-level abort/item failures qualify
        # for the separate continuous unsupported-object lifecycle.
        if not isinstance(result, GenericRead):
            error_class = getattr(result, "_openrbus_error_class", None)
            if error_class in {"abort", "item"}:
                state = self._item_errors.get(key)
                if state is None:
                    state = InvalidValueState(since=now, reason=error_class)
                    self._item_errors[key] = state
                expired = now - state.since >= self.invalid_for
                if expired:
                    self._expired.add(key)
                return ValidityObservation(
                    valid=False,
                    reason=state.reason,
                    invalid_since=state.since,
                    expired=expired,
                )
            # A transport, decode, batch, or correlation failure cannot prove
            # that the object stayed unsupported continuously. Break only
            # the object-error interval; sentinel-value continuity remains
            # independently tracked below.
            self._item_errors.pop(key, None)
            self._expired.discard(key)
            state = self._invalid.get(key)
            return ValidityObservation(
                valid=False,
                reason=state.reason if state else None,
                invalid_since=state.since if state else None,
                expired=key in self._expired,
            )
        self._item_errors.pop(key, None)
        self._expired.discard(key)
        reason = invalid_value_reason(result, register)
        if reason is None:
            previous = self._invalid.pop(key, None)
            recovered = previous is not None or key in self._expired
            self._expired.discard(key)
            return ValidityObservation(valid=True, recovered=recovered)
        state = self._invalid.get(key)
        if state is None:
            state = InvalidValueState(since=now, reason=reason)
            self._invalid[key] = state
        expired = now - state.since >= self.invalid_for
        if expired:
            self._expired.add(key)
        return ValidityObservation(False, state.reason, state.since, expired)

    def is_valid(self, key: object, result: object) -> bool:
        """Return whether a current GenericRead is safe to expose to HA."""

        return (
            isinstance(result, GenericRead)
            and key not in self._invalid
            and key not in self._quarantined
        )

    def quarantine_deterministic_failure(
        self, key: object, result: object, scope: object | None
    ) -> bool:
        """Quarantine fixed object-local errors only when scope is known."""

        if scope is None:
            return False
        error_class = getattr(result, "_openrbus_error_class", None)
        abort_category = getattr(result, "_openrbus_abort_category", None)
        decode_subtype = getattr(result, "_openrbus_decode_subtype", None)
        if error_class == "abort" and abort_category in {
            "unsupported_access",
            "read_not_supported",
        }:
            subtype = abort_category
        elif error_class == "decode" and decode_subtype == "visible_string_non_ascii":
            subtype = decode_subtype
        else:
            return False
        try:
            hash(scope)
        except TypeError:
            return False
        self._quarantined[key] = QuarantinedObject(
            scope=scope, error_class=error_class, subtype=subtype
        )
        return True

    def is_quarantined(self, key: object, scope: object | None) -> bool:
        """Return true only while a deterministic failure's scope still holds."""

        state = self._quarantined.get(key)
        if state is None:
            return False
        if scope == state.scope:
            return True
        self._quarantined.pop(key, None)
        # Any scope change starts a new continuity interval and re-probes the
        # object on the next poll.
        self._item_errors.pop(key, None)
        self._invalid.pop(key, None)
        self._expired.discard(key)
        return False

    def quarantined_snapshot(self, limit: int = 16) -> tuple[dict[str, object], ...]:
        """Return bounded, payload-free object-local failure classifications."""

        if limit <= 0:
            return ()
        items: list[dict[str, object]] = []
        for key, state in self._quarantined.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or type(key[0]) is not int
                or not 0 <= key[0] <= 255
                or not hasattr(key[1], "index")
                or not hasattr(key[1], "subindex")
            ):
                continue
            index, subindex = key[1].index, key[1].subindex
            if (
                type(index) is not int
                or not 0 <= index <= 65535
                or type(subindex) is not int
                or not 0 <= subindex <= 255
            ):
                continue
            items.append(
                {
                    "node": key[0],
                    "index": index,
                    "subindex": subindex,
                    "error_class": state.error_class,
                    "subtype": state.subtype,
                }
            )
            if len(items) >= max(0, limit):
                break
        return tuple(items)

    @property
    def quarantined_count(self) -> int:
        """Number of currently retained scoped quarantine records."""

        return len(self._quarantined)

    def is_expired(self, key: object) -> bool:
        return key in self._expired

    def reactivate(self, key: object) -> None:
        """Allow a rediscovery/user reactivation to poll a retired identity."""

        self._expired.discard(key)
        self._invalid.pop(key, None)
        self._item_errors.pop(key, None)
        self._quarantined.pop(key, None)
