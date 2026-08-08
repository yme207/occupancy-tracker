"""Tests for the signal ingestion layer (docs/ARCHITECTURE.md §1.3)."""

from __future__ import annotations

from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.occupancy_tracker.engine_adapter import egress_connector_id
from custom_components.occupancy_tracker.occupancy_engine import (
    OUTSIDE,
    AreaActivitySignal,
    GraphConnector,
    HouseGraph,
    OccupancyEngine,
    ProvenanceTier,
)
from custom_components.occupancy_tracker.signal_ingestion import SignalIngestion
from custom_components.occupancy_tracker.topology_store import EgressPoint, TopologyData


async def test_selected_entity_turning_on_feeds_an_area_activity_signal(
    hass: HomeAssistant,
) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    ingestion.async_start(topology)

    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()

    assert engine.area_state("kitchen", dt_util.utcnow()).occupant_count == 1


async def test_selected_entity_turning_off_is_not_activity_evidence(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    ingestion.async_start(topology)

    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()

    # No "on" transition was observed after ingestion started, so no signal
    # should have reached the engine at all.
    assert engine.area_state("kitchen", dt_util.utcnow()).occupant_count == 0


async def test_unselected_entity_is_ignored(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.unrelated", "off")
    await hass.async_block_till_done()

    ingestion.async_start(TopologyData(area_entity_selections={"kitchen": ()}))

    hass.states.async_set("binary_sensor.unrelated", "on")
    await hass.async_block_till_done()

    assert engine.area_state("kitchen", dt_util.utcnow()).occupant_count == 0


async def test_egress_entity_turning_on_feeds_a_connector_activity_signal(
    hass: HomeAssistant,
) -> None:
    connector = GraphConnector(egress_connector_id("entryway"), "entryway", OUTSIDE)
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"entryway"}), connectors=(connector,)))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.front_door", "off")
    await hass.async_block_till_done()
    # Entryway already occupied, so the door signal should read as a departure.
    engine.process_signal(
        AreaActivitySignal("entryway", dt_util.utcnow(), source="binary_sensor.entryway_motion")
    )

    topology = TopologyData(egress_points=(EgressPoint("entryway", ("binary_sensor.front_door",)),))
    ingestion.async_start(topology)

    hass.states.async_set("binary_sensor.front_door", "on")
    await hass.async_block_till_done()

    assert engine.area_state("entryway", dt_util.utcnow()).occupant_count == 0


async def test_async_stop_unsubscribes(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    ingestion.async_start(topology)
    ingestion.async_stop()

    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()

    assert engine.area_state("kitchen", dt_util.utcnow()).occupant_count == 0


async def test_automation_caused_activity_is_suppressed_entirely(hass: HomeAssistant) -> None:
    """SPEC.md §6.6: automation-caused changes never become occupancy evidence."""
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    ingestion.async_start(topology)

    automation_context = Context(id="automation-ctx-1")
    hass.bus.async_fire(EVENT_AUTOMATION_TRIGGERED, {}, context=automation_context)
    await hass.async_block_till_done()

    hass.states.async_set("binary_sensor.kitchen_motion", "on", context=automation_context)
    await hass.async_block_till_done()

    assert engine.area_state("kitchen", dt_util.utcnow()).occupant_count == 0


async def test_user_caused_activity_is_tagged_user_confirmed(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    ingestion.async_start(topology)

    hass.states.async_set("binary_sensor.kitchen_motion", "on", context=Context(user_id="user-abc"))
    await hass.async_block_till_done()

    state = engine.area_state("kitchen", dt_util.utcnow())
    assert state.occupant_count == 1
    assert state.last_provenance is ProvenanceTier.USER_CONFIRMED


async def test_contextless_activity_is_tagged_ambiguous_physical(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "off")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    ingestion.async_start(topology)

    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()

    state = engine.area_state("kitchen", dt_util.utcnow())
    assert state.occupant_count == 1
    assert state.last_provenance is ProvenanceTier.AMBIGUOUS_PHYSICAL
