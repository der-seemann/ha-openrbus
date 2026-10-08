"""Tests for durable zone projection identity and fresh-read separation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.openrbus.identity import stable_object_id
from custom_components.openrbus.zone_projection_storage import (
    async_load_zone_projection,
    async_save_zone_projection,
)
from custom_components.openrbus.zones import (
    ZoneProfile,
    ZoneReadState,
    profile_for,
    zone_is_active,
    zone_projection_exists,
    zone_read_state,
)


@pytest.mark.asyncio
async def test_projection_storage_round_trips_and_scopes_entry_and_target(
    monkeypatch,
) -> None:
    payloads = {}

    class FakeStore:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            return payloads.get(self.key)

        async def async_save(self, value):
            payloads[self.key] = value

    monkeypatch.setattr(
        "custom_components.openrbus.zone_projection_storage.Store", FakeStore
    )
    history = {
        "4:scb-10": {
            1: {
                "slot": 1,
                "node": 4,
                "family": "scb-10",
                "function": 2,
                "friendly_name": "Ground floor",
                "node_name": "SCB-10",
                "short_name": None,
                "uids": [
                    stable_object_id(
                        SimpleNamespace(
                            config_entry=SimpleNamespace(
                                data={"ble_device": "target"}, options={}
                            )
                        ),
                        4,
                        0x346A,
                        1,
                    )
                ],
            }
        }
    }

    await async_save_zone_projection(object(), "entry-a", "AA:BB", history)

    assert await async_load_zone_projection(object(), "entry-a", "aa:bb") == history
    assert await async_load_zone_projection(object(), "entry-b", "aa:bb") == {}
    assert await async_load_zone_projection(object(), "entry-a", "CC:DD") == {}


def test_projection_identity_does_not_authorize_current_activity() -> None:
    identity = SimpleNamespace(node=4, family="Scb-10")
    parent = SimpleNamespace(
        zone_profiles={},
        zone_profile_states={(4, 1): ZoneReadState.UNKNOWN},
        zone_projection_history={
            "4:scb-10": {
                1: {
                    "slot": 1,
                    "node": 4,
                    "family": "scb-10",
                    "function": 2,
                    "friendly_name": "Ground floor",
                    "node_name": "SCB-10",
                    "uids": [
                        stable_object_id(
                            SimpleNamespace(
                                config_entry=SimpleNamespace(
                                    data={"ble_device": "target"}, options={}
                                )
                            ),
                            4,
                            0x346A,
                            1,
                        )
                    ],
                }
            }
        },
    )

    assert profile_for(parent, 4, 1, identity) == ZoneProfile(
        4, 1, 2, "Ground floor", "SCB-10", None
    )
    unique_id = stable_object_id(
        SimpleNamespace(
            config_entry=SimpleNamespace(data={"ble_device": "target"}, options={})
        ),
        4,
        0x346A,
        1,
    )
    assert zone_projection_exists(parent, 4, 1, identity, unique_id)
    assert not zone_projection_exists(parent, 4, 1, identity, "gateway:fake")
    assert not zone_is_active(parent, 4, 1)
    assert zone_read_state(parent, 4, 1) is ZoneReadState.UNKNOWN
    assert not zone_projection_exists(
        parent, 4, 1, SimpleNamespace(node=4, family="Ehc-16"), unique_id
    )


def test_unknown_without_projection_history_does_not_retain_legacy_slot() -> None:
    parent = SimpleNamespace(
        zone_profiles={},
        zone_profile_states={(4, 1): ZoneReadState.UNKNOWN},
        zone_projection_history={},
    )
    identity = SimpleNamespace(node=4, family="Scb-10")

    assert profile_for(parent, 4, 1, identity) is None
    assert not zone_projection_exists(parent, 4, 1, identity)
    assert not zone_is_active(parent, 4, 1)
