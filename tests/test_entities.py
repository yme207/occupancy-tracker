"""End-to-end test: a real entity state change reaching the sensor/binary_sensor
entities through registry sync -> topology store -> engine adapter -> signal
ingestion -> engine -> push-updated HA state (docs/ARCHITECTURE.md §1).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.const import CONF_HOUSEHOLD_SIZE_HINT
from custom_components.occupancy_tracker.topology_store import EgressPoint, TopologyData


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

    # The topology's per-area entity selection was empty at setup, so Kitchen
    # isn't tracked yet (no entities at all — project-owner feedback that an
    # untouched Area shouldn't get sensors) and no signal flows either.
    # Selecting the entity and reloading (mirrors what the topology editor
    # actually triggers) is what both creates the entities and picks up the
    # new signal-ingestion subscription in one step.
    await entry.runtime_data.topology_store.async_save(
        TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

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


async def test_total_occupant_count_flags_when_exceeding_household_size_hint(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    """SPEC.md §6.4's household-size hint must never cap the count, but
    should surface as a confidence-only, inspectable attribute (§6.8) once
    the real count exceeds it.
    """
    kitchen = area_registry.async_get_or_create("Kitchen")
    study = area_registry.async_get_or_create("Study")  # not connected to kitchen
    kitchen_motion = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen-motion", suggested_object_id="kitchen_motion"
    )
    study_motion = entity_registry.async_get_or_create(
        "binary_sensor", "test", "study-motion", suggested_object_id="study_motion"
    )
    entity_registry.async_update_entity(kitchen_motion.entity_id, area_id=kitchen.id)
    entity_registry.async_update_entity(study_motion.entity_id, area_id=study.id)
    hass.states.async_set(kitchen_motion.entity_id, "off")
    hass.states.async_set(study_motion.entity_id, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain="occupancy_tracker", options={CONF_HOUSEHOLD_SIZE_HINT: 1})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await entry.runtime_data.topology_store.async_save(
        TopologyData(
            area_entity_selections={
                kitchen.id: (kitchen_motion.entity_id,),
                study.id: (study_motion.entity_id,),
            }
        )
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(kitchen_motion.entity_id, "on")
    await hass.async_block_till_done()
    total_state = hass.states.get("sensor.total_occupant_count")
    assert total_state is not None
    assert total_state.state == "1"
    assert total_state.attributes["exceeds_household_size_hint"] is False

    # Two disconnected, simultaneously-active Areas is unexplainable as one
    # person moving between them (SPEC.md §6.4) — a second, independent
    # occupant, taking the total past the hint of 1.
    hass.states.async_set(study_motion.entity_id, "on")
    await hass.async_block_till_done()
    total_state = hass.states.get("sensor.total_occupant_count")
    assert total_state is not None
    assert total_state.state == "2"
    assert total_state.attributes["exceeds_household_size_hint"] is True


async def test_total_occupant_count_flags_when_unexplained_by_doors(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    """docs/DECISIONS.md's "whole-house conservation" entry: once a real
    door crossing has anchored the total, an interior signal with no
    plausible source (a disconnected Area, same shape SPEC.md §6.4 already
    allows as a genuinely new occupant) should surface as "unexplained by
    doors" — informational only, never capping the count itself.
    """
    entryway = area_registry.async_get_or_create("Entryway")
    study = area_registry.async_get_or_create("Study")  # not connected to entryway
    door = entity_registry.async_get_or_create(
        "binary_sensor", "test", "front-door", suggested_object_id="front_door"
    )
    entryway_motion = entity_registry.async_get_or_create(
        "binary_sensor", "test", "entryway-motion", suggested_object_id="entryway_motion"
    )
    study_motion = entity_registry.async_get_or_create(
        "binary_sensor", "test", "study-motion", suggested_object_id="study_motion"
    )
    entity_registry.async_update_entity(door.entity_id, area_id=entryway.id)
    entity_registry.async_update_entity(entryway_motion.entity_id, area_id=entryway.id)
    entity_registry.async_update_entity(study_motion.entity_id, area_id=study.id)
    hass.states.async_set(door.entity_id, "off")
    hass.states.async_set(entryway_motion.entity_id, "off")
    hass.states.async_set(study_motion.entity_id, "off")
    await hass.async_block_till_done()

    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await entry.runtime_data.topology_store.async_save(
        TopologyData(
            egress_points=(EgressPoint(area_id=entryway.id, entity_ids=(door.entity_id,)),),
            area_entity_selections={
                entryway.id: (entryway_motion.entity_id,),
                study.id: (study_motion.entity_id,),
            },
        )
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # No door evidence has arrived yet — unanchored, no flag at all.
    total_state = hass.states.get("sensor.total_occupant_count")
    assert total_state is not None
    assert "unexplained_by_doors" not in total_state.attributes

    # Door opens, then entryway's own motion corroborates — a confirmed
    # arrival through the front door, establishing the anchor.
    hass.states.async_set(door.entity_id, "on")
    await hass.async_block_till_done()
    hass.states.async_set(entryway_motion.entity_id, "on")
    await hass.async_block_till_done()
    total_state = hass.states.get("sensor.total_occupant_count")
    assert total_state is not None
    assert total_state.state == "1"
    assert total_state.attributes["door_confirmed_occupant_count"] == 1
    assert total_state.attributes["unexplained_by_doors"] is False

    # study is disconnected from entryway — a new, ungrounded occupant.
    hass.states.async_set(study_motion.entity_id, "on")
    await hass.async_block_till_done()
    total_state = hass.states.get("sensor.total_occupant_count")
    assert total_state is not None
    assert total_state.state == "2"
    assert total_state.attributes["door_confirmed_occupant_count"] == 1
    assert total_state.attributes["unexplained_by_doors"] is True
