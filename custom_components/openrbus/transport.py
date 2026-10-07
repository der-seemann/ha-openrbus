"""Native Bluetooth and ESPHome Thin-RPC backends for OpenRBus."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from openrbus.access import ObjectRead
from openrbus.authorization import (
    AuthorizationCorrelationError,
    CanIpGatewayAuthorizer,
    GatewayAuthorizationResult,
    TeaKeyComponent,
)
from openrbus.client import OpenRBusClient, WritePlan
from openrbus.discovery import (
    CapabilityReference,
    DeviceIdentity,
    discover_capabilities,
    discover_devices,
)
from openrbus.errors import (
    CanOpenAbortError,
    ProtocolError,
    RegistryError,
    RequestTimeoutError,
    TransportError,
    ValidationError,
)
from openrbus.object_client import RawObjectClient
from openrbus.protocol.canip import ObjectAddress
from openrbus.registry import Registry
from openrbus.transport import BleakMessageTransport
from openrbus.transport.thin_gatt import (
    GattHandles,
    ThinGattCorrelationError,
    ThinGattFlowControlError,
    ThinGattLink,
    ThinGattMessageTransport,
    ThinGattProfile,
    ThinGattRpcChannel,
    ThinGattRpcServices,
    ThinGattSession,
    ThinGattSessionStateError,
)
from openrbus.value_codec import decode_value

from .bridge import GenericRead
from .const import BACKEND_NATIVE, BACKEND_THIN_RPC
from .proxy_provisioning import check_proxy_compatibility
from .setup_observability import RecoveryFenceMetrics, SetupResponseMetrics

_LOGGER = logging.getLogger(__name__)

_DEFAULT_REQUEST_SERVICE = "openrbus_gatt_rpc_request"
_DEFAULT_POLL_SERVICE = "openrbus_gatt_rpc_poll"
_DEFAULT_BATCH_POLL_SERVICE = "openrbus_gatt_rpc_poll_batch"
_DEFAULT_DIAGNOSTICS_SERVICE = "openrbus_gatt_rpc_diagnostics"
_MAX_BATCH_POLL_FRAMES = 8
_RESPONSE_WRAPPERS = ("response", "service_data", "data", "service_response")
# The firmware pair watchdog is 10 seconds.  Keep the host-side secure-link
# deadline above that boundary so its terminal event can arrive before HA
# cancels the Core operation.  Object reads retain the configured timeout.
_THIN_SECURE_TIMEOUT = 20.0
_THIN_DISCONNECT_TIMEOUT = 5.0
_STARTUP_CLEANUP_TIMEOUT = 8.0
_THIN_CAPABILITY_DISCOVERY_BUDGET = 5.0
_PAIR_ACTION_SUFFIX = "openrbus_pair"
_DISCONNECT_ACTION_SUFFIX = "openrbus_disconnect"
MAX_FRAME_TRACE_ENTRIES = 128
MAX_FRAME_TRACE_BYTES = 24 * 1024
MAX_READ_OPERATION_TRACE_ENTRIES = 32
_TRACE_KINDS = frozenset({"request", "response", "event"})
_TRACE_OPS = frozenset(
    {
        "CAPABILITY",
        "CANCEL",
        "CONNECT",
        "CONNECTION_STATE",
        "DISCONNECT",
        "DISCONNECTED",
        "DISCOVER",
        "ENCRYPTION_STATE",
        "FLOW_CONTROL",
        "HANDLE_LOOKUP",
        "NOTIFICATION",
        "PAIR_ENCRYPT",
        "SUBSCRIBE",
        "WRITE",
        "WRITE_CHAR",
        "WRITE_DESCRIPTOR",
        "READ_CHAR",
        "READ_DESCRIPTOR",
        "SCAN",
        "SCAN_RESULT",
        "SCAN_DONE",
    }
)
_TRACE_STATUSES = frozenset(
    {
        "OK",
        "ACCEPTED",
        "ERROR",
        "WRITE_FAILED",
        "CANCELLED",
        "success",
        "failed",
        "timeout",
        "terminal_success",
        "terminal_error",
        "late_callback",
    }
)
_TRACE_STATES = frozenset(
    {
        "connected",
        "disconnected",
        "discovered",
        "encrypted",
        "failed",
        "success",
        "error",
        "paired",
        "unpaired",
        "idle",
        "ready",
        "active",
        "inactive",
        "received",
        "accepted",
        "timeout",
        "cancelled",
    }
)
_REGISTRY = Registry.load_default()


_SESSION_SUBTYPES = frozenset(
    {
        "not_ready",
        "link_lost",
        "timeout",
        "not_secure",
        "transport",
        "flow_control_queue_full",
        "flow_control_frame_too_large",
        "flow_control_handle_registry_full",
        "flow_control_payload_too_large",
        "flow_control_unknown",
    }
)
_BATCH_SUBTYPES = frozenset({"malformed", "abort", "fallback"})
_ABORT_CATEGORIES = {
    0x06010000: "unsupported_access",
    0x06010001: "read_not_supported",
    0x06010002: "write_not_supported",
    0x06020000: "object_missing",
    0x06090011: "subindex_missing",
    0x06070010: "type_length_mismatch",
}
_VISIBLE_STRING_ERROR_SUBTYPES = frozenset(
    {"visible_string_non_ascii", "visible_string_overlength", "visible_string_other"}
)
_SAFE_BATCH_EXCEPTION_TYPES = (
    (CanOpenAbortError, "canopen_abort"),
    (RequestTimeoutError, "request_timeout"),
    (TransportError, "transport_error"),
    (ProtocolError, "protocol_error"),
    (RegistryError, "registry_error"),
    (ValidationError, "validation_error"),
    (TimeoutError, "timeout"),
    (HomeAssistantError, "home_assistant_error"),
    (ValueError, "value_error"),
    (TypeError, "type_error"),
    # Keep the diagnostic vocabulary finite and payload-free.  These are
    # common failures raised by optional batch implementations and adapters.
    (AssertionError, "assertion_error"),
    (AttributeError, "attribute_error"),
    (IndexError, "index_error"),
    (KeyError, "key_error"),
    (LookupError, "lookup_error"),
    (NotImplementedError, "not_implemented_error"),
    (OSError, "os_error"),
    (RuntimeError, "runtime_error"),
    (UnicodeError, "unicode_error"),
)


def _tag_read_error(
    error: HomeAssistantError,
    error_class: str,
    error_subtype: str | None = None,
    *,
    abort_category: str | None = None,
    decode_subtype: str | None = None,
    batch_exception_type: str | None = None,
) -> HomeAssistantError:
    """Attach a fixed, non-sensitive classification for aggregate diagnostics."""
    if error_class in {"item", "abort", "batch", "decode", "correlation", "session"}:
        error._openrbus_error_class = error_class  # type: ignore[attr-defined]
    allowed = _SESSION_SUBTYPES if error_class == "session" else _BATCH_SUBTYPES
    if error_subtype in allowed:
        error._openrbus_error_subtype = error_subtype  # type: ignore[attr-defined]
    if abort_category in set(_ABORT_CATEGORIES.values()) | {"other_abort"}:
        error._openrbus_abort_category = abort_category  # type: ignore[attr-defined]
    if decode_subtype in _VISIBLE_STRING_ERROR_SUBTYPES:
        error._openrbus_decode_subtype = decode_subtype  # type: ignore[attr-defined]
    if batch_exception_type in _SAFE_BATCH_EXCEPTION_TYPES_VALUES:
        error._openrbus_batch_exception_type = batch_exception_type  # type: ignore[attr-defined]
    return error


_SAFE_BATCH_EXCEPTION_TYPES_VALUES = frozenset(
    value for _error_type, value in _SAFE_BATCH_EXCEPTION_TYPES
) | {"other_error"}


def safe_batch_exception_type(error: BaseException) -> str:
    """Map an exception to an allowlisted label without exposing its message."""
    for error_type, value in _SAFE_BATCH_EXCEPTION_TYPES:
        if isinstance(error, error_type):
            return value
    return "other_error"


_SAFE_RECOVERY_MESSAGES = frozenset(
    {
        "Thin-RPC recovery has no scoped ESPHome disconnect action",
        "Thin-RPC scoped physical disconnect action failed",
        "Thin-RPC physical disconnect did not complete before reload",
        "Thin-RPC pairing service is unavailable",
        "Thin-RPC stale pairing reset/rearm failed",
        "Thin-RPC pairing arm did not reach the disconnect boundary",
        "Thin-RPC backend is not started",
    }
)


def _safe_recovery_error_chain(error: BaseException) -> str:
    """Describe recovery causes using fixed messages and exception classes only."""
    chain: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and len(chain) < 4 and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        safe_message = message if message in _SAFE_RECOVERY_MESSAGES else "redacted"
        chain.append(f"{safe_batch_exception_type(current)}:{safe_message}")
        current = current.__cause__ or current.__context__
    return " <- ".join(chain)


def _abort_category(error: CanOpenAbortError) -> str:
    return _ABORT_CATEGORIES.get(error.code, "other_abort")


def _visible_string_decode_subtype(
    error: BaseException, raw: bytes, expected_length: int
) -> str:
    if len(raw) > expected_length:
        return "visible_string_overlength"
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, UnicodeDecodeError):
            return "visible_string_non_ascii"
        cause = cause.__cause__
    return "visible_string_other"


def _read_error_class(error: BaseException) -> str:
    """Map a read failure to a bounded diagnostic enum without parsing text."""
    tagged = getattr(error, "_openrbus_error_class", None)
    if tagged in {"item", "abort", "batch", "decode", "correlation", "session"}:
        return tagged
    if isinstance(error, ThinGattCorrelationError):
        return "correlation"
    if isinstance(error, ThinGattSessionStateError):
        return "session"
    if isinstance(error, CanOpenAbortError):
        return "abort"
    if isinstance(error, TransportError):
        return "session"
    if isinstance(error, (RegistryError, ValidationError, ValueError)):
        return "decode"
    return "item"


def _read_error_subtype(error: BaseException, error_class: str) -> str | None:
    """Map a failure to a fixed optional subtype without exporting its text."""
    allowed = _SESSION_SUBTYPES if error_class == "session" else _BATCH_SUBTYPES
    tagged = getattr(error, "_openrbus_error_subtype", None)
    if tagged in allowed:
        return tagged
    if error_class != "session":
        return None
    if isinstance(error, (RequestTimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(error, ThinGattFlowControlError):
        reason = error.reason
        if reason in {
            "queue_full",
            "frame_too_large",
            "handle_registry_full",
            "payload_too_large",
        }:
            return f"flow_control_{reason}"
        return "flow_control_unknown"
    if isinstance(error, ThinGattSessionStateError):
        # Core's state exception is a bounded type; its messages currently
        # describe these three fixed states. The message is inspected only to
        # select an enum and is never retained or exported.
        detail = str(error).casefold()
        if "secure" in detail:
            return "not_secure"
        if "disconnect" in detail or "link lost" in detail:
            return "link_lost"
        return "not_ready"
    if isinstance(error, TransportError):
        detail = str(error).casefold()
        if "link lost" in detail or "disconnected" in detail:
            return "link_lost"
        return "transport"
    return "transport"


def _safe_proxy_read_counters(snapshot: object) -> dict[str, Any]:
    """Keep only counters and request/epoch tokens needed for a read join."""
    if not isinstance(snapshot, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in (
        "epoch",
        "epoch_count",
        "rpc_requests",
        "last_rpc_request_id",
        "att_write_char_calls",
        "att_write_char_callbacks",
        "last_att_write_request_id",
        "notification_callbacks",
        "last_notification_epoch",
        "last_notification_seq",
        "last_notification_request_id",
        "event_queue_depth",
        "poll_frame_calls",
        "poll_nonempty_returns",
        "poll_empty_returns",
        "poll_queue_depth_before",
        "poll_queue_depth_after",
        "total_frames_enqueued",
    ):
        value = snapshot.get(key)
        if type(value) is int and 0 <= value <= 4_294_967_295:
            safe[key] = value
    for key in (
        "last_att_write_status",
        "last_rpc_request_op",
    ):
        value = snapshot.get(key)
        if isinstance(value, str) and value in {
            "none",
            "WRITE_CHAR",
            "success",
            "failed",
            "waiting_notification",
        }:
            safe[key] = value
    for key in (
        "host_ready",
        "parent_connected",
        "link_active",
        "last_att_write_waiting_notification",
        "last_notification_nonempty",
        "last_notification_matched_request",
    ):
        value = snapshot.get(key)
        if type(value) is bool:
            safe[key] = value
    return safe


async def _read_objects_batched(
    client: RawObjectClient,
    addresses: Sequence[ObjectAddress],
    *,
    node: int,
    record_batch_event: Callable[[str], None] | None = None,
    record_failure_trace: Callable[[str, BaseException], None] | None = None,
) -> tuple[GenericRead | HomeAssistantError, ...]:
    """Read one node's poll group through Core's size-aware GetList path.

    Polling already groups rows by node in the HA coordinator.  Keep that
    grouping intact at the transport boundary instead of silently turning it
    back into one request per object.  A per-object abort remains an
    object-local Home Assistant error, while a malformed/failed batch keeps
    the existing all-items-error behavior.
    """

    async def read_single(address: ObjectAddress) -> GenericRead | HomeAssistantError:
        """Read one item when a gateway cannot return it in GetList.

        A number of CAN-IP gateways acknowledge GetList but return an abort
        for one or more entries (or a malformed result count).  Treat that as
        a batch capability problem for the affected object and retry through
        the ordinary Core read path.  This keeps a typed entity's state tied
        to the requested ``(node, address)`` instead of making it permanently
        unavailable merely because another object in the batch was rejected.
        """

        try:
            raw = await client.read_raw(node, address)
            value = decode_value(
                _REGISTRY.get(address), address, raw, registry=_REGISTRY
            )
        except (
            CanOpenAbortError,
            ProtocolError,
            RegistryError,
            TransportError,
            ValidationError,
            ValueError,
        ) as error:
            if record_failure_trace is not None:
                record_failure_trace("single_fallback", error)
            error_class = _read_error_class(error)
            definition = _REGISTRY.find(address)
            details: dict[str, str | None] = {}
            if isinstance(error, CanOpenAbortError):
                details["abort_category"] = _abort_category(error)
            if (
                isinstance(error, ValidationError)
                and definition is not None
                and definition.wire.storage.value == "VISIBLESTRING"
            ):
                raw_value = raw if isinstance(locals().get("raw"), bytes) else b""
                details["decode_subtype"] = _visible_string_decode_subtype(
                    error, raw_value, definition.wire.length
                )
            return _tag_read_error(
                HomeAssistantError(str(error)),
                error_class,
                _read_error_subtype(error, error_class),
                **details,
            )
        return GenericRead(node, address, raw, value)

    async def read_singles(
        *, batch_exception_type: str | None = None
    ) -> tuple[GenericRead | HomeAssistantError, ...]:
        if record_batch_event is not None:
            record_batch_event("fallback")
        results: list[GenericRead | HomeAssistantError] = []
        for address in addresses:
            value = await read_single(address)
            if isinstance(value, HomeAssistantError) and batch_exception_type:
                value._openrbus_batch_exception_type = batch_exception_type  # type: ignore[attr-defined]
            results.append(value)
        return tuple(results)

    try:
        items = tuple(
            ObjectRead(node, address, _REGISTRY.get(address).wire.length)
            for address in addresses
        )
        raw_results = await client.read_many_raw(items)
    except (
        CanOpenAbortError,
        ProtocolError,
        RegistryError,
        TransportError,
        ValidationError,
        ValueError,
    ) as error:
        if record_failure_trace is not None:
            record_failure_trace("get_list_call", error)
        # A lost prepared session is not a GetList capability failure.  Use
        # the typed error classification here: Core reports link loss with
        # several stable TransportError messages (for example
        # "disconnected during operation" and "session lost its identity"),
        # none of which need to contain the literal words "link lost".
        # Returning session errors lets ThinRpcBackend fence/reprepare once
        # before retrying only the affected addresses.  CANopen aborts remain
        # on the per-object fallback below.
        error_class = _read_error_class(error)
        if error_class == "session":
            return tuple(
                _tag_read_error(
                    HomeAssistantError(str(error)),
                    error_class,
                    _read_error_subtype(error, error_class),
                )
                for _ in addresses
            )
        if isinstance(error, CanOpenAbortError) and record_batch_event is not None:
            record_batch_event("abort")
        # Function-8/GetList is optional on older or restricted gateways.
        # A failed batch must not make every otherwise readable entity
        # unavailable; retry each object through Core's ordinary validated
        # read path while preserving per-object errors.
        return await read_singles(batch_exception_type=safe_batch_exception_type(error))

    if len(raw_results) != len(addresses):
        if record_failure_trace is not None:
            record_failure_trace("response_parse", ValueError("invalid result count"))
        # Do not turn a malformed/partial batch response into a permanent
        # outage for every typed entity.  Retry each requested object through
        # Core's single-object path and preserve any object-local error.
        if record_batch_event is not None:
            record_batch_event("malformed")
        return await read_singles()

    results: list[GenericRead | HomeAssistantError] = []
    for address, raw_result in zip(addresses, raw_results, strict=True):
        if raw_result.error is not None:
            # Some gateways report GetList unsupported per entry rather than
            # failing the whole request.  Retry only that entry, leaving
            # successful batch results untouched and avoiding duplicate
            # traffic for healthy objects.
            if (
                isinstance(raw_result.error, CanOpenAbortError)
                and record_batch_event is not None
            ):
                record_batch_event("abort")
            if record_batch_event is not None:
                record_batch_event("fallback")
            fallback = await read_single(address)
            results.append(fallback)
            continue
        try:
            definition = _REGISTRY.get(address)
            value = decode_value(
                definition,
                address,
                raw_result.raw,
                registry=_REGISTRY,
            )
        except (RegistryError, ValidationError, ValueError) as error:
            decode_subtype = None
            if (
                isinstance(error, ValidationError)
                and definition.wire.storage.value == "VISIBLESTRING"
                and raw_result.raw is not None
            ):
                decode_subtype = _visible_string_decode_subtype(
                    error, raw_result.raw, definition.wire.length
                )
            results.append(
                _tag_read_error(
                    HomeAssistantError(str(error)),
                    "decode",
                    decode_subtype=decode_subtype,
                )
            )
        else:
            results.append(GenericRead(node, address, raw_result.raw, value))
    return tuple(results)


async def _read_effective_access_levels(
    client: RawObjectClient,
    identities: Sequence[DeviceIdentity],
    *,
    recover_on_transport_error: Callable[[], Any] | None = None,
) -> dict[int, int]:
    """Read the authoritative per-node access level after discovery.

    A Thin-RPC response can be lost while the secure session is still settling
    after discovery. Recover that session at most once and retry the failed
    read once; access remains fail-closed if the retry has no proof.
    """

    levels: dict[int, int] = {}
    recovery_attempted = False
    for identity in identities:
        try:
            raw = await client.read_raw(identity.node, ObjectAddress(0x4002, 0x00))
            if raw:
                levels[identity.node] = max(1, min(3, int(raw[0])))
        except TransportError:
            if recover_on_transport_error is None:
                continue
            try:
                if not recovery_attempted:
                    recovery_attempted = True
                    await recover_on_transport_error()
                raw = await client.read_raw(identity.node, ObjectAddress(0x4002, 0x00))
                if raw:
                    levels[identity.node] = max(1, min(3, int(raw[0])))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - missing proof stays fail-closed
                _LOGGER.debug(
                    "Thin access-level proof unavailable for node %s (%s)",
                    identity.node,
                    type(error).__name__,
                )
                continue
        except Exception:  # noqa: BLE001, S112 - one unavailable node must not abort discovery
            continue
    return levels


async def _discover_capabilities_batched(
    client: RawObjectClient,
    node: int,
    *,
    timeout: float,
    batch_timeout: float | None = None,
) -> tuple[CapabilityReference, ...]:
    """Read a node capability directory with bounded GetList requests.

    Some SCB nodes advertise 80–100 entries. Reading those one at a time can
    hold up config-entry setup for minutes. Core's `read_many_raw` partitions
    the full directory into size-limited GetList calls, while keeping each
    request subject to its ordinary transport timeout. No task cancellation
    is used, so a late response cannot poison the next correlated read.
    """

    count_raw = await client.read_raw(
        node, ObjectAddress(0x5826, 0x00), timeout=timeout
    )
    if not count_raw:
        return ()
    count = count_raw[0]
    if count == 0:
        return ()
    items = tuple(
        ObjectRead(node, ObjectAddress(0x5826, subindex), 4)
        for subindex in range(1, count + 1)
    )
    results = await client.read_many_raw(
        items, timeout=timeout if batch_timeout is None else batch_timeout
    )
    capabilities: list[CapabilityReference] = []
    for item in results:
        raw = item.raw
        if raw is None or len(raw) != 4:
            continue
        flags, index_low, target_subindex, index_high = raw
        capabilities.append(
            CapabilityReference(
                item.address.subindex,
                ObjectAddress((index_high << 8) | index_low, target_subindex),
                flags,
            )
        )
    return tuple(capabilities)


async def _read_thin_access_level(backend: Any, node: int) -> int | None:
    """Read one node's effective access proof with one bounded transport retry.

    The normal read path already performs its single session recovery. A
    follow-up read is useful when the first request still raced gateway
    readiness; it is limited to one extra read and remains fail-closed for
    aborts or any other missing proof.
    """

    address = ObjectAddress(0x4002, 0x00)
    for attempt in range(2):
        try:
            result = await backend.async_read_object(
                address,
                node=node,
                timeout=min(backend.timeout, 3.0),
            )
        except asyncio.CancelledError:
            raise
        except TransportError as error:
            if attempt == 0:
                _LOGGER.debug(
                    "THIN_ACCESS_PROOF node=%s retrying_after=%s",
                    node,
                    type(error).__name__,
                )
                await asyncio.sleep(0.1)
                continue
            _LOGGER.debug(
                "THIN_ACCESS_PROOF node=%s error_type=%s",
                node,
                type(error).__name__,
            )
            return None
        except Exception as error:  # noqa: BLE001 - missing proof remains fail-closed
            _LOGGER.debug(
                "THIN_ACCESS_PROOF node=%s error_type=%s",
                node,
                type(error).__name__,
            )
            return None
        if not result.raw_value:
            _LOGGER.debug("THIN_ACCESS_PROOF node=%s result=empty", node)
            return None
        level = max(1, min(3, int(result.raw_value[0])))
        _LOGGER.debug("THIN_ACCESS_PROOF node=%s level=%s", node, level)
        return level
    return None


def _identity_capability_evidence(
    identity: DeviceIdentity,
) -> tuple[CapabilityReference, ...]:
    """Keep successful identity reads as scoped catalog evidence.

    The internal 0x5826 directory is preferred when a node exposes it, but it
    is optional on deployed buses.  ``discover_devices`` already performed
    read-only, node-scoped identity reads; retaining those observations keeps
    the catalogue useful without falling back to the global registry.
    """

    existing = tuple(getattr(identity, "capabilities", ()) or ())
    seen = {item.address for item in existing}
    evidence: list[CapabilityReference] = list(existing)
    observed = (
        (ObjectAddress(0x2001, 0x02), identity.device_code is not None),
        (ObjectAddress(0x2001, 0x05), identity.parameter_number is not None),
        (ObjectAddress(0x300F, 0x00), identity.name is not None),
    )
    for offset, (address, supported) in enumerate(observed):
        if supported and address not in seen:
            evidence.append(CapabilityReference(0x80 + offset, address, 0))
            seen.add(address)
    return tuple(evidence)


_THIN_SUBSCRIPTIONS = (
    ("identity_notify", "identity_cccd"),
    ("auth_notify", "auth_cccd"),
    ("response", "response_cccd"),
)


class NativeBluetoothBackend:
    """Native Linux/USB Bluetooth backend backed by Core's Bleak adapter.

    HA owns the selected address and lifecycle; Core owns BLE segmentation,
    CAN-IP framing, discovery, registry decoding, and gateway authorization.
    This adapter intentionally has no alternate/fallback transport.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        address: str,
        source: str | None = None,
        passkey: int | None = None,
        access_level: int = 1,
        write_enabled: bool = False,
        key_provider: Callable[[bytes], TeaKeyComponent | bytes] | None = None,
        transport_factory: Callable[[str], Any] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not address:
            raise HomeAssistantError("Native Bluetooth requires a BLE target")
        if not 1 <= int(access_level) <= 3:
            raise HomeAssistantError("OpenRBus access level must be 1, 2, or 3")
        self.hass = hass
        self.address = address
        self.source = source or None
        if passkey is not None and not 0 <= int(passkey) <= 999999:
            raise HomeAssistantError("Native Bluetooth pairing PIN is invalid")
        self.passkey = passkey
        self.access_level = int(access_level)
        self.write_enabled = bool(write_enabled)
        self.key_provider = key_provider
        self.transport_factory = transport_factory or self._default_transport
        self.timeout = timeout
        self.transport: Any | None = None
        self.client: RawObjectClient | None = None
        self.authentication: Any | None = None
        self.devices: tuple[DeviceIdentity, ...] = ()
        self.effective_access_levels: dict[int, int] = {}
        self._started = False
        self._lock = asyncio.Lock()

    def _resolve_adapter(self) -> str:
        """Resolve the BlueZ adapter belonging to the HA Bluetooth source.

        A BLE MAC can be present on more than one local adapter.  Passing the
        MAC to Bleak without its HA scanner source therefore permits a silent
        connection attempt on the wrong controller.  The source persisted by
        the config flow is the HA scanner source (normally the adapter MAC),
        while Bleak needs the scanner's concrete ``hciX`` name.
        """

        from homeassistant.components import bluetooth

        source = self.source
        if source and source.startswith("hci"):
            return source
        info = None
        if source:
            try:
                scanner = bluetooth.async_scanner_by_source(self.hass, source)
            except (AttributeError, RuntimeError, TypeError):
                scanner = None
            adapter = getattr(scanner, "adapter", None) if scanner else None
            if adapter:
                return str(adapter)

        # Legacy entries have no persisted source.  HA's last service info is
        # the only safe migration path because it retains the scanner source
        # associated with the selected target.
        try:
            info = bluetooth.async_last_service_info(
                self.hass, self.address, connectable=True
            )
        except (AttributeError, RuntimeError, TypeError):
            info = None
        if info is not None and getattr(info, "source", None):
            info_source = str(info.source)
            try:
                scanner = bluetooth.async_scanner_by_source(self.hass, info_source)
            except (AttributeError, RuntimeError, TypeError):
                scanner = None
            adapter = getattr(scanner, "adapter", None) if scanner else None
            if adapter:
                return str(adapter)

        raise HomeAssistantError(
            "Native Bluetooth adapter source unavailable for the selected target; "
            "rediscover the device and select it again"
        )

    def _default_transport(self, target: str) -> BleakMessageTransport:
        return BleakMessageTransport(
            target,
            adapter=self._resolve_adapter(),
            pairing_pin=self.passkey,
            gateway_auth=True,
        )

    @property
    def name(self) -> str:
        return BACKEND_NATIVE

    @property
    def started(self) -> bool:
        return self._started

    async def async_start(self) -> None:
        async with self._lock:
            if self._started:
                return
            transport = self.transport_factory(self.address)
            try:
                try:
                    client, authentication = await self._start_transport_once(transport)
                except AuthorizationCorrelationError:
                    # A CRC-valid confirmation from the previous BLE session
                    # can be accepted at a fresh request boundary.  A full
                    # teardown and new transport object fences that callback
                    # and allows exactly one clean authorization retry.
                    await transport.disconnect()
                    transport = self.transport_factory(self.address)
                    client, authentication = await self._start_transport_once(transport)
                self.transport = transport
                self.client = client
                self.authentication = authentication
                self._started = True
            except BaseException as error:
                try:
                    async with asyncio.timeout(_STARTUP_CLEANUP_TIMEOUT):
                        await transport.disconnect()
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve startup error
                    error.add_note(
                        "Native Bluetooth startup disconnect did not complete: "
                        f"{type(cleanup_error).__name__}"
                    )
                    self.transport = transport
                else:
                    self.transport = None
                    self.client = None
                    self.authentication = None
                raise

    async def _start_transport_once(
        self, transport: Any
    ) -> tuple[RawObjectClient, Any]:
        """Connect and authorize once; correlation recovery is owned by caller."""

        await transport.connect()
        if not transport.is_connected:
            raise HomeAssistantError(
                "Native Bluetooth adapter connected without an active BLE link"
            )
        client = RawObjectClient(transport, timeout=self.timeout)
        authentication = None
        if self.access_level >= 2:
            if self.key_provider is None:
                raise HomeAssistantError("Access level 2/3 requires an EHC key secret")
            # Core's direct CAN-IP authorization uses the same externally
            # supplied four-byte material as Thin-RPC.
            material = self.key_provider(b"native-bluetooth")
            if not isinstance(material, (TeaKeyComponent, bytes)):
                raise HomeAssistantError(
                    "EHC key provider returned invalid key material"
                )
            authorizer = CanIpGatewayAuthorizer(
                transport, max_access_level=self.access_level
            )
            authentication = await authorizer.authorize(
                self.access_level, key_component=material, timeout=self.timeout
            )
        return client, authentication

    async def async_stop(self) -> None:
        async with self._lock:
            transport = self.transport
            if transport is not None:
                async with asyncio.timeout(_STARTUP_CLEANUP_TIMEOUT):
                    await transport.disconnect()
            self.transport = None
            self.client = None
            self.authentication = None
            self._started = False

    async def async_discover_devices(self) -> tuple[DeviceIdentity, ...]:
        await self.async_start()
        if self.client is None:
            raise HomeAssistantError("Native Bluetooth backend is not started")
        identities = await discover_devices(self.client, include_serial=False)
        scoped: list[DeviceIdentity] = []
        for identity in identities:
            try:
                capabilities = await discover_capabilities(
                    self.client, identity.node, timeout=self.timeout
                )
            except (CanOpenAbortError, ProtocolError, TransportError, ValueError):
                capabilities = ()
            discovered = replace(identity, capabilities=capabilities)
            scoped.append(
                replace(
                    discovered,
                    capabilities=_identity_capability_evidence(discovered),
                )
            )
        self.devices = tuple(scoped)
        self.effective_access_levels = await _read_effective_access_levels(
            self.client, self.devices
        )
        return self.devices

    async def async_read_object(
        self, address: ObjectAddress, *, node: int = 0xFF, timeout: float | None = None
    ) -> GenericRead:
        await self.async_start()
        if self.client is None:
            raise HomeAssistantError("Native Bluetooth backend is not started")
        raw = await self.client.read_raw(node, address, timeout=timeout)
        definition = _REGISTRY.get(address)
        value = decode_value(definition, address, raw, registry=_REGISTRY)
        return GenericRead(node, address, raw, value)

    async def async_read_objects(
        self, addresses: Sequence[ObjectAddress], *, node: int = 0xFF
    ) -> tuple[GenericRead | HomeAssistantError, ...]:
        await self.async_start()
        if self.client is None:
            raise HomeAssistantError("Native Bluetooth backend is not started")
        return await _read_objects_batched(self.client, addresses, node=node)

    async def async_write_object(
        self,
        node: int,
        address: ObjectAddress,
        value: Any,
        *,
        allow_unsafe: bool = False,
        verify: bool = True,
    ) -> WritePlan:
        if not self.write_enabled:
            raise HomeAssistantError("OpenRBus write access is disabled")
        await self.async_start()
        if self.client is None:
            raise HomeAssistantError("Native Bluetooth backend is not started")
        return await OpenRBusClient(
            self.client,
            enable_writes=True,
            max_access_level=self.access_level,
        ).write(
            node,
            address,
            value,
            allow_unsafe=allow_unsafe,
            verify=verify,
            device_family=None,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "started": self._started,
            "target_configured": bool(self.address),
            "connected": bool(self.transport and self.transport.is_connected),
            "authenticated": self.authentication is not None,
        }


