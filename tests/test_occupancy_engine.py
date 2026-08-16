"""Scenario tests for the occupancy engine (docs/SPEC.md §6.2-§6.5).

Pure Python, no Home Assistant dependency — per docs/TESTING.md layer 1,
these should run in well under a second and don't need
pytest-homeassistant-custom-component at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.occupancy_tracker.occupancy_engine import (
    OUTSIDE,
    AreaActivitySignal,
    ConnectorActivitySignal,
    EngineConfig,
    GraphConnector,
    HouseGraph,
    OccupancyEngine,
    ProvenanceTier,
    StateQuality,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _graph(area_ids: tuple[str, ...], connectors: tuple[GraphConnector, ...] = ()) -> HouseGraph:
    return HouseGraph(area_ids=frozenset(area_ids), connectors=connectors)


def test_new_engine_starts_empty_everywhere() -> None:
    engine = OccupancyEngine(_graph(("kitchen", "hallway")))

    kitchen = engine.area_state("kitchen", T0)

    assert kitchen.occupant_count == 0
    assert kitchen.last_confirmed is None
    assert engine.total_occupant_count(T0) == 0


def test_area_state_rejects_unknown_area() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    with pytest.raises(ValueError, match="Unknown area"):
        engine.area_state("attic", T0)


def test_area_activity_in_empty_area_seeds_one_occupant() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    state = engine.area_state("kitchen", T0)
    assert state.occupant_count == 1
    assert state.quality == StateQuality.CONFIRMED
    assert state.last_confirmed == T0


def test_further_activity_in_occupied_area_does_not_add_a_second_occupant() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    later = T0 + timedelta(seconds=30)
    engine.process_signal(AreaActivitySignal("kitchen", later, source="binary_sensor.motion"))

    state = engine.area_state("kitchen", later)
    assert state.occupant_count == 1
    assert state.last_confirmed == later  # evidence refreshed


def test_latching_through_a_quiet_period_keeps_the_count() -> None:
    """SPEC.md §6.2: absence of signal never decays the count."""
    engine = OccupancyEngine(_graph(("kitchen",)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    much_later = T0 + timedelta(minutes=60)
    state = engine.area_state("kitchen", much_later)

    assert state.occupant_count == 1  # never decayed
    assert state.quality == StateQuality.LATCHED  # but freshness label degraded


def test_quality_degrades_from_confirmed_to_latched_after_freshness_window() -> None:
    config = EngineConfig(confirmed_freshness_window=timedelta(minutes=10))
    engine = OccupancyEngine(_graph(("kitchen",)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    just_inside = T0 + timedelta(minutes=9, seconds=59)
    just_outside = T0 + timedelta(minutes=10, seconds=1)

    assert engine.area_state("kitchen", just_inside).quality == StateQuality.CONFIRMED
    assert engine.area_state("kitchen", just_outside).quality == StateQuality.LATCHED


def test_direct_transit_inferred_from_adjacency_with_no_connector_sensor() -> None:
    """Most Connectors have no sensor of their own (SPEC.md §7.3 only lets users bind
    entities to egress points, not ordinary Connector edges) — destination activity
    plus topology adjacency alone must be enough to move an occupant token rather
    than always reading as a brand-new occupant.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.kitchen_motion"))

    moved = T0 + timedelta(seconds=5)
    engine.process_signal(
        AreaActivitySignal("hallway", moved, source="binary_sensor.hallway_motion")
    )

    kitchen = engine.area_state("kitchen", moved)
    hallway = engine.area_state("hallway", moved)
    assert kitchen.occupant_count == 0
    assert hallway.occupant_count == 1
    assert hallway.quality == StateQuality.CONFIRMED  # no pending step -> confirmed immediately
    assert engine.total_occupant_count(moved) == 1


def test_direct_transit_not_inferred_with_multiple_occupied_neighbors() -> None:
    """Ambiguous which neighbor the person came from -> treated as a new occupant,
    not guessed.
    """
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway))
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))

    later = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", later, source="s3"))

    assert engine.area_state("kitchen", later).occupant_count == 1
    assert engine.area_state("study", later).occupant_count == 1
    assert engine.area_state("hallway", later).occupant_count == 1
    assert engine.total_occupant_count(later) == 3


def test_direct_transit_not_inferred_with_no_occupied_neighbors() -> None:
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))  # kitchen empty

    engine.process_signal(AreaActivitySignal("hallway", T0, source="binary_sensor.hallway_motion"))

    assert engine.area_state("kitchen", T0).occupant_count == 0
    assert engine.area_state("hallway", T0).occupant_count == 1
    assert engine.total_occupant_count(T0) == 1


