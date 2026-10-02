"""Regression tests for the bounded typed-to-sensor registry migration."""

from types import SimpleNamespace

from openrbus.protocol.canip import ObjectAddress

from custom_components.openrbus import register_entities


class _Registry:
    def __init__(self, entries):
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get_entity_id(self, domain, platform, unique_id):
        return next(
            (
                entry.entity_id
                for entry in self.entities.values()
                if entry.domain == domain
                and entry.platform == platform
                and entry.unique_id == unique_id
            ),
            None,
        )

    def async_remove(self, entity_id):
        self.entities.pop(entity_id, None)

    def async_update_entity(self, entity_id, **changes):
        for field, value in changes.items():
            if field == "new_unique_id":
                field = "unique_id"
            setattr(self.entities[entity_id], field, value)


def _parent(entry_id="entry-current"):
    return SimpleNamespace(
        language="de",
        config_entry=SimpleNamespace(
            entry_id=entry_id, data={"ble_device": "00:11:22:33:44:55"}, options={}
        ),
        effective_access_levels={4: 3},
        configured_access_level=3,
        configured_read_access_level=3,
        configured_write_access_level=1,
        write_enabled=True,
    )


def _row(address="346a:00"):
    return SimpleNamespace(
        readable=True,
        writable=True,
        address=ObjectAddress.parse(address),
        access_level_evidence={
            "write": {"known": True, "complete": True, "levels": ["User"]}
        },
    )


def _entity(entity_id, *, domain, platform, unique_id, entry_id, **kwargs):
    values = {
        "entity_id": entity_id,
        "domain": domain,
        "platform": platform,
        "unique_id": unique_id,
        "config_entry_id": entry_id,
        "device_id": None,
        "area_id": None,
        "disabled_by": None,
        "hidden_by": None,
        "icon": None,
        "name": None,
        "entity_category": None,
        "labels": set(),
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_registry_uid_migration_retains_entity_id_and_user_disable(monkeypatch):
    parent = _parent()
    entity = _entity(
        "sensor.my_existing_name",
        domain="sensor",
        platform="openrbus",
        unique_id="entry-current:node:4:object:346a:04",
        entry_id="entry-current",
        disabled_by="user",
    )
    registry = _Registry([entity])
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities.dr,
        "async_get",
        lambda _hass: SimpleNamespace(devices={}),
    )

    register_entities.migrate_stable_registry_ids(object(), parent)

    assert entity.entity_id == "sensor.my_existing_name"
    assert entity.unique_id == register_entities.entity_unique_id(
        parent, SimpleNamespace(node=4), _row("346a:04")
    )
    assert entity.disabled_by == "user"


def test_old_typed_rows_are_removed_only_for_current_entry_and_identity(monkeypatch):
    parent = _parent()
    row = _row()
    unique_id = register_entities.entity_unique_id(parent, SimpleNamespace(node=4), row)
    unrelated_uid = register_entities.entity_unique_id(
        parent, SimpleNamespace(node=4), _row("346a:01")
    )
    entries = [
        _entity(
            "number.openrbus_count",
            domain="number",
            platform="openrbus",
            unique_id=unique_id,
            entry_id="entry-current",
            device_id="device-1",
            area_id="area-1",
            icon="mdi:counter",
            name="My count",
            disabled_by="user",
        ),
        _entity(
            "select.openrbus_count",
            domain="select",
            platform="openrbus",
            unique_id=unique_id,
            entry_id="entry-current",
        ),
        _entity(
            "switch.other_integration",
            domain="switch",
            platform="other",
            unique_id=unique_id,
            entry_id="entry-current",
        ),
        _entity(
            "number.other_entry",
            domain="number",
            platform="openrbus",
            unique_id=unique_id,
            entry_id="entry-other",
        ),
        _entity(
            "number.other_row",
            domain="number",
            platform="openrbus",
            unique_id=unrelated_uid,
            entry_id="entry-current",
        ),
    ]
    registry = _Registry(entries)
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda _parent: ((SimpleNamespace(node=4), row, "standard", True),),
    )
    monkeypatch.setattr(register_entities, "control_kind", lambda *_args: None)

    assert register_entities.cleanup_legacy_sensor_entities(object(), parent) == 2
    assert set(registry.entities) == {
        "switch.other_integration",
        "number.other_entry",
        "number.other_row",
    }
    pending = parent._openrbus_sensor_migrations[unique_id]
    assert pending["device_id"] == "device-1"
    assert pending["area_id"] == "area-1"
    assert pending["disabled_by"] == "user"

    # A second pass is idempotent and cannot affect the protected rows.
    assert register_entities.cleanup_legacy_sensor_entities(object(), parent) == 0
    assert set(registry.entities) == {
        "switch.other_integration",
        "number.other_entry",
        "number.other_row",
    }

    registry.entities["sensor.openrbus_count"] = _entity(
        "sensor.openrbus_count",
        domain="sensor",
        platform="openrbus",
        unique_id=unique_id,
        entry_id="entry-current",
    )
    assert register_entities.restore_migrated_sensor_entities(object(), parent) == 1
    sensor = registry.entities["sensor.openrbus_count"]
    assert sensor.device_id == "device-1"
    assert sensor.area_id == "area-1"
    assert sensor.icon == "mdi:counter"
    assert sensor.name == "My count"
    assert sensor.disabled_by == "user"
    assert not parent._openrbus_sensor_migrations


def test_fresh_entry_is_a_noop(monkeypatch):
    parent = _parent("fresh")
    registry = _Registry([])
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(register_entities, "rows_for_parent", lambda _parent: ())
    assert register_entities.cleanup_legacy_sensor_entities(object(), parent) == 0
    assert register_entities.restore_migrated_sensor_entities(object(), parent) == 0
    assert not registry.entities


def test_legacy_sensor_cleanup_for_current_typed_projection_is_preserved(monkeypatch):
    parent = _parent("typed")
    row = _row("346a:04")
    identity = SimpleNamespace(node=4)
    unique_id = register_entities.entity_unique_id(parent, identity, row)
    registry = _Registry(
        [
            _entity(
                "sensor.openrbus_old",
                domain="sensor",
                platform="openrbus",
                unique_id=unique_id,
                entry_id="typed",
            )
        ]
    )
    monkeypatch.setattr(register_entities.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        register_entities,
        "rows_for_parent",
        lambda _parent: ((identity, row, "standard", True),),
    )
    monkeypatch.setattr(register_entities, "control_kind", lambda *_args: "select")

    assert register_entities.cleanup_legacy_sensor_entities(object(), parent) == 1
    assert not registry.entities
