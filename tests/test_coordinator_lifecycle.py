"""Small lifecycle tests for the live-read waiter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.openrbus import _cancel_task
from custom_components.openrbus.coordinator import OpenRBusCoordinator


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
    coordinator.refresh_action = "proxy_openrbus_gateway_auth"
    coordinator._cycle_id = 0
    return coordinator


@pytest.mark.asyncio
async def test_timeout_removes_listener_and_releases_lock(monkeypatch) -> None:
    coordinator = _coordinator()
    coordinator._poll_lock = asyncio.Lock()
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
