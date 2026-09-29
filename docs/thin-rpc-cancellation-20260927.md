# Thin-RPC setup cancellation: test-HA finding (2026-09-27)

## Scope

Authorized test Home Assistant on `kikis-yoga`, ESPHome Thin-RPC only; no
RBus writes were issued.

## Evidence

`config/home-assistant.log` records two setup failures (00:52 and 07:15)
whose leaf is `asyncio.CancelledError` while `aioesphomeapi` waits for the
response to the Thin-RPC poll user service during Core capability discovery.
Home Assistant's `ConfigEntry.async_setup` logs the "setup ... cancelled"
variant only when the *current setup task* has a pending cancellation
(`task.cancelling() > 0`). This is distinct from an ESPHome service timeout:
the ESPHome manager maps that timeout to `HomeAssistantError`, and the
aioesphome client would first raise `TimeoutError`.

The integration correctly lets `CancelledError` propagate. Catching or
converting it in `HomeAssistantThinGattChannel.poll()` would suppress the
owner's lifecycle cancellation and can leave a stale service request or
session in flight, so there is no safe transport-layer retry/fix for that
condition.

At 07:18 the same test instance subsequently completed coordinator polls
successfully, including a fast update in 13.859 s and a regular update in
23.386 s. Those updates require real object reads through the same loaded
Thin-RPC path. A later duplicate setup attempt failed specifically with
`Thin-RPC controller is already owned by another entry`; it is an overlapping
reload/lifecycle issue, not a proxy response failure.

## Conclusion and next diagnostic

The available evidence attributes the earlier failure to cancellation of the
HA setup/reload request, not a normal proxy response timeout. Do not add a
transport retry for `CancelledError`.

If it recurs, trigger exactly one reload with a caller timeout above 120 s
and do not send a second reload while it is in progress. Capture the reload
caller and the setup task's cancellation message/name at the same timestamp.
If the task is not externally cancelled, capture the ESPHome diagnostics
snapshot before and after the stuck poll; only then investigate a proxy
response-path defect.
