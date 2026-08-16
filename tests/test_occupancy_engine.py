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
    """A gap well beyond even the scored-timing grace zone (docs/DECISIONS.md's
    "scored transit timing" entry) reads as unrelated, not a transfer — the
    graceful tapering near the window's edge is still a *bounded* grace
    period, not an unlimited one.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=90))
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    # Default transit_grace_fraction=0.5 means plausibility hits zero at
    # 90s * 1.5 = 135s — well past that.
    long_after = T0 + timedelta(seconds=200)
    engine.process_signal(AreaActivitySignal("hallway", long_after, source="s2"))

    assert engine.area_state("kitchen", long_after).occupant_count == 1
    assert engine.area_state("hallway", long_after).occupant_count == 1
    assert engine.total_occupant_count(long_after) == 2


def test_direct_transit_still_inferred_just_past_the_window_at_reduced_plausibility() -> None:
    """The graceful-tapering half of "scored transit timing"
    (docs/DECISIONS.md): a walk that finishes *just* past the window — the
    exact shape of the real overnight walk-test bug — now still resolves as
    a continued transit instead of falling off a cliff into "must be a new
    person" the instant the window elapses.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=90))
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    just_past = T0 + timedelta(seconds=91)  # 1s past a 90s window
    engine.process_signal(AreaActivitySignal("hallway", just_past, source="s2"))

    assert engine.area_state("kitchen", just_past).occupant_count == 0
    assert engine.area_state("hallway", just_past).occupant_count == 1
    assert engine.total_occupant_count(just_past) == 1


def test_transit_score_tie_within_the_grace_zone_stays_ambiguous() -> None:
    """Two candidates whose scores land close together in the tapering zone
    (docs/DECISIONS.md's "scored transit timing" entry) are still an
    unresolvable tie, generalizing the old exact-tie rule to near-ties now
    that scores are continuous.
    """
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=90))
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway)), config
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))

    # Both gaps are identical (100s) once the window has elapsed — an exact
    # tie in the tapering zone, not just at full-strength score=1.0 (already
    # covered by test_direct_transit_not_inferred_with_multiple_occupied_neighbors).
    now = T0 + timedelta(seconds=100)
    engine.process_signal(AreaActivitySignal("hallway", now, source="s3"))

    assert engine.area_state("kitchen", now).occupant_count == 1  # untouched — ambiguous
    assert engine.area_state("study", now).occupant_count == 1  # untouched — ambiguous
    assert engine.area_state("hallway", now).occupant_count == 1  # new occupant, not guessed
    assert engine.total_occupant_count(now) == 3


def test_transit_score_picks_the_clearly_more_plausible_candidate() -> None:
    """When one candidate is comfortably more plausible than the other (not
    just barely), scoring resolves it instead of staying ambiguous.
    """
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    config = EngineConfig(transit_confirmation_window=timedelta(seconds=90))
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway)), config
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    # study is refreshed later — already occupied by this point, so this just
    # updates its own last-confirmed timestamp without re-triggering a
    # source search (which would otherwise itself resolve against kitchen).
    engine.process_signal(AreaActivitySignal("study", T0 + timedelta(seconds=40), source="s2"))

    now = T0 + timedelta(seconds=100)  # kitchen gap=100s (tapering); study gap=60s (in-window)
    engine.process_signal(AreaActivitySignal("hallway", now, source="s3"))

    assert engine.area_state("kitchen", now).occupant_count == 1  # untouched
    assert engine.area_state("study", now).occupant_count == 0  # drained — the real source
    assert engine.area_state("hallway", now).occupant_count == 1
    assert engine.total_occupant_count(now) == 2


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


# -- egress_anchor_total / whole-house conservation (docs/DECISIONS.md) ------


def test_egress_anchor_starts_unanchored() -> None:
    """No door/phone evidence has ever happened yet — a fresh install with
    people already home must not be flagged as suspicious.
    """
    engine = OccupancyEngine(_graph(("kitchen",)))

    assert engine.egress_anchor_total is None


