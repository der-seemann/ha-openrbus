"""Small lifecycle tests for the live-read waiter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus import _cancel_task
from custom_components.openrbus.coordinator import OpenRBusCoordinator, _runtime_frame


class _States:
    def __init__(self) -> None:
        self.values = {
            "sensor.generation": SimpleNamespace(state="7"),
            "sensor.uptime": SimpleNamespace(state="100"),
            "sensor.response": SimpleNamespace(
                state="ff01020000000100ff200102161efd82"
            ),
        }

    def get(self, entity_id: str):
        return self.values.get(entity_id)


class _Services:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def async_call(self, *_args, **_kwargs) -> None:
        self.calls += 1
        if self.error:
            raise self.error


def _coordinator() -> OpenRBusCoordinator:
    coordinator = object.__new__(OpenRBusCoordinator)
    coordinator.hass = SimpleNamespace(
        states=_States(), services=_Services()
    )
    coordinator.response_entity = "sensor.response"
    coordinator.generation_entity = "sensor.generation"
    coordinator.uptime_entity = "sensor.uptime"
    coordinator.pairing_status_entity = "sensor.pairing_status"
    coordinator.refresh_action = "proxy_openrbus_gateway_auth"
    coordinator._cycle_id = 0
    coordinator._runtime_authenticated = False
    coordinator._reauth_required = False
    return coordinator


def test_runtime_frame_is_ble_segmented_and_wrapped() -> None:
    assert _runtime_frame(0xFF, ObjectAddress(0x2001, 0x02)) == (
        "ff01020000000000ff2001025d12"
    )


@pytest.mark.asyncio
async def test_timeout_removes_listener_and_releases_lock(monkeypatch) -> None:
    coordinator = _coordinator()
    coordinator._poll_lock = asyncio.Lock()
    coordinator._runtime_authenticated = True
    coordinator._reauth_required = False
    removed = False

    def track(*_args, **_kwargs):
        nonlocal removed

        def remove() -> None:
            nonlocal removed
            removed = True

        return remove

    async def immediate_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.async_track_state_change_event",
        track,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.wait_for",
        immediate_timeout,
    )

    with pytest.raises(UpdateFailed, match="timed out"):
        await coordinator._async_update_data()

    assert removed
    assert not coordinator._poll_lock.locked()
    assert not coordinator._runtime_authenticated
    assert coordinator.hass.services.calls == 1


@pytest.mark.asyncio
async def test_service_error_does_not_overlap_auth_actions(monkeypatch) -> None:
    coordinator = _coordinator()
    coordinator.hass.services = _Services(HomeAssistantError("not ready"))
    removed = False

    def track(*_args, **_kwargs):
        nonlocal removed

        def remove() -> None:
            nonlocal removed
            removed = True

        return remove

    async def immediate_timeout(awaitable, *_args, **_kwargs):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.async_track_state_change_event",
        track,
    )
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.asyncio.wait_for",
        immediate_timeout,
    )

    with pytest.raises(UpdateFailed, match="timed out"):
        await coordinator._async_request_live_read(1)

    assert removed
    assert coordinator.hass.services.calls == 1


@pytest.mark.asyncio
async def test_first_refresh_task_cancel_callback_awaits_cleanup() -> None:
    started = asyncio.Event()

    async def pending() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(pending())
    await started.wait()
    await _cancel_task(task)

    assert task.done()
    assert task.cancelled()


def test_lifecycle_failure_classifier_is_explicit() -> None:
    assert OpenRBusCoordinator._lifecycle_failure(
        SimpleNamespace(state="gateway_auth_failed")
    ) == "gateway_auth_failed"
    assert OpenRBusCoordinator._lifecycle_failure(
        SimpleNamespace(state="gateway_ident_sent")
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["gateway_auth_failed", "invalid_passkey", "unpaired"])
async def test_existing_lifecycle_failure_fails_before_auth_call(status: str) -> None:
    coordinator = _coordinator()
    coordinator.hass.states.values["sensor.pairing_status"] = SimpleNamespace(
        state=status
    )
    with pytest.raises(UpdateFailed, match=status):
        await coordinator._async_request_live_read(1)
    assert coordinator.hass.services.calls == 0


@pytest.mark.asyncio
async def test_raw_read_uptime_reset_invalidates_stale_response(monkeypatch) -> None:
    coordinator = _coordinator()
    coordinator.raw_read_action = "proxy_openrbus_raw_read"
    coordinator.dynamic_enable_action = None
    coordinator._runtime_authenticated = True
    coordinator._poll_lock = asyncio.Lock()
    coordinator._runtime_request_id = 10000
    coordinator.passkey = 0
    callbacks = []
    calls = []
    raw_calls = 0

    def track(*_args, **kwargs):
        callbacks.append(kwargs.get("action") or _args[-1])
        return lambda: None

    async def call(_domain, action, _data, **_kwargs):
        nonlocal raw_calls
        calls.append(action)
        if action == coordinator.refresh_action:
            callback = callbacks[-1]
            fresh = SimpleNamespace(state="gateway_authenticated_dynamic")
            coordinator.hass.states.values["sensor.pairing_status"] = fresh
            await callback(SimpleNamespace(data={"entity_id": "sensor.pairing_status", "new_state": fresh}))
        if action == coordinator.raw_read_action:
            raw_calls += 1
            callback = callbacks[-1] if raw_calls == 1 else callbacks[-2]
            state = coordinator.hass.states.get("sensor.response")
            await callback(SimpleNamespace(data={"entity_id": "sensor.response", "new_state": state}))
            uptime = "1" if raw_calls == 1 else "101"
            await callback(SimpleNamespace(data={"entity_id": "sensor.uptime", "new_state": SimpleNamespace(state=uptime)}))
            generation = "8" if raw_calls == 1 else "9"
            await callback(SimpleNamespace(data={"entity_id": "sensor.generation", "new_state": SimpleNamespace(state=generation)}))

    coordinator.hass.services.async_call = call
    monkeypatch.setattr(
        "custom_components.openrbus.coordinator.async_track_state_change_event", track
    )
    with pytest.raises(HomeAssistantError, match="reconnected"):
        await coordinator.async_read_object(ObjectAddress(0x2001, 0x02))
    assert not coordinator._runtime_authenticated
    coordinator.hass.states.values["sensor.pairing_status"] = SimpleNamespace(
        state="gateway_authenticated_dynamic"
    )
    await coordinator.async_read_object(ObjectAddress(0x2001, 0x02))
    assert calls.count(coordinator.refresh_action) == 1