def _thin_attach_mode(requested: bool, firmware_diagnostics: Mapping[str, Any]) -> bool:
    """Use attach framing only when the firmware reports an active GATT link.

    Core accepts a fresh CONNECT baseline at event sequence one, while an
    attach to an already-ready physical link starts at sequence two or later.
    The diagnostics sample is used only to choose that framing; it is never
    treated as proof of encryption or gateway authorization.
    """

    return requested and firmware_diagnostics.get("link_active") is True


@dataclass(frozen=True, slots=True)
class ThinRpcCapability:
    """Service capability sampled at the coordinator lifecycle boundary."""

    request_service: str
    poll_service: str
    diagnostics_service: str
    missing: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "request_available": "request" not in self.missing,
            "poll_available": "poll" not in self.missing,
            "diagnostics_available": "diagnostics" not in self.missing,
        }


def select_backend_mode(
    configured_backend: str | None, capability: ThinRpcCapability
) -> str:
    """Select one of the two supported transports without fallback."""

    if configured_backend == BACKEND_NATIVE:
        return BACKEND_NATIVE
    if configured_backend == BACKEND_THIN_RPC:
        return BACKEND_THIN_RPC
    raise HomeAssistantError("Unknown OpenRBus transport; reconfigure this entry")


def _service_names(hass: HomeAssistant) -> Mapping[str, Any]:
    return hass.services.async_services().get("esphome", {})