def test_egress_anchor_established_by_a_confirmed_departure() -> None:
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    engine = OccupancyEngine(_graph(("entryway",), (connector,)))
    engine.process_signal(AreaActivitySignal("entryway", T0, source="s1"))
    assert engine.egress_anchor_total is None  # still no door evidence yet

    left = T0 + timedelta(seconds=5)
    engine.process_signal(
        ConnectorActivitySignal("front_door", left, source="binary_sensor.front_door")
    )

    assert engine.egress_anchor_total == 0
    assert engine.total_occupant_count(left) == 0


def test_egress_anchor_established_by_a_confirmed_arrival() -> None:
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    engine = OccupancyEngine(_graph(("entryway",), (connector,)))
    assert engine.egress_anchor_total is None

    engine.process_signal(
        ConnectorActivitySignal("front_door", T0, source="binary_sensor.front_door")
    )
    corroborated = T0 + timedelta(seconds=5)
    engine.process_signal(
        AreaActivitySignal("entryway", corroborated, source="binary_sensor.entryway_motion")
    )

    assert engine.egress_anchor_total == 1
    assert engine.total_occupant_count(corroborated) == 1


def test_egress_anchor_unaffected_by_an_ordinary_interior_transit() -> None:
    front_door = GraphConnector("front_door", "entryway", OUTSIDE)
    hallway_link = GraphConnector("c1", "entryway", "hallway")
    engine = OccupancyEngine(_graph(("entryway", "hallway"), (front_door, hallway_link)))
    engine.process_signal(
        ConnectorActivitySignal("front_door", T0, source="binary_sensor.front_door")
    )
    engine.process_signal(AreaActivitySignal("entryway", T0 + timedelta(seconds=5), source="s1"))
    assert engine.egress_anchor_total == 1

    moved = T0 + timedelta(seconds=10)
    engine.process_signal(AreaActivitySignal("hallway", moved, source="s2"))

    assert engine.egress_anchor_total == 1  # unchanged — an interior handoff, not a door crossing
    assert engine.total_occupant_count(moved) == 1  # still matches: nothing unexplained


def test_egress_anchor_diverges_from_an_ungrounded_interior_birth() -> None:
    """The exact bug shape this anchor exists to surface: an interior signal
    with no plausible source creates a brand-new occupant with no door
    crossing behind it at all.
    """
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    engine = OccupancyEngine(_graph(("entryway", "kitchen"), (connector,)))
    engine.process_signal(
        ConnectorActivitySignal("front_door", T0, source="binary_sensor.front_door")
    )
    engine.process_signal(AreaActivitySignal("entryway", T0 + timedelta(seconds=5), source="s1"))
    assert engine.egress_anchor_total == 1

    later = T0 + timedelta(hours=1)  # disconnected from entryway, no plausible source
    engine.process_signal(AreaActivitySignal("kitchen", later, source="s2"))

    assert engine.egress_anchor_total == 1  # untouched — no door crossing happened
    assert engine.total_occupant_count(later) == 2  # interior model now believes 2: divergence


def test_egress_anchor_unaffected_by_an_arrival_reattributed_to_a_neighbor() -> None:
    """A door confirmation that turns out to be the same person already
    counted in an adjacent Area (docs/DECISIONS.md's 2026-08-15 "egress-
    arrival confirmation" entry) isn't a genuinely new arrival from OUTSIDE —
    the anchor must stay untouched (and unestablished here, since this is
    the only door-adjacent event in the scenario).
    """
    front_door = GraphConnector("front_door", "entryway", OUTSIDE)
    yard_link = GraphConnector("c1", "front_yard", "entryway")
    engine = OccupancyEngine(_graph(("entryway", "front_yard"), (front_door, yard_link)))
    engine.process_signal(AreaActivitySignal("front_yard", T0, source="s1"))
    engine.process_signal(
        ConnectorActivitySignal(
            "front_door", T0 + timedelta(seconds=2), source="binary_sensor.front_door"
        )
    )
    corroborated = T0 + timedelta(seconds=4)
    engine.process_signal(AreaActivitySignal("entryway", corroborated, source="s2"))

    assert engine.area_state("entryway", corroborated).occupant_count == 1
    assert engine.area_state("front_yard", corroborated).occupant_count == 0
    assert engine.egress_anchor_total is None


