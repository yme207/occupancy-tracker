"""Tests for config-entry setup/unload wiring (custom_components/occupancy_tracker/__init__.py).

Uses the real `hass.config_entries.async_setup()`/`async_unload()` flow
(needs `enable_custom_integrations` so the loader can find this integration
on disk) rather than calling `async_setup_entry`/`async_unload_entry`
directly, since Phase 4 forwards to real sensor/binary_sensor platforms that
only that flow discovers.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker import OccupancyTrackerRuntimeData
from custom_components.occupancy_tracker.const import CONF_TRACKED_PERSONS
from custom_components.occupancy_tracker.topology_store import Connector, TopologyData


async def test_setup_entry_wires_up_runtime_data(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, OccupancyTrackerRuntimeData)


async def test_setup_entry_creates_entities_for_each_area(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    area_registry.async_get_or_create("Kitchen")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_occupant_count") is not None
    assert hass.states.get("binary_sensor.kitchen_occupied") is not None
    assert hass.states.get("sensor.total_occupant_count") is not None
    assert hass.states.get("binary_sensor.pre_armed") is not None


async def test_setup_entry_registry_sync_reacts_to_live_changes(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    attic = area_registry.async_get_or_create("Attic")
    await hass.async_block_till_done()

    assert attic.id in entry.runtime_data.registry_sync.house_shape.areas


async def test_setup_entry_reconciles_topology_on_live_area_removal(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    topology_store = entry.runtime_data.topology_store
    await topology_store.async_save(
        TopologyData(connectors=(Connector("c1", kitchen.id, hallway.id),))
    )

    area_registry.async_delete(kitchen.id)
    await hass.async_block_till_done()

    assert topology_store.topology.connectors == ()


async def test_unload_entry_stops_registry_sync_listening(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry_sync = entry.runtime_data.registry_sync

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED

    area_registry.async_get_or_create("Loft")
    await hass.async_block_till_done()

    assert registry_sync.house_shape.areas == {}


async def test_options_change_reloads_entry_and_zone_fusion_picks_it_up(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zone_fusion.house_zone_corroboration().name == "UNKNOWN"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACKED_PERSONS: ["person.alice"]}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    hass.states.async_set("person.alice", "home")
    await hass.async_block_till_done()

    assert entry.runtime_data.zone_fusion.house_zone_corroboration().name == "CORROBORATED"