def test_direct_transit_not_inferred_when_gap_too_short_to_be_the_same_person() -> None:
    """Two sensors firing near-simultaneously can't be one person walking between
    rooms -- they can't teleport -- so this reads as a second, independent occupant.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(min_transit_time=timedelta(seconds=2))
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    almost_simultaneous = T0 + timedelta(milliseconds=500)
    engine.process_signal(AreaActivitySignal("hallway", almost_simultaneous, source="s2"))

    assert engine.area_state("kitchen", almost_simultaneous).occupant_count == 1
    assert engine.area_state("hallway", almost_simultaneous).occupant_count == 1
    assert engine.total_occupant_count(almost_simultaneous) == 2


def test_direct_transit_inferred_at_exactly_the_minimum_plausible_gap() -> None:
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(min_transit_time=timedelta(seconds=2))
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    exactly_min_gap = T0 + timedelta(seconds=2)
    engine.process_signal(AreaActivitySignal("hallway", exactly_min_gap, source="s2"))

    assert engine.area_state("kitchen", exactly_min_gap).occupant_count == 0
    assert engine.area_state("hallway", exactly_min_gap).occupant_count == 1
    assert engine.total_occupant_count(exactly_min_gap) == 1


def test_direct_transit_not_inferred_when_source_evidence_is_stale() -> None:
    """A gap longer than the transit window reads as unrelated, not a transfer."""
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=90))
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    long_after = T0 + timedelta(seconds=91)
    engine.process_signal(AreaActivitySignal("hallway", long_after, source="s2"))

    assert engine.area_state("kitchen", long_after).occupant_count == 1
    assert engine.area_state("hallway", long_after).occupant_count == 1
    assert engine.total_occupant_count(long_after) == 2


def test_confirmed_multi_room_transit() -> None:
    """A Connector that *does* have a sensor bound to it (SPEC.md §6.3) still goes
    through the candidate-evidence -> pending -> corroboration cycle, distinct from
    the sensor-less-Connector direct-adjacency path above.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.kitchen_motion"))

    fired = T0 + timedelta(seconds=5)
    engine.process_signal(
        ConnectorActivitySignal("c1", fired, source="binary_sensor.hallway_motion")
    )
    assert engine.area_state("kitchen", fired).quality == StateQuality.AMBIGUOUS
    assert engine.area_state("hallway", fired).quality == StateQuality.AMBIGUOUS

    corroborated = fired + timedelta(seconds=10)
    engine.process_signal(
        AreaActivitySignal("hallway", corroborated, source="binary_sensor.hallway_motion")
    )

    hallway = engine.area_state("hallway", corroborated)
    assert engine.area_state("kitchen", corroborated).occupant_count == 0
    assert hallway.occupant_count == 1
    assert hallway.quality == StateQuality.CONFIRMED
    assert engine.total_occupant_count(corroborated) == 1


def test_unconfirmed_transit_leaves_occupant_in_source_area() -> None:
    """SPEC.md §6.3: no corroborating Connector activity -> occupant assumed to stay put."""
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(
        transit_confirmation_window=timedelta(seconds=30),
        confirmed_freshness_window=timedelta(seconds=10),
    )
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.kitchen_motion"))

    fired = T0 + timedelta(seconds=5)
    engine.process_signal(
        ConnectorActivitySignal("c1", fired, source="binary_sensor.hallway_motion")
    )

    after_timeout = fired + timedelta(seconds=31)
    kitchen = engine.area_state("kitchen", after_timeout)
    hallway = engine.area_state("hallway", after_timeout)

    assert kitchen.occupant_count == 1
    assert hallway.occupant_count == 0
    assert kitchen.quality == StateQuality.LATCHED  # ambiguity resolved, back to latched
    assert engine.pending_transit_connector_ids(after_timeout) == frozenset()


def test_connector_activity_with_both_sides_occupied_is_inconclusive() -> None:
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("hallway", T0, source="s2"))

    fired = T0 + timedelta(seconds=5)
    engine.process_signal(ConnectorActivitySignal("c1", fired, source="s3"))

    assert engine.pending_transit_connector_ids(fired) == frozenset()
    assert engine.area_state("kitchen", fired).occupant_count == 1
    assert engine.area_state("hallway", fired).occupant_count == 1


def test_connector_activity_with_both_sides_empty_is_inconclusive() -> None:
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))

    engine.process_signal(ConnectorActivitySignal("c1", T0, source="s1"))

    assert engine.pending_transit_connector_ids(T0) == frozenset()
    assert engine.total_occupant_count(T0) == 0