def test_override_occupant_count_does_not_touch_the_egress_anchor() -> None:
    """Deliberate: the anchor only ever moves on a confirmed door crossing —
    see `override_occupant_count`'s own docstring for why a manual
    correction doesn't adjust it either way.
    """
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    engine = OccupancyEngine(_graph(("entryway", "kitchen"), (connector,)))
    engine.process_signal(
        ConnectorActivitySignal("front_door", T0, source="binary_sensor.front_door")
    )
    engine.process_signal(AreaActivitySignal("entryway", T0 + timedelta(seconds=5), source="s1"))
    assert engine.egress_anchor_total == 1

    engine.override_occupant_count("kitchen", 1, T0 + timedelta(minutes=1))

    assert engine.egress_anchor_total == 1  # unchanged
    assert engine.total_occupant_count(T0 + timedelta(minutes=1)) == 2  # now unexplained by doors


def test_override_occupant_count_does_not_establish_an_unset_egress_anchor() -> None:
    engine = OccupancyEngine(_graph(("kitchen",)))
    assert engine.egress_anchor_total is None

    engine.override_occupant_count("kitchen", 1, T0)

    assert engine.egress_anchor_total is None


def test_expire_vacant_area_does_not_touch_the_egress_anchor() -> None:
    connector = GraphConnector("front_door", "entryway", OUTSIDE)
    graph = HouseGraph(
        area_ids=frozenset({"entryway", "landing"}),
        connectors=(connector,),
        decay_eligible_area_ids=frozenset({"landing"}),
    )
    engine = OccupancyEngine(graph)
    engine.process_signal(
        ConnectorActivitySignal("front_door", T0, source="binary_sensor.front_door")
    )
    engine.process_signal(AreaActivitySignal("entryway", T0 + timedelta(seconds=5), source="s1"))
    assert engine.egress_anchor_total == 1

    engine.process_signal(AreaActivitySignal("landing", T0 + timedelta(hours=1), source="s2"))
    assert engine.total_occupant_count(T0 + timedelta(hours=1)) == 2  # ungrounded, unexplained

    cleared_at = T0 + timedelta(hours=1, minutes=5)
    engine.expire_vacant_area("landing", cleared_at)

    assert engine.egress_anchor_total == 1  # unchanged throughout
    assert engine.total_occupant_count(cleared_at) == 1  # back in sync with the anchor


# -- Uncertain births (docs/DECISIONS.md's "uncertain births" entry) ---------


def test_unambiguous_new_occupant_creates_no_uncertain_birth() -> None:
    """No plausible source at all -- a plain, unambiguous new occupant,
    exactly as before this feature existed. Nothing to fork.
    """
    engine = OccupancyEngine(_graph(("kitchen",)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))

    assert engine.uncertain_births(T0) == ()


