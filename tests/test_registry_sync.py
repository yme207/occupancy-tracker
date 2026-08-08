"""Tests for the registry sync layer (docs/ARCHITECTURE.md §1.1).

Uses pytest-homeassistant-custom-component's real (test-mode) registries —
not hand-rolled mocks — per docs/TESTING.md layer 2/4.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.registry_sync import RegistrySync


def _mock_device(hass: HomeAssistant, device_registry: dr.DeviceRegistry, *, name: str) -> str:
    """Create a device tied to a throwaway config entry and return its id."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", name)},
        name=name,
    )
    return device.id


async def test_empty_house_shape(hass: HomeAssistant) -> None:
    registry_sync = RegistrySync(hass)
    shape = registry_sync.house_shape
    assert shape.areas == {}
    assert shape.floors == {}
    assert shape.entities == {}


async def test_area_and_floor_snapshot(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    floor = floor_registry.async_create("Ground Floor")
    kitchen = area_registry.async_get_or_create("Kitchen")
    area_registry.async_update(kitchen.id, floor_id=floor.floor_id)
    hallway = area_registry.async_get_or_create("Hallway")  # no floor assigned

    registry_sync = RegistrySync(hass)
    shape = registry_sync.house_shape

    assert set(shape.floors) == {floor.floor_id}
    assert shape.floors[floor.floor_id].name == "Ground Floor"

    assert set(shape.areas) == {kitchen.id, hallway.id}
    assert shape.areas[kitchen.id].name == "Kitchen"
    assert shape.areas[kitchen.id].floor_id == floor.floor_id
    assert shape.areas[hallway.id].floor_id is None
    assert shape.areas[hallway.id].entity_ids == ()


async def test_entity_own_area_wins_over_device_area(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    device_id = _mock_device(hass, device_registry, name="motion-hub")
    device_registry.async_update_device(device_id, area_id=kitchen.id)

    entry = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "unique-1",
        suggested_object_id="hallway_motion",
        device_id=device_id,
    )
    entity_registry.async_update_entity(entry.entity_id, area_id=hallway.id)

    registry_sync = RegistrySync(hass)
    shape = registry_sync.house_shape

    snapshot = shape.entities[entry.entity_id]
    assert snapshot.area_id == hallway.id
    assert entry.entity_id in shape.areas[hallway.id].entity_ids
    assert entry.entity_id not in shape.areas[kitchen.id].entity_ids


async def test_entity_falls_back_to_device_area(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    device_id = _mock_device(hass, device_registry, name="kitchen-hub")
    device_registry.async_update_device(device_id, area_id=kitchen.id)

    entry = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "unique-2",
        suggested_object_id="kitchen_motion",
        device_id=device_id,
    )

    registry_sync = RegistrySync(hass)
    shape = registry_sync.house_shape

    assert shape.entities[entry.entity_id].area_id == kitchen.id
    assert entry.entity_id in shape.areas[kitchen.id].entity_ids


async def test_entity_with_no_area_or_device_has_no_area(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "unique-3", suggested_object_id="orphan"
    )

    registry_sync = RegistrySync(hass)
    shape = registry_sync.house_shape

    assert shape.entities[entry.entity_id].area_id is None


async def test_entity_disabled_and_hidden_flags(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    entry = entity_registry.async_get_or_create(
        "binary_sensor",
        "test",
        "unique-4",
        suggested_object_id="flagged",
        disabled_by=er.RegistryEntryDisabler.USER,
        hidden_by=er.RegistryEntryHider.USER,
    )

    registry_sync = RegistrySync(hass)
    shape = registry_sync.house_shape

    snapshot = shape.entities[entry.entity_id]
    assert snapshot.disabled is True
    assert snapshot.hidden is True


async def test_live_area_creation_triggers_listener_and_rebuild(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
) -> None:
    registry_sync = RegistrySync(hass)
    registry_sync.async_setup()

    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    registry_sync.async_add_listener(on_change)

    living_room = area_registry.async_get_or_create("Living Room")
    await hass.async_block_till_done()

    assert calls == 1
    assert living_room.id in registry_sync.house_shape.areas


async def test_area_removal_clears_entity_area_id(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "unique-5", suggested_object_id="kitchen_sensor"
    )
    entity_registry.async_update_entity(entry.entity_id, area_id=kitchen.id)

    registry_sync = RegistrySync(hass)
    registry_sync.async_setup()

    area_registry.async_delete(kitchen.id)
    await hass.async_block_till_done()

    shape = registry_sync.house_shape
    assert kitchen.id not in shape.areas
    # HA core cascades area deletion by clearing area_id on referencing
    # entities (area_registry.py's async_delete calls
    # entity_registry.async_clear_area_id) — verify the sync layer reflects
    # that, not a stale reference to a deleted area.
    assert shape.entities[entry.entity_id].area_id is None


async def test_unload_stops_listening(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
) -> None:
    registry_sync = RegistrySync(hass)
    registry_sync.async_setup()
    registry_sync.async_unload()

    area_registry.async_get_or_create("Attic")
    await hass.async_block_till_done()

    assert registry_sync.house_shape.areas == {}


async def test_listener_can_unsubscribe(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
) -> None:
    registry_sync = RegistrySync(hass)
    registry_sync.async_setup()

    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    remove = registry_sync.async_add_listener(on_change)
    remove()

    area_registry.async_get_or_create("Basement")
    await hass.async_block_till_done()

    assert calls == 0
