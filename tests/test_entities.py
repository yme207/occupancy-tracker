"""End-to-end test: a real entity state change reaching the sensor/binary_sensor
entities through registry sync -> topology store -> engine adapter -> signal
ingestion -> engine -> push-updated HA state (docs/ARCHITECTURE.md §1).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.topology_store import TopologyData


async def test_area_activity_updates_entities_end_to_end(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    motion_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen-motion-unique-id", suggested_object_id="kitchen_motion"
    )
    entity_registry.async_update_entity(motion_entry.entity_id, area_id=kitchen.id)
    hass.states.async_set(motion_entry.entity_id, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # The topology's per-area entity selection was empty at setup, so no
    # signal will flow yet -- select the entity now and restart signal
    # ingestion to pick it up (mirrors what the Phase 7 topology editor will
    # eventually trigger).
    runtime_data = entry.runtime_data
    topology = TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    await runtime_data.topology_store.async_save(topology)
    runtime_data.signal_ingestion.async_start(topology)

    assert hass.states.get("sensor.kitchen_occupant_count") is not None

    hass.states.async_set(motion_entry.entity_id, "on")
    await hass.async_block_till_done()

    count_state = hass.states.get("sensor.kitchen_occupant_count")
    occupied_state = hass.states.get("binary_sensor.kitchen_occupied")
    total_state = hass.states.get("sensor.total_occupant_count")

    assert count_state is not None
    assert count_state.state == "1"
    assert count_state.attributes["provenance"] == "ambiguous_physical"
    assert occupied_state is not None
    assert occupied_state.state == "on"
    assert occupied_state.attributes["provenance"] == "ambiguous_physical"
    assert total_state is not None
    assert total_state.state == "1"