def test_clean_transit_creates_no_uncertain_birth() -> None:
    """A single, unambiguous candidate resolves as a normal transit -- no
    tie, so nothing to fork either.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    now = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", now, source="s2"))

    assert engine.uncertain_births(now) == ()


def test_ambiguous_new_occupant_creates_an_uncertain_birth() -> None:
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway))
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    now = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", now, source="s3"))

    births = engine.uncertain_births(now)
    assert len(births) == 1
    assert births[0].area_id == "hallway"
    assert births[0].candidate_area_ids == frozenset({"kitchen", "study"})
    assert births[0].created_at == now


def test_uncertain_birth_resolves_to_the_untouched_candidate_after_the_delay() -> None:
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    config = EngineConfig(uncertain_birth_resolution_delay=timedelta(minutes=30))
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway)), config
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    fork_time = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", fork_time, source="s3"))
    assert engine.total_occupant_count(fork_time) == 3

    after_delay = fork_time + timedelta(minutes=31)

    assert engine.total_occupant_count(after_delay) == 2  # resolved back to one continuous transit
    assert engine.area_state("hallway", after_delay).occupant_count == 1
    assert engine.uncertain_births(after_delay) == ()
    # One of kitchen/study was drained (whichever the tie-break picked) —
    # deliberately not asserting *which*, since either is an equally correct
    # resolution of a genuine, symmetric tie.
    kitchen_count = engine.area_state("kitchen", after_delay).occupant_count
    study_count = engine.area_state("study", after_delay).occupant_count
    assert {kitchen_count, study_count} == {0, 1}


def test_uncertain_birth_not_resolved_before_the_delay_elapses() -> None:
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    config = EngineConfig(uncertain_birth_resolution_delay=timedelta(minutes=30))
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway)), config
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    fork_time = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", fork_time, source="s3"))

    just_before = fork_time + timedelta(minutes=29, seconds=59)

    assert engine.total_occupant_count(just_before) == 3  # still unresolved
    assert len(engine.uncertain_births(just_before)) == 1


def test_uncertain_birth_does_not_reattribute_if_a_candidate_was_touched_since_the_fork() -> None:
    """Both original candidates get fresh, independent evidence of their own
    after the fork — real, ongoing activity, not a stale guess — so neither
    is safe to silently reattribute to. The birth is dropped from tracking
    without changing any count, leaving both as genuinely separate occupants.
    """
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    config = EngineConfig(uncertain_birth_resolution_delay=timedelta(minutes=30))
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway)), config
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    fork_time = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", fork_time, source="s3"))

    refreshed = fork_time + timedelta(seconds=10)
    engine.process_signal(AreaActivitySignal("kitchen", refreshed, source="s1"))
    engine.process_signal(AreaActivitySignal("study", refreshed, source="s2"))

    after_delay = fork_time + timedelta(minutes=31)

    assert engine.total_occupant_count(after_delay) == 3  # left alone, not silently merged
    assert engine.uncertain_births(after_delay) == ()  # dropped from tracking, not resolved


def test_uncertain_birth_dropped_silently_if_its_own_area_already_emptied() -> None:
    """If the ambiguous new occupant's own Area is cleared some other way
    before resolution (e.g. a manual correction, or they left again) before
    the delay elapses, there's nothing left to resolve — dropped, no
    reattribution attempted.
    """
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    config = EngineConfig(uncertain_birth_resolution_delay=timedelta(minutes=30))
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway)), config
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    fork_time = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", fork_time, source="s3"))

    engine.override_occupant_count("hallway", 0, fork_time + timedelta(seconds=10))
    # The override itself should have already discarded the birth record —
    # confirmed separately below — but this also proves the lazy resolver
    # doesn't error or misbehave if it somehow still saw a zeroed Area.
    after_delay = fork_time + timedelta(minutes=31)

    assert engine.uncertain_births(after_delay) == ()
    assert engine.area_state("kitchen", after_delay).occupant_count == 1  # untouched
    assert engine.area_state("study", after_delay).occupant_count == 1  # untouched


def test_uncertain_births_are_capped() -> None:
    """Bounded memory (docs/ARCHITECTURE.md) — the oldest is dropped rather
    than letting the list grow without limit.
    """
    config = EngineConfig(max_uncertain_births=2)
    area_ids: list[str] = []
    connectors: list[GraphConnector] = []
    for i in range(3):
        kitchen, study, hallway = f"kitchen{i}", f"study{i}", f"hallway{i}"
        area_ids += [kitchen, study, hallway]
        connectors += [
            GraphConnector(f"ck{i}", kitchen, hallway),
            GraphConnector(f"cs{i}", study, hallway),
        ]
    engine = OccupancyEngine(
        HouseGraph(area_ids=frozenset(area_ids), connectors=tuple(connectors)), config
    )

    now = T0 + timedelta(seconds=5)
    for i in range(3):
        kitchen, study, hallway = f"kitchen{i}", f"study{i}", f"hallway{i}"
        engine.process_signal(AreaActivitySignal(kitchen, T0, source="s1"))
        engine.process_signal(AreaActivitySignal(study, T0, source="s2"))
        engine.process_signal(AreaActivitySignal(hallway, now, source="s3"))

    assert len(engine.uncertain_births(now)) == 2


def test_override_occupant_count_discards_a_referencing_uncertain_birth() -> None:
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway))
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    now = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", now, source="s3"))
    assert len(engine.uncertain_births(now)) == 1

    engine.override_occupant_count("hallway", 1, now)  # user confirms it manually

    assert engine.uncertain_births(now) == ()


def test_override_occupant_count_discards_a_birth_referencing_it_as_a_candidate() -> None:
    """A manual correction to one of the *candidate* Areas (not the ambiguous
    new occupant itself) also supersedes the automatic guess about it.
    """
    kitchen_hallway = GraphConnector("c1", "kitchen", "hallway")
    study_hallway = GraphConnector("c2", "study", "hallway")
    engine = OccupancyEngine(
        _graph(("kitchen", "study", "hallway"), (kitchen_hallway, study_hallway))
    )
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("study", T0, source="s2"))
    now = T0 + timedelta(seconds=5)
    engine.process_signal(AreaActivitySignal("hallway", now, source="s3"))
    assert len(engine.uncertain_births(now)) == 1

    engine.override_occupant_count("kitchen", 0, now)  # e.g. user confirms kitchen is empty

    assert engine.uncertain_births(now) == ()


# -- Learned transit timing (docs/DECISIONS.md's "learned transit timing" entry) --


def _walk_kitchen_to_hallway(engine: OccupancyEngine, start: datetime, gap_seconds: float) -> None:
    """One clean, unambiguous, whole-house-count-of-1 kitchen -> hallway
    transit, learnable per docs/DECISIONS.md — settle in kitchen, then
    (fully departing so the pair starts back at 0 occupants) arrive in
    hallway `gap_seconds` later.
    """
    engine.process_signal(AreaActivitySignal("kitchen", start, source="s1"))
    engine.process_signal(
        AreaActivitySignal("hallway", start + timedelta(seconds=gap_seconds), source="s2")
    )
    # Walk back so the next call starts from a clean, single-occupant slate.
    engine.override_occupant_count("hallway", 0, start + timedelta(seconds=gap_seconds + 1))


def test_learned_transit_times_starts_empty() -> None:
    engine = OccupancyEngine(_graph(("kitchen", "hallway")))

    assert engine.learned_transit_times() == {}


def test_no_sample_recorded_when_more_than_one_occupant_in_the_house() -> None:
    """docs/DECISIONS.md: only a whole-house-count-of-1 transit is
    unambiguous enough to learn from.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway", "study"), (connector,)))
    engine.process_signal(AreaActivitySignal("study", T0, source="s0"))  # a second occupant
    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    engine.process_signal(AreaActivitySignal("hallway", T0 + timedelta(seconds=10), source="s2"))

    assert engine.learned_transit_times() == {}


