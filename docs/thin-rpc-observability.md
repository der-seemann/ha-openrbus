# Thin-RPC stability diagnostics

The config-entry diagnostics export now includes optional `coordinator.poll_groups`
`coordinator.coordinator_poll_count`, `coordinator.coordinator_error_counts`,
and `coordinator.transport_session` fields. They are additive, so existing
diagnostic consumers can ignore them. Poll groups are restricted to `fast`,
`standard`, and `slow`; each reports bounded cumulative poll/item counts, the
number of currently available and unavailable items, the change in available
items since its previous poll, and fixed error counters (`item`, `batch`,
`decode`, `correlation`, `session`). Counters saturate at 2,147,483,647.
Each poll group also reports fixed subtype counters for session failures
(`not_ready`, `link_lost`, `timeout`, `not_secure`, `transport`) and for
returned batch failures. The separate backend-level `batch_events` reports
malformed batches, aborts, and single-read fallbacks, including recovered
fallbacks. Address-level failed-item entries remain capped at 16 and appear
only when `diagnostics_enabled` is on; entries contain only numeric
node/object coordinates and fixed class/subtype enums.

Thin-RPC session diagnostics contain only the local session-generation count
and Core's numeric current epoch when available. They do not expose physical
identity, request identifiers, raw frames, payloads, credentials, MACs,
addresses, or error messages. Transport failure classes are retained only as
fixed internal tags and aggregate diagnostic counts; the integration does not
log them by default. `item` means an object-local read failure, `batch` means a
poll transaction or cardinality failure, `decode` means a value/registry
validation failure, `correlation` means a Thin-GATT correlation rejection,
and `session` means a transport/session-state failure. Its subtype is selected
from the typed failure or a small fixed mapping of Core's known session-state
messages; arbitrary text is never retained or exported. `batch_events` is a
backend-lifetime aggregate of malformed batch cardinality, per-batch aborts,
and single-read fallbacks, including recovered fallbacks. Those events are
separate from returned failed-item counters: a successful fallback is an
event, not an item error.

The fields are observational only. They do not change retries, availability
policy, write safety, or Core's frame acceptance rules. A counter records
failed returned items; a batch failure followed by successful single-item
fallbacks is not a returned item error.

`coordinator.transport_session.recovery_fence` reports the backend-lifetime
attempt count, whether the latest Thin-RPC disconnect action was acknowledged,
the latest bounded `link_active` and `parent_connected` booleans, and a
saturating timeout count split across fixed state categories. The epoch is not
a disconnect sentinel: the proxy increments it for a new connection and
retains it after disconnect. A replacement connection is not prepared until
both live connection booleans are false. These fields report the final sample
from a bounded wait and do not alter that gate.

## Initial setup service-response evidence

Config-entry diagnostics also include `coordinator.setup_response` while the
Thin-RPC backend is available, including when initial setup stopped before a
coordinator became usable. It reports fixed counters for `pairing_arm`,
`handle_lookup`, and `poll_request`, plus counts in coarse elapsed-time bands
(`lt_100ms`, `100_499ms`, `500_1999ms`, `2_9999ms`, `gte_10s`). Counters
saturate at 2,147,483,647. Fixed outcomes distinguish a locally absent
service (`call_not_sent`), an ESPHome service timeout (`timeout_no_response`),
a completed request dispatch (`call_completed`), a Thin-RPC frame observed
(`esp_response` / `response`), an empty poll with no Thin-RPC frame
(`no_esp_response`, attributed to `handle_lookup` while a lookup response is
pending), and HA task cancellation before or after a correlated HANDLE_LOOKUP
response. Pairing-arm recovery also records `recovery_succeeded` or
`recovery_failed` when the first arm returns but the physical disconnect
boundary does not arrive and the bounded sibling-action reset/rearm is used.
`error` is a fixed category; exception text is never kept.

The setup-response counts are explicitly scoped to the lifetime of one backend
object and include `setup_attempts`, which increments each time startup really
runs after a stopped/failed state. They are not one-setup counters. For
example, 81 correlated HANDLE_LOOKUP responses can represent three startup
attempts of 27 responses each; the aggregate alone does not prove that split.
A normal config-entry reload creates a new backend object and therefore a
clean diagnostics scope.

These counts contain no service or controller names, addresses, request IDs,
frame content, credentials, or raw errors. They are in-memory, cumulative for
the backend object's lifetime, and observational only. They do not catch or
convert HA task cancellation, retry a service call, or affect request/session
correlation. A missing service is the only condition reported as
`call_not_sent`; other failures after invoking the HA service API are not
claimed to prove whether the request reached the proxy.