def test_two_simultaneous_disconnected_signals_infer_a_second_occupant() -> None:
    """SPEC.md §6.4: unbounded, evidence-driven multi-occupant disambiguation."""
    engine = OccupancyEngine(_graph(("kitchen", "study")))  # not connected to each other

    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))

    assert engine.total_occupant_count(T0) == 2
    assert engine.area_state("kitchen", T0).occupant_count == 1
    assert engine.area_state("study", T0).occupant_count == 1


def test_egress_departure_confirms_immediately() -> None:
    """SPEC.md §6.5: egress activity from an occupied Area confirms without a pending window."""
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    engine = OccupancyEngine(_graph(("entryway",), (connector,)))
    engine.process_signal(
        AreaActivitySignal("entryway", T0, source="binary_sensor.entryway_motion")
    )

    left = T0 + timedelta(seconds=5)
    engine.process_signal(
        ConnectorActivitySignal("front_door", left, source="binary_sensor.front_door")
    )

    entryway = engine.area_state("entryway", left)
    assert entryway.occupant_count == 0
    assert entryway.quality == StateQuality.CONFIRMED  # confirmed departure, not left ambiguous
    assert engine.pending_transit_connector_ids(left) == frozenset()


def test_egress_arrival_requires_corroboration() -> None:
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=60))
    engine = OccupancyEngine(_graph(("entryway",), (connector,)), config)

    opened = T0
    engine.process_signal(
        ConnectorActivitySignal("front_door", opened, source="binary_sensor.front_door")
    )
    assert engine.area_state("entryway", opened).quality == StateQuality.AMBIGUOUS
    assert engine.area_state("entryway", opened).occupant_count == 0

    corroborated = opened + timedelta(seconds=10)
    engine.process_signal(
        AreaActivitySignal("entryway", corroborated, source="binary_sensor.entryway_motion")
    )

    entryway = engine.area_state("entryway", corroborated)
    assert entryway.occupant_count == 1
    assert entryway.quality == StateQuality.CONFIRMED


def test_egress_arrival_without_corroboration_does_not_add_an_occupant() -> None:
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=30))
    engine = OccupancyEngine(_graph(("entryway",), (connector,)), config)

    engine.process_signal(
        ConnectorActivitySignal("front_door", T0, source="binary_sensor.front_door")
    )

    after_timeout = T0 + timedelta(seconds=31)
    entryway = engine.area_state("entryway", after_timeout)

    assert entryway.occupant_count == 0
    assert engine.pending_transit_connector_ids(after_timeout) == frozenset()


def test_two_pending_transits_from_the_same_source_do_not_double_drain_it() -> None:
    """A source Area can only actually lose the one occupant that leaves it."""
    to_hallway = GraphConnector("c1", "kitchen", "hallway")
    to_study = GraphConnector("c2", "kitchen", "study")
    engine = OccupancyEngine(_graph(("kitchen", "hallway", "study"), (to_hallway, to_study)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s0"))

    fired = T0 + timedelta(seconds=1)
    engine.process_signal(ConnectorActivitySignal("c1", fired, source="s1"))
    engine.process_signal(ConnectorActivitySignal("c2", fired, source="s2"))

    confirm_hallway = fired + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", confirm_hallway, source="s3"))
    # kitchen is now drained to 0; study's pending transit is now stale.
    confirm_study = confirm_hallway + timedelta(seconds=1)
    engine.process_signal(AreaActivitySignal("study", confirm_study, source="s4"))

    assert engine.area_state("kitchen", confirm_study).occupant_count == 0
    assert engine.area_state("hallway", confirm_study).occupant_count == 1
    # study's stale pending transit was dropped rather than driving kitchen negative
    # or fabricating an ungrounded arrival.
    assert engine.area_state("study", confirm_study).occupant_count == 0
    assert engine.total_occupant_count(confirm_study) == 1


def test_connector_activity_rejects_unknown_connector() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    with pytest.raises(ValueError, match="Unknown connector"):
        engine.process_signal(ConnectorActivitySignal("nope", T0, source="s1"))


def test_all_area_states_covers_every_area() -> None:
    engine = OccupancyEngine(_graph(("kitchen", "hallway")))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    states = engine.all_area_states(T0)

    assert set(states) == {"kitchen", "hallway"}
    assert states["kitchen"].occupant_count == 1
    assert states["hallway"].occupant_count == 0


def test_listener_is_called_on_every_processed_signal() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))
    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    engine.add_listener(on_change)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    assert calls == 1