def test_learned_transit_times_accumulates_clean_samples() -> None:
    connector = GraphConnector("c1", "kitchen", "hallway")
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)))
    t = T0
    for _ in range(3):
        _walk_kitchen_to_hallway(engine, t, gap_seconds=10.0)
        t += timedelta(minutes=1)

    learned = engine.learned_transit_times()
    assert len(learned) == 1
    count, mean_seconds, _m2 = next(iter(learned.values()))
    assert count == 3
    assert mean_seconds == pytest.approx(10.0)


def test_effective_window_stays_generic_below_the_learning_threshold() -> None:
    """Fewer than transit_learning_min_samples (default 5) real
    observations — nothing to trust yet, the flat formula still applies.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(
        transit_confirmation_window=timedelta(seconds=90), transit_learning_min_samples=5
    )
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    t = T0
    for _ in range(4):  # one short of the threshold
        _walk_kitchen_to_hallway(engine, t, gap_seconds=10.0)
        t += timedelta(minutes=1)

    # A gap far beyond the learned mean (10s) but still within the flat 90s
    # window must still resolve as a continued transit — proving the flat
    # formula, not a premature learned one, is still in effect.
    engine.process_signal(AreaActivitySignal("kitchen", t, source="s1"))
    arrival = t + timedelta(seconds=80)
    engine.process_signal(AreaActivitySignal("hallway", arrival, source="s2"))

    assert engine.area_state("hallway", arrival).occupant_count == 1
    assert engine.area_state("kitchen", arrival).occupant_count == 0


def test_effective_window_tightens_below_the_generic_default_once_learned() -> None:
    """The core "fully replace, including tightening" behavior: once
    kitchen<->hallway has enough learned samples clustered tightly around a
    short real time, a gap that the flat 90s default would have accepted is
    now correctly read as implausible for *this* pair — someone else,
    unrelated, not a continuation of the same short hop.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(
        transit_confirmation_window=timedelta(seconds=90),
        transit_learning_min_samples=5,
        transit_learning_stddev_margin=2.0,
    )
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    t = T0
    for _ in range(6):  # comfortably past the threshold, tight/consistent timing
        _walk_kitchen_to_hallway(engine, t, gap_seconds=5.0)
        t += timedelta(minutes=1)

    # 60s is well inside the flat 90s window, but far past a learned
    # ~5s-typical, tightly-consistent pair's own effective window.
    engine.process_signal(AreaActivitySignal("kitchen", t, source="s1"))
    arrival = t + timedelta(seconds=60)
    engine.process_signal(AreaActivitySignal("hallway", arrival, source="s2"))

    assert engine.area_state("kitchen", arrival).occupant_count == 1  # untouched
    assert engine.area_state("hallway", arrival).occupant_count == 1  # a new, unrelated occupant
    assert engine.total_occupant_count(arrival) == 2