def _has_esphome_service(hass: HomeAssistant, service: str) -> bool:
    """Check a service without depending on a particular registry facade."""
    has_service = getattr(hass.services, "has_service", None)
    if callable(has_service):
        return bool(has_service("esphome", service))
    return service in _service_names(hass)


class _PairingArmBoundaryTimeout(HomeAssistantError):
    """The pair action returned, but no physical disconnect boundary followed."""


def detect_thin_rpc_capability(
    hass: HomeAssistant,
    *,
    request_service: str = _DEFAULT_REQUEST_SERVICE,
    poll_service: str = _DEFAULT_POLL_SERVICE,
    diagnostics_service: str = _DEFAULT_DIAGNOSTICS_SERVICE,
    preferred_prefix: str | None = None,
) -> ThinRpcCapability:
    """Detect the complete three-service capability without touching hardware."""

    available = _service_names(hass)
    required = (
        ("request", request_service),
        ("poll", poll_service),
        ("diagnostics", diagnostics_service),
    )
    candidates = {role: _matching_services(available, name) for role, name in required}
    missing = tuple(role for role, values in candidates.items() if not values)
    if not missing:
        prefixes = {
            role: {_service_prefix(value, name) for value in values}
            for (role, name), values in zip(required, candidates.values(), strict=True)
        }
        common = set.intersection(*(values for values in prefixes.values()))
        if preferred_prefix is not None:
            if preferred_prefix in common:
                common = {preferred_prefix}
            elif common == {""}:
                # Bare service names carry no controller identity; accept the
                # sole unprefixed trio because no competing route exists.
                common = {""}
            else:
                return ThinRpcCapability(
                    request_service,
                    poll_service,
                    diagnostics_service,
                    ("preferred_controller",),
                )
        elif len(common) != 1:
            missing = ("same_controller",)
            return ThinRpcCapability(
                request_service, poll_service, diagnostics_service, missing
            )
        prefix = min(common)
        configured = dict(required)
        resolved = {
            role: next(
                value
                for value in values
                if _service_prefix(value, configured[role]) == prefix
            )
            for role, values in candidates.items()
        }
        return ThinRpcCapability(
            resolved["request"],
            resolved["poll"],
            resolved["diagnostics"],
        )
    return ThinRpcCapability(
        request_service, poll_service, diagnostics_service, missing
    )


def thin_rpc_controller_choices(
    hass: HomeAssistant,
    *,
    request_service: str = _DEFAULT_REQUEST_SERVICE,
    poll_service: str = _DEFAULT_POLL_SERVICE,
    diagnostics_service: str = _DEFAULT_DIAGNOSTICS_SERVICE,
) -> dict[str, ThinRpcCapability]:
    """Return complete, same-prefix service trios available to config flow.

    The empty prefix is intentionally represented by ``"default"`` as an
    opaque UI key. It is only a route selector; diagnostics never expose it.
    """
    available = _service_names(hass)
    required = (
        ("request", request_service),
        ("poll", poll_service),
        ("diagnostics", diagnostics_service),
    )
    candidates = {role: _matching_services(available, name) for role, name in required}
    if any(not values for values in candidates.values()):
        return {}
    prefixes = {
        role: {_service_prefix(value, name) for value in values}
        for (role, name), values in zip(required, candidates.values(), strict=True)
    }
    common = set.intersection(*(values for values in prefixes.values()))
    choices: dict[str, ThinRpcCapability] = {}
    for prefix in sorted(common):
        resolved = {
            role: next(
                value
                for value in values
                if _service_prefix(value, dict(required)[role]) == prefix
            )
            for role, values in candidates.items()
        }
        choices[prefix or "default"] = ThinRpcCapability(
            resolved["request"], resolved["poll"], resolved["diagnostics"]
        )
    return choices


def resolve_thin_rpc_capability(
    hass: HomeAssistant,
    *,
    request_service: str,
    poll_service: str,
    diagnostics_service: str,
) -> ThinRpcCapability:
    """Validate one persisted exact trio without rediscovering another route."""
    available = _service_names(hass)
    names = {
        "request": request_service,
        "poll": poll_service,
        "diagnostics": diagnostics_service,
    }
    missing = tuple(role for role, name in names.items() if name not in available)
    if missing:
        return ThinRpcCapability(
            request_service, poll_service, diagnostics_service, missing
        )
    prefixes = {_service_prefix(name, name) for name in names.values()}
    if len(prefixes) != 1:
        return ThinRpcCapability(
            request_service,
            poll_service,
            diagnostics_service,
            ("same_controller",),
        )
    return ThinRpcCapability(request_service, poll_service, diagnostics_service)


def _matching_services(
    available: Mapping[str, Any], configured: str
) -> tuple[str, ...]:
    return tuple(
        sorted(
            name
            for name in available
            if name == configured or name.endswith(f"_{configured}")
        )
    )


def _service_prefix(service: str, suffix: str) -> str:
    # Config entries may persist a resolved, prefixed service name. Derive
    # identity from the canonical OpenRBus suffix rather than treating each
    # exact configured string as an unrelated bare route.
    marker_suffix = suffix.split("_openrbus_", 1)[-1]
    marker_suffix = marker_suffix.removeprefix("openrbus_")
    marker = f"_openrbus_{marker_suffix}"
    if service.endswith(marker):
        return service[: -len(marker)]
    return ""