def test_listener_can_unsubscribe() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))
    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    remove = engine.add_listener(on_change)
    remove()
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    assert calls == 0


def test_new_area_has_no_provenance_yet() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    assert engine.area_state("kitchen", T0).last_provenance is None


def test_direct_evidence_records_its_provenance() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    engine.process_signal(
        AreaActivitySignal("kitchen", T0, source="s1", provenance=ProvenanceTier.AMBIGUOUS_PHYSICAL)
    )

    assert engine.area_state("kitchen", T0).last_provenance == ProvenanceTier.AMBIGUOUS_PHYSICAL


def test_confirmed_transit_records_the_confirming_signals_provenance_on_both_areas() -> None:
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    fired = T0 + timedelta(seconds=5)
    engine.process_signal(ConnectorActivitySignal("c1", fired, source="s2"))

    corroborated = fired + timedelta(seconds=10)
    engine.process_signal(
        AreaActivitySignal(
            "hallway", corroborated, source="s3", provenance=ProvenanceTier.AMBIGUOUS_PHYSICAL
        )
    )

    assert (
        engine.area_state("kitchen", corroborated).last_provenance
        == ProvenanceTier.AMBIGUOUS_PHYSICAL
    )
    assert (
        engine.area_state("hallway", corroborated).last_provenance
        == ProvenanceTier.AMBIGUOUS_PHYSICAL
    )


def test_override_occupant_count_sets_value_directly() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    engine.override_occupant_count("kitchen", 3, T0)

    state = engine.area_state("kitchen", T0)
    assert state.occupant_count == 3
    assert state.last_confirmed == T0
    assert state.last_provenance == ProvenanceTier.USER_CONFIRMED
    assert state.quality == StateQuality.CONFIRMED


def test_override_occupant_count_rejects_unknown_area() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    with pytest.raises(ValueError, match="Unknown area"):
        engine.override_occupant_count("attic", 1, T0)


def test_override_occupant_count_rejects_negative_count() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    with pytest.raises(ValueError, match="negative"):
        engine.override_occupant_count("kitchen", -1, T0)