def test_effective_window_widens_beyond_the_generic_default_once_learned() -> None:
    """The flip side: a pair with genuinely *variable* real transit times
    (sometimes quick, sometimes someone dawdles) gets a wider learned
    window (mean + a safety margin of standard deviations) than a flat
    default tuned only for the "usual" case — catching a slow-but-genuine
    transit the flat formula alone wouldn't have. Each individual bootstrap
    sample still has to resolve under the *flat* window first (learning
    can only observe transits the engine already recognized), so this uses
    realistic variance rather than an oversized mean to demonstrate
    widening without that chicken-and-egg problem.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(
        transit_confirmation_window=timedelta(seconds=70),
        transit_grace_fraction=0.0,  # isolate the learned-window effect
        transit_learning_min_samples=5,
        transit_learning_stddev_margin=2.0,
    )
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config)
    t = T0
    # Alternating quick/slow genuine walks — each individual gap comfortably
    # resolves under the flat 70s window, but the resulting mean (35s) plus
    # 2 standard deviations (~27.4s) works out to a learned window (~89.8s)
    # wider than the flat default alone ever was.
    for gap in (10.0, 60.0, 10.0, 60.0, 10.0, 60.0):
        _walk_kitchen_to_hallway(engine, t, gap_seconds=gap)
        t += timedelta(minutes=5)

    engine.process_signal(AreaActivitySignal("kitchen", t, source="s1"))
    arrival = t + timedelta(seconds=85)  # past the flat 70s window, within the learned ~89.8s one
    engine.process_signal(AreaActivitySignal("hallway", arrival, source="s2"))

    assert engine.area_state("kitchen", arrival).occupant_count == 0  # correctly resolved
    assert engine.area_state("hallway", arrival).occupant_count == 1
    assert engine.total_occupant_count(arrival) == 1


def test_learned_transit_times_can_be_seeded_at_construction() -> None:
    """Feeding a prior snapshot back in (docs/DECISIONS.md — how the
    HA-dependent persistence layer resumes learning after a restart)
    reproduces the same learned behavior without needing to relearn.
    """
    connector = GraphConnector("c1", "kitchen", "hallway")
    config = EngineConfig(
        transit_confirmation_window=timedelta(seconds=90), transit_learning_min_samples=5
    )
    seed = {frozenset({"kitchen", "hallway"}): (6, 5.0, 0.0)}  # 6 samples, mean 5s, zero variance
    engine = OccupancyEngine(_graph(("kitchen", "hallway"), (connector,)), config, seed)

    assert engine.learned_transit_times() == seed

    engine.process_signal(AreaActivitySignal("kitchen", T0, source="s1"))
    far_past_learned = T0 + timedelta(seconds=60)  # within the flat window, past the learned one
    engine.process_signal(AreaActivitySignal("hallway", far_past_learned, source="s2"))

    assert engine.area_state("kitchen", far_past_learned).occupant_count == 1  # untouched
    assert engine.area_state("hallway", far_past_learned).occupant_count == 1  # new, unrelated