def controller_prefix(value: str | None) -> str | None:
    """Derive an ESPHome service prefix from a configured entity/device."""

    if not value:
        return None
    name = value.rsplit(".", 1)[-1]
    if name == "openrbus_thin_rpc_response":
        return None
    suffix = "_openrbus_read_raw_response"
    name = name.removesuffix(suffix)
    return name or None


class HomeAssistantNativeApiClient:
    """Small Native API facade backed by HA's ESPHome service registry."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def execute_service(
        self,
        service: str,
        data: Mapping[str, Any],
        *,
        return_response: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """Invoke an ESPHome service using HA's response-capable service API."""

        kwargs: dict[str, Any] = {"blocking": True}
        if return_response:
            kwargs["return_response"] = True
        if timeout is not None:
            # HA service calls do not universally accept a timeout argument;
            # the Core adapter's surrounding wait_for supplies the deadline.
            del timeout
        return await self.hass.services.async_call(
            "esphome", service, dict(data), **kwargs
        )


class HomeAssistantThinGattChannel:
    """Translate Core Thin-RPC channel calls into ESPHome HA services.

    This adapter intentionally does not parse or construct CAN-IP or BLE
    segments.  Those responsibilities stay in ``ThinGattSession`` and
    ``ThinGattMessageTransport`` from Core.
    """

    def __init__(
        self,
        client: HomeAssistantNativeApiClient,
        capability: ThinRpcCapability,
        frame_trace: list[dict[str, Any]] | None = None,
        target_address: str | None = None,
        target_address_type: int | None = None,
        setup_metrics: SetupResponseMetrics | None = None,
    ) -> None:
        self.client = client
        self.capability = capability
        self.frame_trace = frame_trace
        self.trace_context: str | None = None
        self.target_address = target_address
        self.target_address_type = target_address_type
        self.setup_metrics = setup_metrics
        self._setup_handle_lookup_request_ids: set[int] = set()
        self.effective_access_levels: dict[int, int] = {}
        self._frame_trace_bytes = 0
        self.trace_operation_id: int | None = None
        self._batch_poll_service = (
            capability.poll_service.removesuffix("_poll") + "_poll_batch"
        )
        self._batch_poll_frames: deque[Mapping[str, Any]] = deque()

    def _record_frame(self, direction: str, frame: Mapping[str, Any]) -> None:
        """Record only bounded, categorical correlation evidence when enabled."""

        if self.frame_trace is None or len(self.frame_trace) >= MAX_FRAME_TRACE_ENTRIES:
            return
        try:
            op = frame.get("op")
            status = frame.get("status")
            payload = frame.get("payload")
            state = payload.get("state") if isinstance(payload, Mapping) else None
            record = {
                "direction": direction
                if direction in {"request", "response"}
                else "other",
                "kind": (
                    frame.get("kind") if frame.get("kind") in _TRACE_KINDS else "other"
                ),
                "op": op if op in _TRACE_OPS else "other",
                "seq": frame.get("seq") if type(frame.get("seq")) is int else None,
                "epoch": frame.get("epoch")
                if type(frame.get("epoch")) is int
                else None,
                "request_id_present": type(frame.get("request_id")) is int,
                "request_id": (
                    frame.get("request_id")
                    if type(frame.get("request_id")) is int
                    and 0 <= frame["request_id"] <= 4_294_967_295
                    else None
                ),
                "context": (
                    self.trace_context if self.trace_context in {"discovery"} else None
                ),
                "status": status if status in _TRACE_STATUSES else None,
                "state_category": state if state in _TRACE_STATES else None,
            }
            if (
                type(self.trace_operation_id) is int
                and 1 <= self.trace_operation_id <= 4_294_967_295
            ):
                record["read_operation_id"] = self.trace_operation_id
            encoded_size = (
                len(
                    json.dumps(
                        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                    )
                )
                + 1
            )
            if self._frame_trace_bytes + encoded_size > MAX_FRAME_TRACE_BYTES:
                return
            self.frame_trace.append(record)
            self._frame_trace_bytes += encoded_size
        except Exception:  # noqa: BLE001 - diagnostics must not mask transport errors
            # Diagnostics must never alter the primary transport result.
            return

    async def action(
        self, name: str, payload: Mapping[str, Any], *, timeout: float
    ) -> None:
        if payload.get("op") in {"CONNECT", "DISCONNECT"}:
            # A bootstrap boundary retires any frames already buffered from
            # the previous physical/session epoch.
            self._batch_poll_frames.clear()
        if not name:
            raise ValueError("Thin-RPC request service name is required")
        op = payload.get("op")
        request_id = payload.get("request_id", 0)
        if (
            not isinstance(op, str)
            or type(request_id) is not int
            or request_id < 0
            or (op != "DISCONNECT" and request_id <= 0)
        ):
            raise ValueError("invalid Thin-RPC action correlation")
        frame = {
            "v": 1,
            "kind": "request",
            "op": op,
            "epoch": payload.get("epoch", 0),
            "request_id": request_id,
            "gattc_if": payload.get("gattc_if", 0),
            "conn_id": payload.get("conn_id", 0),
            "payload": {
                key: value
                for key, value in payload.items()
                if key not in {"op", "epoch", "request_id", "gattc_if", "conn_id"}
            },
        }
        if op == "CONNECT" and self.target_address:
            frame["payload"]["target_address"] = self.target_address
            if self.target_address_type is not None:
                frame["payload"]["target_address_type"] = self.target_address_type
        self._record_frame("request", frame)
        observed = self.setup_metrics if op == "HANDLE_LOOKUP" else None
        started = asyncio.get_running_loop().time() if observed is not None else 0.0
        if observed is not None and not _has_esphome_service(self.client.hass, name):
            observed.record("handle_lookup", "call_not_sent", 0.0)
            raise TransportError("Thin-RPC request service is unavailable")
        try:
            await asyncio.wait_for(
                self.client.execute_service(
                    name,
                    {
                        "request_id": request_id,
                        "frame": json.dumps(frame, separators=(",", ":")),
                    },
                    timeout=timeout,
                ),
                timeout,
            )
        except asyncio.CancelledError:
            if observed is not None:
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                observed.record("handle_lookup", "cancelled_before_response", elapsed)
            raise
        except TimeoutError as exc:
            if observed is not None:
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                observed.record("handle_lookup", "timeout_no_response", elapsed)
            raise TransportError("Thin-RPC request service failed") from exc
        except Exception as exc:
            if observed is not None:
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                observed.record("handle_lookup", "error", elapsed)
            raise TransportError("Thin-RPC request service failed") from exc
        if observed is not None:
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            observed.record("handle_lookup", "call_completed", elapsed)
            if len(self._setup_handle_lookup_request_ids) < 8:
                self._setup_handle_lookup_request_ids.add(request_id)

    async def poll(self, *, timeout: float) -> Mapping[str, Any] | None:
        if self._batch_poll_frames:
            frame = self._batch_poll_frames.popleft()
            self._record_frame("response", frame)
            if frame.get("op") == "NOTIFICATION":
                self._decode_notification(frame)  # type: ignore[arg-type]
            return frame

        observed = self.setup_metrics
        started = asyncio.get_running_loop().time() if observed is not None else 0.0
        hass = getattr(self.client, "hass", None)
        use_batch_poll = hass is not None and _has_esphome_service(
            hass, self._batch_poll_service
        )
        selected_poll_service = (
            self._batch_poll_service if use_batch_poll else self.capability.poll_service
        )
        if (
            observed is not None
            and hass is not None
            and not _has_esphome_service(hass, selected_poll_service)
        ):
            observed.record("poll_request", "call_not_sent", 0.0)
            raise TransportError("Thin-RPC poll service is unavailable")
        try:
            response = await self.client.execute_service(
                selected_poll_service,
                {},
                return_response=True,
                timeout=timeout,
            )
        except asyncio.CancelledError:
            if observed is not None:
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                observed.record("poll_request", "cancelled_before_response", elapsed)
            raise
        except TimeoutError:
            if observed is not None:
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                observed.record("poll_request", "timeout_no_response", elapsed)
            return None
        except Exception as exc:
            if observed is not None:
                elapsed = (asyncio.get_running_loop().time() - started) * 1000
                observed.record("poll_request", "error", elapsed)
            raise TransportError("Thin-RPC poll service failed") from exc
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        if isinstance(response, Mapping) and response.get("success") is False:
            raise TransportError("Thin-RPC poll service failed")
        if (
            not isinstance(response, Mapping)
            and getattr(response, "success", True) is False
        ):
            raise TransportError("Thin-RPC poll service failed")
        try:
            if use_batch_poll:
                body = self._response_object(response, "frames")
                frame_texts = body["frames"]
                if (
                    not isinstance(frame_texts, list)
                    or len(frame_texts) > _MAX_BATCH_POLL_FRAMES
                ):
                    raise ValueError("invalid batch length")
                frames = [self._decode_json_value(item) for item in frame_texts]
                if any(not isinstance(frame, dict) for frame in frames):
                    raise ValueError("batch frame is not an object")
                self._batch_poll_frames.extend(frames)
                frame = (
                    self._batch_poll_frames.popleft()
                    if self._batch_poll_frames
                    else None
                )
            else:
                body = self._response_object(response, "frame")
                frame_text = body["frame"]
                frame = self._decode_json_value(frame_text)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if observed is not None:
                observed.record("poll_request", "error", elapsed)
            raise TransportError("invalid Thin-RPC poll response") from exc
        if frame is None:
            if observed is not None:
                observed.record("poll_request", "no_esp_response", elapsed)
                if self._setup_handle_lookup_request_ids:
                    observed.record("handle_lookup", "no_esp_response", elapsed)
            return None
        if not isinstance(frame, dict):
            if observed is not None:
                observed.record("poll_request", "error", elapsed)
            raise TransportError("Thin-RPC poll frame is not an object")
        if observed is not None:
            observed.record("poll_request", "esp_response", elapsed)
        self._record_frame("response", frame)
        if (
            observed is not None
            and frame.get("op") == "HANDLE_LOOKUP"
            and frame.get("kind") == "response"
            and type(frame.get("request_id")) is int
            and frame["request_id"] in self._setup_handle_lookup_request_ids
        ):
            self._setup_handle_lookup_request_ids.discard(frame["request_id"])
            observed.mark_handle_lookup_response()
            observed.record("handle_lookup", "response", elapsed)
        if frame.get("op") == "NOTIFICATION":
            self._decode_notification(frame)
        return frame

    async def diagnostics(self) -> Mapping[str, Any]:
        try:
            response = await self.client.execute_service(
                self.capability.diagnostics_service, {}, return_response=True
            )
            body = self._response_object(response, "snapshot")
            snapshot = self._decode_json_value(body["snapshot"])
        except Exception as exc:
            raise TransportError("invalid Thin-RPC diagnostics response") from exc
        if not isinstance(snapshot, dict):
            raise TransportError("Thin-RPC diagnostics snapshot is not an object")
        return snapshot

    @staticmethod
    def _response_object(response: Any, field: str) -> Mapping[str, Any]:
        """Extract a strict response object from HA/native response shapes."""

        value: Any = response
        if not isinstance(value, Mapping) and not isinstance(value, (bytes, bytearray)):
            value = getattr(value, "response_data", None)
        if isinstance(value, (bytes, bytearray)):
            value = json.loads(bytes(value))
        if not isinstance(value, Mapping):
            raise TransportError("Native API response envelope is invalid")
        if value.get("success") is False:
            raise TransportError("Native API response was unsuccessful")
        wrappers = [key for key in _RESPONSE_WRAPPERS if key in value]
        if field in value:
            if wrappers:
                raise TransportError("Native API response envelope is ambiguous")
            return value
        if len(wrappers) > 1:
            raise TransportError("Native API response envelope is ambiguous")
        if not wrappers:
            raise TransportError(f"Native API response {field} is missing")
        nested = value[wrappers[0]]
        if not isinstance(nested, Mapping):
            raise TransportError("Native API response envelope is invalid")
        if nested.get("success") is False:
            raise TransportError("Native API response was unsuccessful")
        if any(key in nested for key in _RESPONSE_WRAPPERS):
            raise TransportError("Native API response envelope is ambiguous")
        if field not in nested:
            raise TransportError(f"Native API response {field} is missing")
        return nested

    @staticmethod
    def _decode_json_value(value: Any) -> Any:
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            return None if not raw.strip() else json.loads(raw)
        if isinstance(value, str):
            return None if not value.strip() else json.loads(value)
        if isinstance(value, Mapping):
            return dict(value)
        if value is None:
            return None
        raise TypeError("Native API JSON value is invalid")

    @staticmethod
    def _decode_notification(frame: dict[str, Any]) -> None:
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            raise TransportError("Thin-RPC notification payload is invalid")
        value = payload.get("value")
        if not isinstance(value, str):
            raise TransportError("Thin-RPC notification value is invalid")
        try:
            payload["value"] = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise TransportError(
                "Thin-RPC notification value is invalid base64"
            ) from exc
        handle = frame.get("handle", payload.get("handle"))
        if type(handle) is not int or handle < 0:
            raise TransportError("Thin-RPC notification handle is invalid")
        frame["handle"] = handle