def test_override_occupant_count_clears_pending_transit_touching_the_area() -> None:
    """A manual correction is more authoritative than an unresolved automatic
    guess about the same Area — the stale guess must not still be sitting
    there afterward, e.g. later resolving and changing counts out from under
    the override, or holding the Area's quality at AMBIGUOUS.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    fired = T0 + timedelta(seconds=5)
    engine.process_signal(ConnectorActivitySignal("c1", fired, source="s2"))
    assert engine.pending_transit_connector_ids(fired) == frozenset({"c1"})

    override_time = fired + timedelta(seconds=1)
    engine.override_occupant_count("hallway", 0, override_time)

    assert engine.pending_transit_connector_ids(override_time) == frozenset()
    assert engine.area_state("hallway", override_time).quality == StateQuality.CONFIRMED


def test_override_occupant_count_notifies_listeners() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))
    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    engine.add_listener(on_change)
    engine.override_occupant_count("kitchen", 2, T0)

    assert calls == 1


def test_household_size_hint_defaults_to_none() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    assert engine.household_size_hint is None


def test_household_size_hint_reflects_config() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)), EngineConfig(household_size_hint=4))

    assert engine.household_size_hint == 4


# -- Outdoor-Area total exclusion (docs/DECISIONS.md) ------------------------


def test_total_occupant_count_excludes_outside_areas() -> None:
    graph = HouseGraph(
        area_ids=frozenset({"kitchen", "front_yard"}), outside_area_ids=frozenset({"front_yard"})
    )
    engine = OccupancyEngine(graph)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))
    engine.process_signal(AreaActivitySignal("front_yard", T0, source="binary_sensor.motion"))

    # Both Areas are still individually tracked...
    assert engine.area_state("kitchen", T0).occupant_count == 1
    assert engine.area_state("front_yard", T0).occupant_count == 1
    # ...but only kitchen counts toward the whole-house total.
    assert engine.total_occupant_count(T0) == 1


def test_total_occupant_count_includes_everything_when_nothing_is_flagged_outside() -> None:
    engine = OccupancyEngine(_graph(("kitchen", "hallway")))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    assert engine.total_occupant_count(T0) == 1


# -- decay_grace_period property (docs/DECISIONS.md's decay entry) -----------


def test_decay_grace_period_defaults() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    assert engine.decay_grace_period == timedelta(minutes=5)


def test_decay_grace_period_reflects_config() -> None:
    engine = OccupancyEngine(
        _graph(("kitchen",)), EngineConfig(decay_grace_period=timedelta(minutes=1))
    )

    assert engine.decay_grace_period == timedelta(minutes=1)


# -- expire_vacant_area (docs/DECISIONS.md's decay entry) --------------------


def _decay_graph() -> HouseGraph:
    return HouseGraph(
        area_ids=frozenset({"landing"}), decay_eligible_area_ids=frozenset({"landing"})
    )


def test_expire_vacant_area_clears_a_decay_eligible_area() -> None:
    engine = OccupancyEngine(_decay_graph())
    engine.process_signal(AreaActivitySignal("landing", T0, source="binary_sensor.presence"))
    assert engine.area_state("landing", T0).occupant_count == 1

    later = T0 + timedelta(minutes=5)
    engine.expire_vacant_area("landing", later)

    state = engine.area_state("landing", later)
    assert state.occupant_count == 0
    assert state.last_confirmed == later


def test_expire_vacant_area_is_a_noop_for_a_non_decay_eligible_area() -> None:
    """SPEC.md §6.2's "never decay" guarantee holds for anything not
    explicitly decay-eligible — an ordinary motion-sensor Area must never be
    auto-cleared, even if this method is somehow called for it.
    """
    engine = OccupancyEngine(_graph(("kitchen",)))  # no decay_eligible_area_ids at all
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    engine.expire_vacant_area("kitchen", T0 + timedelta(minutes=5))

    assert engine.area_state("kitchen", T0 + timedelta(minutes=5)).occupant_count == 1


def test_expire_vacant_area_is_a_noop_when_already_zero() -> None:
    graph = _decay_graph()
    engine = OccupancyEngine(graph)

    engine.expire_vacant_area("landing", T0)  # already 0 — should not error

    assert engine.area_state("landing", T0).occupant_count == 0


def test_expire_vacant_area_clears_a_pending_transit_touching_the_area() -> None:
    connector = GraphConnector("c1", "landing", "office")
    graph = HouseGraph(
        area_ids=frozenset({"landing", "office"}),
        connectors=(connector,),
        decay_eligible_area_ids=frozenset({"landing"}),
    )
    engine = OccupancyEngine(graph)
    engine.process_signal(AreaActivitySignal("landing", T0, source="binary_sensor.presence"))
    # office is empty, landing occupied — a Connector-crossing event can
    # resolve a direction, registering a pending transit with landing as its
    # source (unconfirmed until office corroborates).
    engine.process_signal(ConnectorActivitySignal("c1", T0, source="binary_sensor.landing_door"))
    assert "c1" in engine.pending_transit_connector_ids(T0)

    later = T0 + timedelta(minutes=5)
    engine.expire_vacant_area("landing", later)

    assert "c1" not in engine.pending_transit_connector_ids(later)
    assert engine.area_state("landing", later).occupant_count == 0


def test_expire_vacant_area_notifies_listeners() -> None:
    graph = _decay_graph()
    engine = OccupancyEngine(graph)
    engine.process_signal(AreaActivitySignal("landing", T0, source="binary_sensor.presence"))

    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    engine.add_listener(on_change)
    engine.expire_vacant_area("landing", T0 + timedelta(minutes=5))

    assert calls == 1


# -- needs_review flag (docs/DECISIONS.md's decay entry) ---------------------


def test_needs_review_false_while_fresh() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    assert engine.area_state("kitchen", T0).needs_review is False


def test_needs_review_true_once_latched_past_the_threshold() -> None:
    config = EngineConfig(long_latched_review_threshold=timedelta(hours=12))
    engine = OccupancyEngine(_graph(("kitchen",)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="binary_sensor.motion"))

    just_inside = T0 + timedelta(hours=11, minutes=59)
    just_outside = T0 + timedelta(hours=12, minutes=1)

    assert engine.area_state("kitchen", just_inside).needs_review is False
    assert engine.area_state("kitchen", just_outside).needs_review is True


def test_needs_review_false_for_a_decay_eligible_area() -> None:
    """A decay-eligible Area relies on expire_vacant_area to self-correct —
    it should never also get flagged for manual review.
    """
    graph = _decay_graph()
    config = EngineConfig(long_latched_review_threshold=timedelta(hours=12))
    engine = OccupancyEngine(graph, config)
    engine.process_signal(AreaActivitySignal("landing", T0, source="binary_sensor.presence"))

    assert engine.area_state("landing", T0 + timedelta(hours=24)).needs_review is False


def test_needs_review_false_when_count_is_zero() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))

    assert engine.area_state("kitchen", T0 + timedelta(hours=24)).needs_review is False
