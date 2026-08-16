"""Tests for the signal ingestion layer (docs/ARCHITECTURE.md §1.3)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.occupancy_tracker.engine_adapter import egress_connector_id
from custom_components.occupancy_tracker.occupancy_engine import (
    OUTSIDE,
    AreaActivitySignal,
    EngineConfig,
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


def _decay_graph(area_id: str = "landing") -> HouseGraph:
    return HouseGraph(area_ids=frozenset({area_id}), decay_eligible_area_ids=frozenset({area_id}))


async def test_decay_timer_scheduled_once_all_decay_eligible_evidence_goes_off(
    hass: HomeAssistant,
) -> None:
    engine = OccupancyEngine(_decay_graph(), EngineConfig(decay_grace_period=timedelta(minutes=5)))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.landing_presence", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"landing": ("binary_sensor.landing_presence",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.landing_presence", "off")
        await hass.async_block_till_done()

    assert mock_call_later.call_count == 1
    args, _ = mock_call_later.call_args
    assert args[0] is hass
    assert args[1] == timedelta(minutes=5)


async def test_decay_timer_not_scheduled_for_a_non_decay_eligible_area(
    hass: HomeAssistant,
) -> None:
    engine = OccupancyEngine(HouseGraph(area_ids=frozenset({"kitchen"})))
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.kitchen_motion", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.kitchen_motion", "off")
        await hass.async_block_till_done()

    mock_call_later.assert_not_called()


async def test_decay_timer_cancelled_when_evidence_turns_back_on(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(_decay_graph())
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.landing_presence", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"landing": ("binary_sensor.landing_presence",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.landing_presence", "off")
        await hass.async_block_till_done()
        cancel = mock_call_later.return_value

        hass.states.async_set("binary_sensor.landing_presence", "on")
        await hass.async_block_till_done()

    cancel.assert_called_once()


async def test_decay_timer_firing_clears_the_area(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(_decay_graph())
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.landing_presence", "off")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"landing": ("binary_sensor.landing_presence",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.landing_presence", "on")
        await hass.async_block_till_done()
        assert engine.area_state("landing", dt_util.utcnow()).occupant_count == 1

        hass.states.async_set("binary_sensor.landing_presence", "off")
        await hass.async_block_till_done()

        fire = mock_call_later.call_args.args[2]
        fire(dt_util.utcnow())

    assert engine.area_state("landing", dt_util.utcnow()).occupant_count == 0


async def test_decay_timer_firing_does_nothing_if_reoccupied_since_scheduling(
    hass: HomeAssistant,
) -> None:
    """Guards against a stale timer callback (captured before the entity came
    back on) clearing a room that's genuinely occupied again — the fire
    callback re-checks live state rather than trusting the state at schedule
    time.
    """
    engine = OccupancyEngine(_decay_graph())
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.landing_presence", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"landing": ("binary_sensor.landing_presence",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.landing_presence", "off")
        await hass.async_block_till_done()
        fire = mock_call_later.call_args.args[2]

        # Back on before the (mocked, never-really-scheduled) timer fires.
        hass.states.async_set("binary_sensor.landing_presence", "on")
        await hass.async_block_till_done()
        assert engine.area_state("landing", dt_util.utcnow()).occupant_count == 1

        fire(dt_util.utcnow())  # the stale callback still gets invoked directly

    assert engine.area_state("landing", dt_util.utcnow()).occupant_count == 1


async def test_decay_timer_ignores_entity_going_unavailable(hass: HomeAssistant) -> None:
    """A sensor dropping offline is absence of *data*, not positive evidence
    of vacancy — must not start (or be mistaken for) a decay countdown.
    """
    engine = OccupancyEngine(_decay_graph())
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.landing_presence", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"landing": ("binary_sensor.landing_presence",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.landing_presence", "unavailable")
        await hass.async_block_till_done()

    mock_call_later.assert_not_called()


async def test_async_stop_cancels_pending_decay_timers(hass: HomeAssistant) -> None:
    engine = OccupancyEngine(_decay_graph())
    ingestion = SignalIngestion(hass, engine)
    hass.states.async_set("binary_sensor.landing_presence", "on")
    await hass.async_block_till_done()

    topology = TopologyData(area_entity_selections={"landing": ("binary_sensor.landing_presence",)})
    with patch(
        "custom_components.occupancy_tracker.signal_ingestion.async_call_later"
    ) as mock_call_later:
        ingestion.async_start(topology)
        hass.states.async_set("binary_sensor.landing_presence", "off")
        await hass.async_block_till_done()
        cancel = mock_call_later.return_value

        ingestion.async_stop()

    cancel.assert_called_once()


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