class _PreparedThinGattMessageTransport(ThinGattMessageTransport):
    """Prevent Core's convenience reconnect from bypassing HA lifecycle."""

    async def connect(self) -> None:
        raise TransportError(
            "Thin-RPC link lost; the backend must reprepare before the next read"
        )


async def async_scan_thin_rpc_devices(
    hass: HomeAssistant,
    capability: ThinRpcCapability,
    *,
    duration: float = 5.0,
) -> tuple[dict[str, Any], ...]:
    """Run the transport-only remote BLE scan before a GATT target is chosen."""

    channel = HomeAssistantThinGattChannel(
        HomeAssistantNativeApiClient(hass), capability
    )
    # SCAN is intentionally session-independent: the ESP server consumes the
    # already-running tracker callback before any target GATT CONNECT exists.
    # Zero routing metadata is part of this contract; CONNECT and all later
    # GATT operations remain target/session-specific.
    request_id = 1
    await channel.action(
        capability.request_service,
        {
            "op": "SCAN",
            "request_id": request_id,
            "epoch": 0,
            "gattc_if": 0,
            "conn_id": 0,
            "duration_ms": int(duration * 1000),
        },
        timeout=5.0,
    )
    results: dict[str, dict[str, Any]] = {}
    deadline = asyncio.get_running_loop().time() + max(10.0, duration + 5.0)
    while asyncio.get_running_loop().time() < deadline:
        frame = await channel.poll(timeout=1.0)
        if frame is None:
            await asyncio.sleep(0.05)
            continue
        # A previous GATT lifecycle may leave one terminal connection event
        # queued in the shared ESPHome poll service.  It is unrelated to this
        # session-independent scan and must not abort the user flow before
        # the correlated SCAN response arrives.
        if frame.get("op") not in {"SCAN", "SCAN_RESULT", "SCAN_DONE"}:
            continue
        _validate_scan_frame(frame, request_id)
        if frame.get("op") == "SCAN_RESULT":
            payload = frame.get("payload")
            if isinstance(payload, Mapping) and isinstance(payload.get("address"), str):
                results[payload["address"]] = dict(payload)
        elif frame.get("op") == "SCAN_DONE":
            return tuple(
                sorted(results.values(), key=lambda item: str(item.get("address")))
            )
        elif frame.get("kind") == "response" and frame.get("status") == "ERROR":
            raise HomeAssistantError(
                f"Remote BLE scan failed: {frame.get('error', 'unknown')}"
            )
    raise HomeAssistantError("Remote BLE scan timed out")


def _validate_scan_frame(frame: Mapping[str, Any], request_id: int) -> None:
    """Validate a session-independent scan control envelope.

    SCAN runs before a target GATT connection exists.  Its zero identity is
    therefore meaningful and must not be sent through the normal
    ``ThinGattSession`` identity fence.  Correlation is still strict: only
    the active scan request may produce SCAN/RESULT/DONE frames.
    """

    if not isinstance(frame, Mapping):
        raise ThinGattCorrelationError("malformed Thin-RPC scan frame")
    op = frame.get("op")
    if op not in {"SCAN", "SCAN_RESULT", "SCAN_DONE"}:
        # Non-scan traffic is not valid input to this transport-only parser.
        raise ThinGattCorrelationError("unexpected Thin-RPC scan frame")
    if (
        type(frame.get("request_id")) is not int
        or frame.get("request_id") != request_id
    ):
        raise ThinGattCorrelationError("stale or mismatched Thin-RPC scan request")
    # The canonical ESP scan server uses the transport's no-session sentinel
    # (255) for ``gattc_if`` in responses, while the request envelope carries
    # zero.  Accept both representations; any real active interface remains
    # rejected so scan frames cannot cross session boundaries.
    if type(frame.get("epoch")) is not int or frame["epoch"] != 0:
        raise ThinGattCorrelationError("session identity on scan frame is not zero")
    if type(frame.get("gattc_if")) is not int or frame["gattc_if"] not in {0, 255}:
        raise ThinGattCorrelationError("session identity on scan frame is not zero")
    if type(frame.get("conn_id")) is not int or frame["conn_id"] != 0:
        raise ThinGattCorrelationError("session identity on scan frame is not zero")
    if frame.get("kind") not in {"response", "event"}:
        raise ThinGattCorrelationError("invalid Thin-RPC scan frame kind")
    if op == "SCAN" and frame.get("kind") != "response":
        raise ThinGattCorrelationError("invalid Thin-RPC SCAN response")
    if op in {"SCAN_RESULT", "SCAN_DONE"} and frame.get("kind") != "event":
        raise ThinGattCorrelationError("invalid Thin-RPC scan event")


class ThinRpcBackend:
    """Stable read backend for one configured HA controller session."""

    _active_controllers: ClassVar[set[str]] = set()
    _controller_owner_generations: ClassVar[dict[str, int]] = {}
    _next_owner_generation: ClassVar[int] = 1

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        controller_id: str,
        request_service: str = _DEFAULT_REQUEST_SERVICE,
        poll_service: str = _DEFAULT_POLL_SERVICE,
        diagnostics_service: str = _DEFAULT_DIAGNOSTICS_SERVICE,
        pair_action: str | None = None,
        passkey: int | None = None,
        access_level: int = 1,
        write_enabled: bool = False,
        preferred_prefix: str | None = None,
        request_handle: int | None = None,
        response_handle: int | None = None,
        key_provider: Callable[[bytes], TeaKeyComponent | bytes] | None = None,
        profile: ThinGattProfile | None = None,
        roles: Mapping[str, tuple[str, ...]] | None = None,
        link_factory: Callable[[ThinGattSession], ThinGattLink] | None = None,
        authenticator_factory: Callable[[ThinGattLink], Any] | None = None,
        capability: ThinRpcCapability | None = None,
        channel: ThinGattRpcChannel | None = None,
        frame_trace: list[dict[str, Any]] | None = None,
        target_address: str | None = None,
        target_address_type: int | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.hass = hass
        self.controller_id = controller_id
        self.request_service = request_service
        self.poll_service = poll_service
        self.diagnostics_service = diagnostics_service
        self.pair_action = pair_action
        self.passkey = passkey
        if not 1 <= int(access_level) <= 3:
            raise HomeAssistantError("OpenRBus access level must be 1, 2, or 3")
        self.access_level = int(access_level)
        self.write_enabled = bool(write_enabled)
        self.request_handle = request_handle
        self.response_handle = response_handle
        # Key material is deliberately a provider, never a value retained by
        # HA. Core consumes it for the current challenge and stores only a
        # proof bound to the current physical identity and epoch.
        self.key_provider = key_provider
        self.profile = profile
        self.roles = dict(roles) if roles is not None else None
        self.link_factory = link_factory
        self.authenticator_factory = authenticator_factory
        self.timeout = timeout
        effective_prefix = (
            preferred_prefix
            if preferred_prefix is not None
            else controller_prefix(controller_id)
        )
        self.capability = capability or detect_thin_rpc_capability(
            hass,
            request_service=request_service,
            poll_service=poll_service,
            diagnostics_service=diagnostics_service,
            preferred_prefix=effective_prefix,
        )
        self.channel = channel
        self.setup_metrics = SetupResponseMetrics()
        self._recovery_fence_metrics = RecoveryFenceMetrics()
        self._batch_event_counts = dict.fromkeys(("malformed", "abort", "fallback"), 0)
        self.frame_trace = frame_trace
        self.target_address = target_address
        self.target_address_type = target_address_type
        self.session: ThinGattSession | None = None
        self._session_generation = 0
        self.link: ThinGattLink | None = None
        self.authentication: GatewayAuthorizationResult | Any | None = None
        self.transport: ThinGattMessageTransport | None = None
        self.client: RawObjectClient | None = None
        self._diagnostics: Mapping[str, Any] = {}
        self._next_read_operation_id = 0
        self._read_operation_trace: list[dict[str, Any]] = []
        self._last_read_transport_capture: dict[str, Any] | None = None
        self._batch_failure_trace_captured = False
        self._last_batch_failure_trace: dict[str, Any] | None = None
        self._started = False
        self._owns_controller = False
        self._owner_generation = self._next_owner_generation
        type(self)._next_owner_generation += 1
        self._last_disconnect_snapshot: dict[str, bool | int | None] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._read_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return BACKEND_THIN_RPC

    @property
    def started(self) -> bool:
        return self._started

    async def async_start(self) -> None:
        """Start once at lifecycle boundary; capability is never reselected."""

        async with self._lifecycle_lock:
            if self._started:
                return
            # Counters deliberately aggregate over this backend object's
            # lifetime. Keep startup-attempt count beside them so repeated
            # failed setup attempts cannot be mistaken for one setup epoch.
            self.setup_metrics.begin_attempt()
            if not self.capability.available and self.channel is None:
                missing = ", ".join(self.capability.missing)
                raise HomeAssistantError(
                    f"Thin-RPC backend is unavailable; missing ESPHome service(s): {missing}"
                )
            if self.profile is None and self.authenticator_factory is None:
                raise HomeAssistantError(
                    "Thin-RPC requires a configured Thin-GATT profile"
                )
            if (
                self.access_level >= 2
                and self.key_provider is None
                and self.authenticator_factory is None
            ):
                raise HomeAssistantError("Thin-RPC requires an EHC key provider")
            if (
                self.controller_id in self._active_controllers
                and not self._owns_controller
            ):
                existing_owner = self._controller_owner_generations.get(
                    self.controller_id
                )
                _LOGGER.warning(
                    "THIN_OWNER event=claim_rejected claimant_generation=%d "
                    "existing_generation=%s",
                    self._owner_generation,
                    existing_owner,
                )
                raise HomeAssistantError(
                    "Thin-RPC controller is already owned by another entry"
                )
            if not self._owns_controller:
                self._active_controllers.add(self.controller_id)
                self._controller_owner_generations[self.controller_id] = (
                    self._owner_generation
                )
                self._owns_controller = True
                _LOGGER.debug(
                    "THIN_OWNER event=claim_acquired generation=%d",
                    self._owner_generation,
                )
            else:
                _LOGGER.debug(
                    "THIN_OWNER event=claim_reused generation=%d",
                    self._owner_generation,
                )
            setup_started = asyncio.get_running_loop().time()
            try:
                if self.channel is None:
                    self.channel = HomeAssistantThinGattChannel(
                        HomeAssistantNativeApiClient(self.hass),
                        self.capability,
                        frame_trace=self.frame_trace,
                        target_address=self.target_address,
                        target_address_type=self.target_address_type,
                        setup_metrics=self.setup_metrics,
                    )
                elif hasattr(self.channel, "setup_metrics"):
                    self.channel.setup_metrics = self.setup_metrics
                already_secure = await self._arm_pairing_if_configured()
                if already_secure:
                    # A previous physical disconnect boundary can remain in
                    # the ESP event queue when the bonded target reconnects
                    # automatically.  It is stale before this new CONNECT;
                    # drain only that bounded pre-session queue.
                    await self._drain_stale_frames()
                try:
                    await self._establish_session(attach=True)
                except (
                    AuthorizationCorrelationError,
                    ThinGattCorrelationError,
                    ThinGattSessionStateError,
                ):
                    # A freshly created HA session can still observe one
                    # CRC-valid Function-3 notification from the previous
                    # physical link.  Fence that link completely, then allow
                    # exactly one fresh stream-baseline attempt.  Never
                    # reinterpret the stale response or weaken correlation.
                    with contextlib.suppress(Exception):
                        if self.link is not None and self.link.is_connected:
                            await self.link.disconnect(timeout=_THIN_DISCONNECT_TIMEOUT)
                        else:
                            await self._force_disconnect_current_session()
                    await self._wait_for_physical_disconnect()
                    await self._drain_stale_frames()
                    # The ESP stream sequence is reset by the physical
                    # CONNECT boundary, but a bonded proxy may retain one
                    # disconnect marker with a non-zero sequence.  The
                    # second, freshly fenced session may therefore use the
                    # attach baseline rule; identity/epoch correlation still
                    # remains strict and no frame is reinterpreted.
                    await self._establish_session(attach=True)
                self._started = True
            except asyncio.CancelledError as error:
                elapsed = (asyncio.get_running_loop().time() - setup_started) * 1000
                self.setup_metrics.record_setup_cancellation(elapsed)
                try:
                    async with asyncio.timeout(_STARTUP_CLEANUP_TIMEOUT):
                        await self._cleanup_owned_session()
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation while reporting cleanup failure
                    error.add_note(
                        "Thin-RPC startup cleanup did not prove physical disconnect: "
                        f"{type(cleanup_error).__name__}; controller ownership retained"
                    )
                raise
            except Exception as error:
                try:
                    async with asyncio.timeout(_STARTUP_CLEANUP_TIMEOUT):
                        await self._cleanup_owned_session()
                except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation while reporting cleanup failure
                    error.add_note(
                        "Thin-RPC startup cleanup did not prove physical disconnect: "
                        f"{type(cleanup_error).__name__}; controller ownership retained"
                    )
                raise

    async def _cleanup_owned_session(self) -> None:
        """Release a claimed controller only after the physical link is down."""

        if not self._owns_controller:
            return
        existing_owner = self._controller_owner_generations.get(self.controller_id)
        if existing_owner not in (None, self._owner_generation):
            raise HomeAssistantError(
                "Thin-RPC controller ownership changed before cleanup"
            )
        channel = getattr(self, "channel", None)
        link = getattr(self, "link", None)
        session = getattr(self, "session", None)
        if channel is None and (
            session is not None or getattr(link, "is_connected", False)
        ):
            raise HomeAssistantError(
                "Thin-RPC cannot prove physical disconnect without its channel"
            )
        disconnect_error: BaseException | None = None
        if link is not None and getattr(link, "is_connected", False):
            try:
                await link.disconnect(timeout=min(5.0, self.timeout))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - physical state is checked below
                disconnect_error = error
        elif session is not None:
            try:
                await self._force_disconnect_current_session()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - physical state is checked below
                disconnect_error = error
        # DISCONNECT acknowledgement alone is not the safety boundary. Keep
        # ownership until the proxy confirms both physical connection flags
        # are down. Some ESPHome BLE clients acknowledge the Thin-RPC command
        # before their asynchronous BLE disconnect callback runs. If that
        # physical boundary does not arrive, use the exact scoped proxy action
        # as one safe fallback, then require the same two down flags.
        try:
            await self._wait_for_physical_disconnect()
        except asyncio.CancelledError:
            raise
        except HomeAssistantError as physical_error:
            disconnect_action = self._configured_disconnect_action()
            if disconnect_action is None or not _has_esphome_service(
                self.hass, disconnect_action
            ):
                if disconnect_error is not None:
                    physical_error.add_note(
                        "Thin-RPC DISCONNECT request failed: "
                        f"{type(disconnect_error).__name__}"
                    )
                raise
            _LOGGER.debug(
                "THIN_OWNER event=scoped_disconnect_fallback generation=%d",
                self._owner_generation,
            )
            try:
                await self.hass.services.async_call(
                    "esphome", disconnect_action, {}, blocking=True
                )
            except asyncio.CancelledError:
                raise
            except Exception as fallback_error:  # noqa: BLE001 - state remains authoritative
                physical_error.add_note(
                    "Scoped ESPHome disconnect action failed: "
                    f"{type(fallback_error).__name__}"
                )
            try:
                await self._wait_for_physical_disconnect()
            except asyncio.CancelledError:
                raise
            except HomeAssistantError as final_error:
                if disconnect_error is not None:
                    final_error.add_note(
                        "Thin-RPC DISCONNECT request failed: "
                        f"{type(disconnect_error).__name__}"
                    )
                final_error.add_note(
                    "Scoped ESPHome disconnect fallback did not prove physical disconnect"
                )
                raise
        if session is not None:
            retire = getattr(session, "retire", None)
            if callable(retire):
                retire()
        self._started = False
        self.transport = None
        self.client = None
        self.session = None
        self.link = None
        self.authentication = None
        self._active_controllers.discard(self.controller_id)
        self._controller_owner_generations.pop(self.controller_id, None)
        self._owns_controller = False
        _LOGGER.debug(
            "THIN_OWNER event=claim_released generation=%d",
            self._owner_generation,
        )

    async def _force_disconnect_current_session(self) -> bool:
        """Fence a partially prepared session using its observed identity."""

        if self._current_disconnect_guard_outcome() is not None:
            return False
        identity = self.session.identity
        epoch = self.session.epoch
        await self.channel.action(
            self.capability.request_service,
            {
                "op": "DISCONNECT",
                "request_id": 0,
                "epoch": epoch,
                "gattc_if": identity.gattc_if,
                "conn_id": identity.conn_id,
            },
            timeout=_THIN_DISCONNECT_TIMEOUT,
        )
        self.session.retire()
        return True

    def _current_disconnect_guard_outcome(self) -> str | None:
        """Return the fixed reason the current session cannot be fenced."""

        if getattr(self, "channel", None) is None:
            return "missing_channel"
        session = getattr(self, "session", None)
        if session is None:
            return "missing_session"
        if getattr(session, "identity", None) is None:
            return "missing_identity"
        epoch = getattr(session, "epoch", None)
        if type(epoch) is not int or epoch <= 0:
            return "invalid_epoch"
        return None

    async def _arm_pairing_if_configured(self) -> bool:
        """Arm the existing ESPHome pairing state machine before first connect.

        The Thin-RPC PAIR_ENCRYPT operation proves the link and correlates its
        terminal event; the established ESPHome wrapper still owns the PIN
        callback.  Arming that wrapper is therefore part of the normal setup
        boundary, not a second transport path.
        """
        if not self.pair_action:
            return False
        if self.passkey is None or not 0 <= self.passkey <= 999999:
            raise HomeAssistantError("Thin-RPC pairing PIN is invalid")
        started = asyncio.get_running_loop().time()
        if not _has_esphome_service(self.hass, self.pair_action):
            self.setup_metrics.record("pairing_arm", "call_not_sent", 0.0)
            raise HomeAssistantError("Thin-RPC pairing service is unavailable")
        try:
            await self.hass.services.async_call(
                "esphome",
                self.pair_action,
                {"passkey": self.passkey},
                blocking=True,
            )
        except asyncio.CancelledError:
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            self.setup_metrics.record(
                "pairing_arm", "cancelled_before_response", elapsed
            )
            raise
        except TimeoutError:
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            self.setup_metrics.record("pairing_arm", "timeout_no_response", elapsed)
            raise
        except Exception:
            elapsed = (asyncio.get_running_loop().time() - started) * 1000
            self.setup_metrics.record("pairing_arm", "error", elapsed)
            raise
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        self.setup_metrics.record("pairing_arm", "response", elapsed)
        if self.channel is None:
            return False
        try:
            await self._wait_for_pairing_arm_boundary()
        except _PairingArmBoundaryTimeout:
            # The deployed ESPHome wrapper can retain pair_state=1 and
            # pairing_armed=true after an interrupted Thin-RPC setup.  Its
            # ordinary pair action rejects another arm in that state.  The
            # sibling disconnect action clears the armed PIN and sets the
            # terminal state; after the physical down boundary, the deployed
            # pair action safely clears terminal state 5 and accepts one new
            # arm.  Attempt this normal-action recovery once, and only after
            # the specific missing-boundary timeout.
            if not self.pair_action.endswith(_PAIR_ACTION_SUFFIX):
                raise
            disconnect_action = (
                self.pair_action[: -len(_PAIR_ACTION_SUFFIX)]
                + _DISCONNECT_ACTION_SUFFIX
            )
            if not _has_esphome_service(self.hass, disconnect_action):
                raise
            recovery_started = asyncio.get_running_loop().time()
            try:
                await self.hass.services.async_call(
                    "esphome", disconnect_action, {}, blocking=True
                )
                await self._wait_for_physical_disconnect()
                await self._drain_stale_frames()
                await self.hass.services.async_call(
                    "esphome",
                    self.pair_action,
                    {"passkey": self.passkey},
                    blocking=True,
                )
                await self._wait_for_pairing_arm_boundary()
                await self._drain_stale_frames()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elapsed = (asyncio.get_running_loop().time() - recovery_started) * 1000
                self.setup_metrics.record("pairing_arm", "recovery_failed", elapsed)
                raise HomeAssistantError(
                    "Thin-RPC stale pairing reset/rearm failed"
                ) from exc
            elapsed = (asyncio.get_running_loop().time() - recovery_started) * 1000
            self.setup_metrics.record("pairing_arm", "recovery_succeeded", elapsed)
            return False
        await self._drain_stale_frames()
        return False

    async def _wait_for_pairing_arm_boundary(self) -> None:
        """Wait for this arm's disconnect; stale RPC terminals are not proof."""

        deadline = asyncio.get_running_loop().time() + _THIN_DISCONNECT_TIMEOUT
        while True:
            snapshot = await self.channel.diagnostics()
            if (
                snapshot.get("link_active") is False
                and snapshot.get("parent_connected") is False
            ):
                return
            # The RPC already-secure terminal belongs to PAIR_ENCRYPT, which
            # is issued only after this pairing-arm boundary. It cannot prove
            # that the ESPHome pairing action just completed: diagnostics may
            # still contain that terminal from an earlier session. Keep the
            # arm boundary physical and current to this operation.
            if asyncio.get_running_loop().time() >= deadline:
                raise _PairingArmBoundaryTimeout(
                    "Thin-RPC pairing arm did not reach the disconnect boundary"
                )
            await asyncio.sleep(0.05)

    async def async_stop(self) -> None:
        # Read operations take the read lock before the lifecycle lock.  Stop
        # follows the same order so it cannot retire a session mid-request.
        async with self._read_lock, self._lifecycle_lock:
            await self._cleanup_owned_session()

    async def _wait_for_physical_disconnect(
        self, *, recovery_fence: RecoveryFenceMetrics | None = None
    ) -> None:
        """Fence reload until the ESP reports that the BLE link is down."""

        channel = getattr(self, "channel", None)
        if channel is None:
            return
        deadline = asyncio.get_running_loop().time() + _THIN_DISCONNECT_TIMEOUT
        while True:
            snapshot = await channel.diagnostics()
            epoch = snapshot.get("epoch")
            self._last_disconnect_snapshot = {
                "link_active": (
                    snapshot.get("link_active")
                    if type(snapshot.get("link_active")) is bool
                    else None
                ),
                "parent_connected": (
                    snapshot.get("parent_connected")
                    if type(snapshot.get("parent_connected")) is bool
                    else None
                ),
                "epoch": (
                    epoch
                    if type(epoch) is int and 0 <= epoch <= 2_147_483_647
                    else None
                ),
            }
            if recovery_fence is not None:
                recovery_fence.record_state(
                    snapshot.get("link_active"), snapshot.get("parent_connected")
                )
            if (
                snapshot.get("link_active") is False
                and snapshot.get("parent_connected") is False
            ):
                return
            if asyncio.get_running_loop().time() >= deadline:
                if recovery_fence is not None:
                    recovery_fence.record_timeout()
                raise HomeAssistantError(
                    "Thin-RPC physical disconnect did not complete before reload"
                )
            await asyncio.sleep(0.05)

    async def _drain_stale_frames(self) -> None:
        """Discard bounded RPC frames left by the retired physical link."""

        if self.channel is None:
            return
        empty_polls = 0
        for _ in range(32):
            try:
                frame = await self.channel.poll(timeout=0.2)
            except Exception:  # noqa: BLE001 - stale polling must never mask teardown
                return
            if frame is None:
                # BLE callbacks can enqueue the disconnect boundary just
                # after the queue briefly appears empty.  Require a short
                # quiet window before opening the next session boundary.
                empty_polls += 1
                if empty_polls >= 3:
                    return
                await asyncio.sleep(0.05)
                continue
            empty_polls = 0

    async def async_read_object(
        self,
        address: ObjectAddress,
        *,
        node: int = 0xFF,
        timeout: float | None = None,
        recover_on_transport_error: bool = True,
    ) -> GenericRead:
        async with self._read_lock:
            await self._ensure_session_ready()
            operation_id = self._begin_read_operation("single")
            outcome = "error"
            try:
                try:
                    if timeout is None:
                        raw = await self.client.read_raw(node, address)
                    else:
                        raw = await self.client.read_raw(node, address, timeout=timeout)
                except TransportError as transport_error:
                    if not recover_on_transport_error:
                        raise
                    # A read is idempotent. If the secure link disappears
                    # after the readiness check, fence and retry this one
                    # object once on a fully re-prepared session.
                    try:
                        await self._recover_after_transport_loss()
                    except asyncio.CancelledError:
                        raise
                    except Exception as recovery_error:  # noqa: BLE001
                        transport_error.add_note(
                            f"Thin-RPC recovery failed: {type(recovery_error).__name__}"
                        )
                        raise transport_error
                    if timeout is None:
                        raw = await self.client.read_raw(node, address)
                    else:
                        raw = await self.client.read_raw(node, address, timeout=timeout)
                definition = _REGISTRY.get(address)
                value = decode_value(definition, address, raw, registry=_REGISTRY)
                outcome = "success"
                return GenericRead(node, address, raw, value)
            finally:
                self._end_read_operation(operation_id, "single", outcome)

    async def async_discover_devices(self) -> tuple[DeviceIdentity, ...]:
        """Run Core's read-only bus discovery through the prepared Thin link."""

        async with self._read_lock:
            await self.async_start()
            if self.client is None:
                raise HomeAssistantError("Thin-RPC backend is not started")
            trace_channel = getattr(self, "channel", None)
            if isinstance(trace_channel, HomeAssistantThinGattChannel):
                trace_channel.trace_context = "discovery"
            try:
                identities = await discover_devices(self.client, include_serial=False)
            except TransportError as transport_error:
                # Bus discovery is read-only and idempotent. Use the same
                # single-generation recovery as ordinary reads when a
                # request (notably the initial 1f85:00 assignment bound)
                # loses its Thin-GATT response, then repeat discovery once.
                # Protocol/abort errors are not retried or reinterpreted.
                try:
                    await self._recover_after_transport_loss()
                except asyncio.CancelledError:
                    raise
                except Exception as recovery_error:  # noqa: BLE001
                    transport_error.add_note(
                        f"Thin-RPC recovery failed: {type(recovery_error).__name__}"
                    )
                    raise transport_error
                identities = await discover_devices(self.client, include_serial=False)
            finally:
                if isinstance(trace_channel, HomeAssistantThinGattChannel):
                    trace_channel.trace_context = None
            scoped: list[DeviceIdentity] = []
            capability_recovery_attempted = False
            capability_session_ready = True
            for identity in identities:
                capabilities: tuple[CapabilityReference, ...] = ()
                if not capability_session_ready:
                    scoped.append(
                        replace(
                            identity,
                            capabilities=_identity_capability_evidence(identity),
                        )
                    )
                    continue
                try:
                    capabilities = await _discover_capabilities_batched(
                        self.client,
                        identity.node,
                        timeout=min(self.timeout, _THIN_CAPABILITY_DISCOVERY_BUDGET),
                        batch_timeout=self.timeout,
                    )
                except (
                    CanOpenAbortError,
                    ProtocolError,
                    TimeoutError,
                    ValueError,
                ) as error:
                    # A node remains visible even when its optional capability
                    # directory cannot be read.  An empty directory is
                    # intentionally conservative: catalog_for_node() must not
                    # substitute the global registry in that case.
                    _LOGGER.debug(
                        "THIN_CAPABILITIES node=%s result=error type=%s",
                        identity.node,
                        type(error).__name__,
                    )
                except TransportError:
                    # A transport error retires this session. Recover once and
                    # retry the idempotent capability read before probing the
                    # next node. If the replacement also fails, do not issue
                    # more directory requests on an unproven session.
                    if capability_recovery_attempted:
                        capability_session_ready = False
                    else:
                        capability_recovery_attempted = True
                        try:
                            await self._recover_after_transport_loss()
                        except asyncio.CancelledError:
                            raise
                        except Exception as recovery_error:  # noqa: BLE001 - keep discovery fail-safe
                            _LOGGER.debug(
                                "THIN_CAPABILITIES node=%s recovery_error_type=%s",
                                identity.node,
                                type(recovery_error).__name__,
                            )
                            capability_session_ready = False
                        else:
                            try:
                                capabilities = await _discover_capabilities_batched(
                                    self.client,
                                    identity.node,
                                    timeout=min(
                                        self.timeout,
                                        _THIN_CAPABILITY_DISCOVERY_BUDGET,
                                    ),
                                    batch_timeout=self.timeout,
                                )
                            except asyncio.CancelledError:
                                raise
                            except (
                                CanOpenAbortError,
                                ProtocolError,
                                TransportError,
                                TimeoutError,
                                ValueError,
                            ) as retry_error:
                                _LOGGER.debug(
                                    "THIN_CAPABILITIES node=%s retry_error_type=%s",
                                    identity.node,
                                    type(retry_error).__name__,
                                )
                                capability_session_ready = not isinstance(
                                    retry_error, TransportError
                                )
                    if not capability_session_ready:
                        _LOGGER.debug(
                            "THIN_CAPABILITIES node=%s session_unavailable=true",
                            identity.node,
                        )
                else:
                    _LOGGER.debug(
                        "THIN_CAPABILITIES node=%s count=%s",
                        identity.node,
                        len(capabilities),
                    )
                discovered = replace(identity, capabilities=capabilities)
                scoped.append(
                    replace(
                        discovered,
                        capabilities=_identity_capability_evidence(discovered),
                    )
                )
        # Access proof is read after releasing the discovery lock.  Use the
        # normal session-ready single-read path so a capability timeout cannot
        # leave this proof pass on a retired Thin session.
        levels: dict[int, int] = {}
        for identity in scoped:
            level = await _read_thin_access_level(self, identity.node)
            if level is not None:
                levels[identity.node] = level
        self.effective_access_levels = levels
        return tuple(scoped)

    async def async_read_objects(
        self,
        addresses: Sequence[ObjectAddress],
        *,
        node: int = 0xFF,
        trace_failure: bool = False,
    ) -> tuple[GenericRead | HomeAssistantError, ...]:
        async with self._read_lock:
            await self._ensure_session_ready()
            capture_failure = trace_failure and not self._batch_failure_trace_captured
            before: Mapping[str, Any] = {}
            if capture_failure and isinstance(
                self.channel, HomeAssistantThinGattChannel
            ):
                try:
                    before = await self.channel.diagnostics()
                except Exception:  # noqa: BLE001 - diagnostics cannot mask polling
                    before = {}
            operation_id = self._begin_read_operation("batch")
            outcome = "error"
            failure: dict[str, str] = {}

            def record_failure(stage: str, error: BaseException) -> None:
                if stage in {
                    "get_list_call",
                    "response_parse",
                    "single_fallback",
                    "recovery_dispatch",
                }:
                    failure["stage"] = stage
                    failure["exception_class"] = safe_batch_exception_type(error)

            try:
                values = await _read_objects_batched(
                    self.client,
                    addresses,
                    node=node,
                    record_batch_event=self._record_batch_event,
                    record_failure_trace=record_failure if capture_failure else None,
                )
                # A transport/session failure can occur after the initial
                # readiness check. Retry only those object-local failures once
                # after Core/Thin-RPC re-prepares the secure session; protocol,
                # decode, and unsupported-object errors remain object-local.
                failed = tuple(
                    address
                    for address, value in zip(addresses, values, strict=True)
                    if isinstance(value, HomeAssistantError)
                    and (
                        _read_error_class(value) == "session"
                        or "link lost" in str(value).casefold()
                    )
                )
                if failed:
                    # Retry only after a complete session boundary; preserve
                    # successful results from the first pass.
                    try:
                        await self._recover_after_transport_loss()
                    except Exception as error:
                        record_failure("recovery_dispatch", error)
                        raise
                    recovered = await _read_objects_batched(
                        self.client, failed, node=node
                    )
                    recovered_by_address = dict(zip(failed, recovered, strict=True))
                    values = tuple(
                        recovered_by_address.get(address, value)
                        for address, value in zip(addresses, values, strict=True)
                    )
                outcome = (
                    "partial_error"
                    if any(isinstance(value, HomeAssistantError) for value in values)
                    else "success"
                )
                if capture_failure and failure and outcome != "success":
                    await self._finish_batch_failure_trace(
                        operation_id, failure, before
                    )
                return values
            except Exception as error:
                if capture_failure:
                    if not failure:
                        record_failure("get_list_call", error)
                    await self._finish_batch_failure_trace(
                        operation_id, failure, before
                    )
                raise
            finally:
                self._end_read_operation(operation_id, "batch", outcome)

    async def _finish_batch_failure_trace(
        self,
        operation_id: int,
        failure: Mapping[str, str],
        before: Mapping[str, Any],
    ) -> None:
        """Store one bounded, payload-free fast-poll failure attribution."""
        if self._batch_failure_trace_captured or not failure:
            return
        self._batch_failure_trace_captured = True
        if isinstance(self.channel, HomeAssistantThinGattChannel):
            self.channel.trace_operation_id = None
        try:
            after = await self.channel.diagnostics() if self.channel is not None else {}
        except Exception:  # noqa: BLE001 - diagnostics must not mask poll outcome
            after = {}
        frames = getattr(self, "frame_trace", None) or ()
        correlated = [
            item
            for item in frames
            if isinstance(item, dict) and item.get("read_operation_id") == operation_id
        ]
        last = correlated[-1] if correlated else {}
        record: dict[str, Any] = {
            "operation_id": operation_id,
            "stage": failure.get("stage"),
            "exception_class": failure.get("exception_class"),
            "before": _safe_proxy_read_counters(before),
            "after": _safe_proxy_read_counters(after),
        }
        for key in ("request_id", "epoch"):
            value = last.get(key)
            if type(value) is not int:
                # The diagnostic frame ring stops accepting rows when its
                # fixed capacity is full.  Under the read lock the proxy's
                # last request token still belongs to this operation window.
                value = (
                    after.get("last_rpc_request_id")
                    if key == "request_id"
                    else after.get("epoch")
                )
            if type(value) is int and 0 <= value <= 4_294_967_295:
                record[key] = value
        self._last_batch_failure_trace = record

    def _begin_read_operation(self, kind: str) -> int:
        """Tag the serialized bus operation without retaining its address."""
        current_id = getattr(self, "_next_read_operation_id", 0)
        self._next_read_operation_id = (
            1 if current_id >= 4_294_967_295 else current_id + 1
        )
        operation_id = self._next_read_operation_id
        if isinstance(getattr(self, "channel", None), HomeAssistantThinGattChannel):
            self.channel.trace_operation_id = operation_id
        return operation_id

    def _end_read_operation(self, operation_id: int, kind: str, outcome: str) -> None:
        """Close one bounded trace interval and release the channel tag."""
        if (
            isinstance(getattr(self, "channel", None), HomeAssistantThinGattChannel)
            and self.channel.trace_operation_id == operation_id
        ):
            self.channel.trace_operation_id = None
        epoch = getattr(getattr(self, "session", None), "epoch", None)
        record: dict[str, Any] = {
            "operation_id": operation_id,
            "kind": kind if kind in {"single", "batch"} else "other",
            "outcome": outcome
            if outcome in {"success", "partial_error", "error"}
            else "error",
        }
        if type(epoch) is int and 0 <= epoch <= 4_294_967_295:
            record["epoch"] = epoch
        trace = getattr(self, "_read_operation_trace", None)
        if not isinstance(trace, list):
            trace = []
            self._read_operation_trace = trace
        trace.append(record)
        del trace[:-MAX_READ_OPERATION_TRACE_ENTRIES]

    async def async_read_object_with_transport_capture(
        self, address: ObjectAddress, *, node: int = 0xFF
    ) -> GenericRead:
        """Read one object and bracket its transport with proxy counters.

        This opt-in path is for a single read-only diagnostic probe. Holding
        the backend read lock through the post-snapshot prevents another bus
        read from replacing the proxy's last GATT callback/notification IDs.
        """
        async with self._read_lock:
            await self._ensure_session_ready()
            if not isinstance(self.channel, HomeAssistantThinGattChannel):
                raise HomeAssistantError("Thin-RPC diagnostic capture is unavailable")
            before = await self.channel.diagnostics()
            operation_id = self._begin_read_operation("single")
            outcome = "error"
            try:
                try:
                    raw = await self.client.read_raw(node, address)
                except TransportError as transport_error:
                    try:
                        await self._recover_after_transport_loss()
                    except asyncio.CancelledError:
                        raise
                    except Exception as recovery_error:  # noqa: BLE001
                        transport_error.add_note(
                            f"Thin-RPC recovery failed: {type(recovery_error).__name__}"
                        )
                        raise transport_error
                    raw = await self.client.read_raw(node, address)
                definition = _REGISTRY.get(address)
                value = decode_value(definition, address, raw, registry=_REGISTRY)
                outcome = "success"
                return GenericRead(node, address, raw, value)
            finally:
                self._end_read_operation(operation_id, "single", outcome)
                try:
                    after = await self.channel.diagnostics()
                except Exception:  # noqa: BLE001 - capture never changes read result
                    after = {}
                self._last_read_transport_capture = {
                    "operation_id": operation_id,
                    "outcome": outcome,
                    "before": _safe_proxy_read_counters(before),
                    "after": _safe_proxy_read_counters(after),
                }

    async def _recover_after_transport_loss(self) -> None:
        """Fence one failed transport generation and prepare exactly one replacement."""

        # The disconnect callback can lag behind an object-local failure.
        # Retire locally even when the best-effort remote fence cannot be
        # delivered, so no old identity, handles, or authorization proof can
        # be reused by the replacement session.
        fence_metrics = getattr(self, "_recovery_fence_metrics", None)
        session_epoch = getattr(getattr(self, "session", None), "epoch", None)
        if fence_metrics is not None:
            fence_metrics.begin_attempt(session_epoch)
        dispatch_acknowledged = False
        guard_outcome: str | None = None
        try:
            guard_outcome = self._current_disconnect_guard_outcome()
            if guard_outcome is None:
                dispatch_acknowledged = (
                    await self._force_disconnect_current_session()
                ) is True
            if fence_metrics is not None:
                fence_metrics.record_disconnect_outcome(
                    guard_outcome
                    or (
                        "service_completed"
                        if dispatch_acknowledged
                        else "service_not_acknowledged"
                    )
                )
        except Exception as error:  # noqa: BLE001 - physical state remains authoritative
            dispatch_acknowledged = False
            if fence_metrics is not None:
                fence_metrics.record_disconnect_outcome("service_exception", error)
        if fence_metrics is not None:
            fence_metrics.record_dispatch(dispatch_acknowledged)
        session = getattr(self, "session", None)
        identity = getattr(session, "identity", None)
        epoch = getattr(session, "epoch", None)
        expected_identity_present = identity is not None
        expected_epoch_valid = type(epoch) is int and epoch > 0
        stage = "scoped_disconnect"
        if self.session is not None:
            retire = getattr(self.session, "retire", None)
            if callable(retire):
                retire()
        # ESPHome acknowledges DISCONNECT before its asynchronous BLE callback
        # marks the physical link down.  Starting CONNECT as soon as that API
        # action returns can therefore attach the replacement session to the
        # old link; its delayed disconnect event then invalidates the new
        # session.  Use the same physical boundary and bounded queue drain as
        # the controlled lifecycle paths before opening the new epoch.
        try:
            if guard_outcome in {
                "missing_identity",
                "invalid_epoch",
                "missing_session",
            }:
                # With no usable identity, a Thin DISCONNECT cannot name the
                # physical connection. Use only the sibling action of the exact
                # configured pairing action; its proxy template targets the same
                # BLE client and clears the stale pairing arm before disconnect.
                stage = "scoped_disconnect"
                disconnect_action = self._configured_disconnect_action()
                if disconnect_action is None or not _has_esphome_service(
                    self.hass, disconnect_action
                ):
                    raise HomeAssistantError(
                        "Thin-RPC recovery has no scoped ESPHome disconnect action"
                    )
                try:
                    await self.hass.services.async_call(
                        "esphome", disconnect_action, {}, blocking=True
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise HomeAssistantError(
                        "Thin-RPC scoped physical disconnect action failed"
                    ) from error
                if fence_metrics is not None:
                    fence_metrics.record_dispatch(True)
            stage = "physical_disconnect"
            await self._wait_for_physical_disconnect(recovery_fence=fence_metrics)
            stage = "stale_frame_drain"
            await self._drain_stale_frames()
            # openrbus_disconnect clears the proxy's passkey and armed flag. The
            # ordinary pair action re-establishes that state and independently
            # confirms its own physical disconnect boundary before CONNECT.
            if guard_outcome in {
                "missing_identity",
                "invalid_epoch",
                "missing_session",
            }:
                stage = "pairing_rearm"
                await self._arm_pairing_if_configured()
            stage = "session_prepare"
            await self._ensure_session_ready()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            observed = getattr(self, "_last_disconnect_snapshot", {})
            _LOGGER.debug(
                "THIN_RECOVERY event=failed stage=%s guard=%s "
                "expected_identity_present=%s expected_epoch_valid=%s "
                "observed_link=%s observed_parent=%s observed_epoch=%s "
                "error_chain=%s",
                stage,
                guard_outcome or "identity_fenced",
                expected_identity_present,
                expected_epoch_valid,
                observed.get("link_active"),
                observed.get("parent_connected"),
                observed.get("epoch"),
                _safe_recovery_error_chain(error),
            )
            raise

    def _configured_disconnect_action(self) -> str | None:
        """Return only the disconnect sibling of the configured pair action."""

        pair_action = getattr(self, "pair_action", None)
        capability = getattr(self, "capability", None)
        request_service = getattr(capability, "request_service", None)
        if (
            not isinstance(pair_action, str)
            or not pair_action.endswith(_PAIR_ACTION_SUFFIX)
            or not isinstance(request_service, str)
        ):
            return None
        pair_scope = _service_prefix(pair_action, _PAIR_ACTION_SUFFIX)
        request_scope = _service_prefix(request_service, "_openrbus_gatt_rpc_request")
        # Bare service names carry no device identity. A fallback is safe
        # only when both configured services have the same explicit ESPHome
        # prefix, which binds the disconnect action to this RPC controller.
        if not pair_scope or pair_scope != request_scope:
            return None
        return pair_action[: -len(_PAIR_ACTION_SUFFIX)] + _DISCONNECT_ACTION_SUFFIX

    async def _ensure_session_ready(self) -> None:
        """Start or reprepare the Thin-RPC session before a read."""

        await self.async_start()
        if self.client is None:
            raise HomeAssistantError("Thin-RPC backend is not started")
        if self.session is not None and self.session.connected:
            return
        try:
            await self._establish_session(attach=True)
        except Exception as error:
            try:
                async with self._lifecycle_lock:
                    await self._cleanup_owned_session()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve cancellation while reporting cleanup failure
                error.add_note(
                    "Thin-RPC reprepare cleanup did not prove physical disconnect: "
                    f"{type(cleanup_error).__name__}; controller ownership retained"
                )
            raise

    async def async_write_object(
        self,
        node: int,
        address: ObjectAddress,
        value: Any,
        *,
        allow_unsafe: bool = False,
        verify: bool = True,
    ) -> WritePlan:
        if not self.write_enabled:
            raise HomeAssistantError("OpenRBus write access is disabled")
        async with self._read_lock:
            # Writes deliberately share the same recovered-session gate as
            # reads, but are never automatically retried: a lost response can
            # mean the device already applied the first write.  The caller
            # receives that failure and can inspect/read back before deciding
            # whether another explicit write is appropriate.
            await self._ensure_session_ready()
            if self.client is None:
                raise HomeAssistantError("Thin-RPC backend is not started")
            return await OpenRBusClient(
                self.client,
                enable_writes=True,
                max_access_level=self.access_level,
            ).write(
                node,
                address,
                value,
                allow_unsafe=allow_unsafe,
                verify=verify,
                device_family=None,
            )

    async def _establish_session(self, *, attach: bool) -> None:
        """Prepare a fresh Core link and authenticate its current epoch."""

        if self.channel is None:
            raise HomeAssistantError("Thin-RPC channel is not configured")
        firmware_diagnostics = await self.channel.diagnostics()
        compatibility = check_proxy_compatibility(firmware_diagnostics)
        if not compatibility.compatible:
            raise HomeAssistantError(
                "ESPHome Thin-RPC proxy is incompatible; update it from the "
                "matching OpenRBus ESPHome source release"
            )
        # Firmware diagnostics are a liveness sample only. Never retain a
        # vendor identity, boot token, credential, or readiness assertion in
        # the HA backend; Core's link/auth proof is authoritative.
        self._diagnostics = {
            "available": True,
            "liveness": firmware_diagnostics.get("parent_connected") is True
            or firmware_diagnostics.get("link_active") is True,
            "compatibility": compatibility.reason,
        }
        services = ThinGattRpcServices(
            request=self.capability.request_service,
            poll=self.capability.poll_service,
            diagnostics=self.capability.diagnostics_service,
        )
        session = ThinGattSession(
            self.channel,
            services=services,
            profile=self.profile,
            poll_timeout=min(1.0, self.timeout),
        )
        self._session_generation = min(
            2_147_483_647, getattr(self, "_session_generation", 0) + 1
        )
        self.session = session
        link = (
            self.link_factory(session)
            if self.link_factory is not None
            else ThinGattLink(
                session,
                profile=self.profile,
                roles=self.roles,
                subscriptions=_THIN_SUBSCRIPTIONS,
            )
        )
        self.link = link
        # Attach only when this fresh session can actually attach to an
        # already-active physical link.  Otherwise Core must use the fresh
        # stream baseline (seq=1); its correlation rules remain authoritative.
        secure_timeout = max(self.timeout, _THIN_SECURE_TIMEOUT)
        await session.prepare(
            timeout=secure_timeout,
            attach=_thin_attach_mode(attach, firmware_diagnostics),
        )
        await link.prepare(timeout=secure_timeout)
        if not session.capability or not link.is_ready:
            raise HomeAssistantError("Thin-RPC secure link was not prepared")
        # Pairing/encryption is only the link proof.  The gateway service has
        # a separate fixed identity/auth exchange which must precede the
        # transparent CAN-IP transport, including for access level 1.
        if self.authenticator_factory is None:
            await link.authenticate_gateway(timeout=secure_timeout)
        # A configured role map is preferred. Explicit handles are retained as
        # an HA adapter escape hatch for installations whose firmware exposes
        # stable handles but no UUID metadata; they are still fenced to this
        # newly prepared epoch.
        request_handle, response_handle = self._resolve_handles(
            {}, link.session.handles
        )
        if request_handle is None or response_handle is None:
            raise HomeAssistantError(
                "Thin-RPC Core discovery did not provide request/response handles; "
                "configure both handles or a complete Thin-GATT profile"
            )
        # Core's link may retain descriptive role metadata alongside numeric
        # ATT handles.  The HA message adapter only needs the two transport
        # handles; do not re-validate non-handle metadata as GattHandles.
        values = {
            role: handle
            for role, handle in (
                session.handles.values.items() if session.handles is not None else ()
            )
            if type(handle) is int and handle >= 0
        }
        values.update({"request": request_handle, "response": response_handle})
        session.install_handles(
            GattHandles(
                values,
                epoch=session.epoch,
            )
        )
        self.transport = _PreparedThinGattMessageTransport(
            session,
            request_handle=request_handle,
            response_handle=response_handle,
        )
        self.client = RawObjectClient(self.transport, timeout=self.timeout)
        if (
            self.access_level >= 2
            and self.key_provider is None
            and self.authenticator_factory is None
        ):
            raise HomeAssistantError(
                "Thin-RPC gateway key provider is required for authenticated access"
            )
        if self.authenticator_factory is not None:
            authenticator = self.authenticator_factory(link)
            self.authentication = await authenticator.authenticate(
                timeout=secure_timeout
            )
            if not authenticator.is_authenticated:
                raise HomeAssistantError(
                    "Thin-RPC gateway authentication was not proven"
                )
        elif self.access_level >= 2:
            if self.key_provider is None:
                raise HomeAssistantError("Thin-RPC gateway key provider is required")
            material = self.key_provider(b"thin-rpc")
            if not isinstance(material, (TeaKeyComponent, bytes)):
                raise HomeAssistantError(
                    "EHC key provider returned invalid key material"
                )
            self.authentication = await CanIpGatewayAuthorizer(
                self.transport, max_access_level=self.access_level
            ).authorize(
                self.access_level, key_component=material, timeout=secure_timeout
            )

    def diagnostics(self) -> dict[str, Any]:
        """Return a safe snapshot; never expose credentials or private identity."""
        # Keep the output safe even if a future code path stores raw proxy
        # diagnostics here.  _establish_session currently stores only these
        # fields, but the public diagnostics boundary should enforce that.
        proxy = {
            key: self._diagnostics[key]
            for key in ("available", "liveness", "compatibility")
            if key in self._diagnostics
        }
        return {
            "backend": self.name,
            "started": self._started,
            "capability": self.capability.as_dict(),
            "session_connected": bool(self.session and self.session.connected),
            "session_generation": getattr(self, "_session_generation", 0),
            "session_epoch": (
                self.session.epoch
                if self.session is not None and type(self.session.epoch) is int
                else None
            ),
            "proxy": proxy,
            "setup_response": self.setup_metrics.diagnostics(),
            "recovery_fence": self._recovery_fence_metrics.diagnostics(),
            "batch_events": dict(self._batch_event_counts),
            "thin_rpc_frame_trace": tuple(getattr(self, "frame_trace", None) or ()),
            "read_operation_trace": tuple(getattr(self, "_read_operation_trace", ())),
            "last_read_transport_capture": getattr(
                self, "_last_read_transport_capture", None
            ),
            "last_batch_failure_trace": getattr(
                self, "_last_batch_failure_trace", None
            ),
        }

    def _record_batch_event(self, event: str) -> None:
        """Keep a bounded, backend-lifetime count of batch recovery events."""
        if event in self._batch_event_counts:
            self._batch_event_counts[event] = min(
                2_147_483_647, self._batch_event_counts[event] + 1
            )

    def _resolve_handles(
        self, snapshot: Mapping[str, Any], handles: GattHandles | None = None
    ) -> tuple[int | None, int | None]:
        # Config-entry values are JSON-decoded and may contain numeric
        # strings; Core's GattHandles contract is intentionally stricter.
        request = _safe_handle(self.request_handle)
        response = _safe_handle(self.response_handle)
        if handles is not None:
            request = (
                request
                if request is not None
                else _safe_handle(handles.values.get("request"))
            )
            response = (
                response
                if response is not None
                else _safe_handle(handles.values.get("response"))
            )
        return request, response


def _safe_handle(value: Any) -> int | None:
    return int(value) if type(value) is int and value >= 0 else None
