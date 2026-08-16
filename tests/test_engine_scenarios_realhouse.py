"""Scripted, known-ground-truth walkthrough scenarios against the real house
topology (docs/SPEC.md §6.2-§6.5), on top of the existing unit-level coverage
in test_occupancy_engine.py.

Pure Python, no Home Assistant dependency — same docs/TESTING.md layer-1
tier as test_occupancy_engine.py. The house shape below mirrors the actual
topology saved by the project owner's dev instance (11 Areas, 10 Connectors,
2 real egress points — entrance_hallway and kitchen; front_yard/back_yard are
ordinary interior-adjacent Areas, not egress points themselves) rather than a
minimal made-up fixture, so these scenarios exercise the same connector
adjacency/complexity a real house actually has. Deliberately hardcoded here
rather than loaded from a live `.storage` file: the point is a portable,
committable regression scenario, not a snapshot of one machine's dev config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from custom_components.occupancy_tracker.occupancy_engine import (
    OUTSIDE,
    AreaActivitySignal,
    AreaKind,
    ConnectorActivitySignal,
    EngineConfig,
    GraphConnector,
    HouseGraph,
    OccupancyEngine,
    StateQuality,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

LIVING_ROOM = "living_room"
KITCHEN = "kitchen"
ENTRANCE_HALLWAY = "entrance_hallway"
STAIRS = "stairs"
FRONT_YARD = "front_yard"
BACK_YARD = "back_yard"
BEDROOM = "bedroom"
BEDROOM_2 = "bedroom_2"
OFFICE = "office"
LANDING = "landing"
GUEST_BATHROOM = "guest_bathroom"

CONN_LANDING_BATHROOM = "landing-guest_bathroom"
CONN_LANDING_BEDROOM_2 = "landing-bedroom_2"
CONN_LANDING_OFFICE = "landing-office"
CONN_LANDING_BEDROOM = "landing-bedroom"
CONN_STAIRS_LANDING = "stairs-landing"
CONN_HALLWAY_KITCHEN = "entrance_hallway-kitchen"
CONN_HALLWAY_LIVING_ROOM = "entrance_hallway-living_room"
CONN_HALLWAY_STAIRS = "entrance_hallway-stairs"
CONN_KITCHEN_BACKYARD = "kitchen-back_yard"
CONN_FRONTYARD_HALLWAY = "front_yard-entrance_hallway"
CONN_FRONT_DOOR = "entrance_hallway-outside"
CONN_KITCHEN_DOOR = "kitchen-outside"


def real_house_graph() -> HouseGraph:
    """Mirrors the real dev-instance topology, connector-for-connector
    (docs/STATUS.md's dev-instance section), including the two synthesized
    egress connectors `engine_adapter.build_house_graph()` would produce for
    `entrance_hallway`/`kitchen`'s real access-point entities.

    `area_kinds` mirrors what `engine_adapter.py`'s topology-shape inference
    (docs/DECISIONS.md's "area-kind classification" entry) would actually
    compute for this fixture, not a hand-picked shortcut: `stairs` is the
    only Area that's both a through-node (2 Connectors: entrance_hallway and
    landing) *and* has no activity-evidence entity selected in the real
    topology (see the "Unsensored rooms" section below) — `guest_bathroom`/
    `bedroom_2` are also unsensored but are dead ends (1 Connector each), so
    they read as ordinary (if unconfigured) ROOMs, not passages, under that
    same shape-based rule.
    """
    connectors = (
        GraphConnector(CONN_LANDING_BATHROOM, LANDING, GUEST_BATHROOM),
        GraphConnector(CONN_LANDING_BEDROOM_2, LANDING, BEDROOM_2),
        GraphConnector(CONN_LANDING_OFFICE, LANDING, OFFICE),
        GraphConnector(CONN_LANDING_BEDROOM, LANDING, BEDROOM),
        GraphConnector(CONN_STAIRS_LANDING, STAIRS, LANDING),
        GraphConnector(CONN_HALLWAY_KITCHEN, ENTRANCE_HALLWAY, KITCHEN),
        GraphConnector(CONN_HALLWAY_LIVING_ROOM, ENTRANCE_HALLWAY, LIVING_ROOM),
        GraphConnector(CONN_HALLWAY_STAIRS, ENTRANCE_HALLWAY, STAIRS),
        GraphConnector(CONN_KITCHEN_BACKYARD, KITCHEN, BACK_YARD),
        GraphConnector(CONN_FRONTYARD_HALLWAY, FRONT_YARD, ENTRANCE_HALLWAY),
        GraphConnector(CONN_FRONT_DOOR, ENTRANCE_HALLWAY, OUTSIDE),
        GraphConnector(CONN_KITCHEN_DOOR, KITCHEN, OUTSIDE),
    )
    area_ids = (
        LIVING_ROOM,
        KITCHEN,
        ENTRANCE_HALLWAY,
        STAIRS,
        FRONT_YARD,
        BACK_YARD,
        BEDROOM,
        BEDROOM_2,
        OFFICE,
        LANDING,
        GUEST_BATHROOM,
    )
    return HouseGraph(
        area_ids=frozenset(area_ids),
        connectors=connectors,
        area_kinds={STAIRS: AreaKind.TRANSIT},
    )


@dataclass(frozen=True, slots=True)
class Move:
    """One scripted signal: `area_id` for an AreaActivitySignal, or
    `connector_id` for a ConnectorActivitySignal crossing — exactly one of
    the two is set. `offset` is seconds after the scenario's start time.
    """

    offset: float
    area_id: str | None = None
    connector_id: str | None = None


def apply_moves(engine: OccupancyEngine, moves: list[Move], start: datetime = T0) -> None:
    """Fold each Move into `engine` in order, mutating it in place.

    Deliberately separate from `run_scenario` below: `OccupancyEngine` has no
    way to "rewind," so a scenario that wants to assert an *intermediate*
    belief state partway through a longer walkthrough has to apply moves in
    stages and check between them — checking `counts()` against an engine
    that already processed every later move too (there is no such thing as
    "as of an earlier time" for `occupant_count`, only `quality` reads `now`)
    would silently assert against the *final* state instead, mislabeled with
    an earlier timestamp. `run_scenario` covers the common "only the end
    state matters" case in one call; scenarios that need real checkpoints
    call this directly, once per stage.
    """
    for move in moves:
        t = start + timedelta(seconds=move.offset)
        if move.area_id is not None:
            engine.process_signal(
                AreaActivitySignal(move.area_id, t, source=f"test:{move.area_id}")
            )
        else:
            assert move.connector_id is not None
            engine.process_signal(
                ConnectorActivitySignal(move.connector_id, t, source=f"test:{move.connector_id}")
            )


def run_scenario(
    graph: HouseGraph, moves: list[Move], config: EngineConfig | None = None, start: datetime = T0
) -> OccupancyEngine:
    """Build a fresh engine and fold in every Move in order, as a single
    scripted walkthrough with known, exact timestamps — the point of this
    harness: precise control over ground truth, not observed-and-guessed
    real-world timing. Only suitable when the scenario checks the *final*
    state — see `apply_moves` for scenarios that need intermediate
    checkpoints.
    """
    engine = OccupancyEngine(graph, config)
    apply_moves(engine, moves, start)
    return engine


def counts(engine: OccupancyEngine, now: datetime) -> dict[str, int]:
    """Every Area's occupant count as of `now`, for a full-house assertion
    in one line rather than one `area_state()` call per Area.
    """
    return {area_id: state.occupant_count for area_id, state in engine.all_area_states(now).items()}


def test_single_person_enters_through_front_door_and_walks_to_back_yard() -> None:
    """Baseline happy path: one person, straight through the house, no
    lingering in any connector-adjacent Area — the front door (egress-sensor
    path) followed by three ordinary sensor-less-connector hops in a row.
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, connector_id=CONN_FRONT_DOOR),
        Move(offset=2, area_id=ENTRANCE_HALLWAY),
        Move(offset=10, area_id=KITCHEN),
        Move(offset=20, area_id=BACK_YARD),
    ]
    engine = run_scenario(graph, moves)
    end = T0 + timedelta(seconds=20)

    result = counts(engine, end)
    assert result[BACK_YARD] == 1
    assert result[KITCHEN] == 0
    assert result[ENTRANCE_HALLWAY] == 0
    assert sum(result.values()) == 1


def test_person_lingers_in_front_yard_before_using_the_front_door() -> None:
    """A person triggers the front_yard motion sensor (e.g. walking up the
    path) *before* opening the front door, rather than the door being the
    very first signal. Ground truth is still exactly one person — an earlier
    version of this scenario caught a real bug where front_yard's occupant
    was left stranded (never decremented) while the door's own arrival was
    *also* counted as a brand-new person, double-counting one real person as
    two (see docs/DECISIONS.md's 2026-08-15 "phantom-duplicated arrival"
    entry for the fix in `_confirm_transit`).
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=FRONT_YARD),
        Move(offset=5, connector_id=CONN_FRONT_DOOR),
        Move(offset=7, area_id=ENTRANCE_HALLWAY),
    ]
    engine = run_scenario(graph, moves)
    end = T0 + timedelta(seconds=7)

    result = counts(engine, end)
    assert result[ENTRANCE_HALLWAY] == 1
    assert result[FRONT_YARD] == 0
    assert sum(result.values()) == 1


def test_two_independent_people_are_not_merged_into_one() -> None:
    """The flip side of the scenario above: a genuinely *unrelated* person
    already settled in the kitchen (long before the door event, well outside
    the transit-confirmation window) must not be misread as the front door
    arrival's source — the neighbor-preference fix must stay timing-gated,
    not treat any occupied neighbor as automatically "the same person."
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=KITCHEN),  # someone already in the kitchen
        Move(offset=600, connector_id=CONN_FRONT_DOOR),  # 10 minutes later, front door
        Move(offset=602, area_id=ENTRANCE_HALLWAY),
    ]
    engine = run_scenario(
        graph, moves, config=EngineConfig(transit_confirmation_window=timedelta(seconds=90))
    )
    end = T0 + timedelta(seconds=602)

    result = counts(engine, end)
    assert result[KITCHEN] == 1  # untouched — not misattributed as the door's source
    assert result[ENTRANCE_HALLWAY] == 1  # counted as a genuinely new arrival from OUTSIDE
    assert sum(result.values()) == 2


def test_two_plausible_neighbors_at_once_stays_ambiguous_not_guessed() -> None:
    """If *both* front_yard and living_room (both Connector-adjacent to
    entrance_hallway) have fresh, equally-plausible activity when the door
    confirms, the engine can't tell which one is the real source — same
    "more than one candidate, don't guess" rule `_plausible_transit_source`
    already applies elsewhere, now also reachable from this path.
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=FRONT_YARD),
        Move(offset=1, area_id=LIVING_ROOM),
        Move(offset=5, connector_id=CONN_FRONT_DOOR),
        Move(offset=7, area_id=ENTRANCE_HALLWAY),
    ]
    engine = run_scenario(graph, moves)
    end = T0 + timedelta(seconds=7)

    result = counts(engine, end)
    # Ambiguous: neither neighbor is drained, and the arrival still counts as
    # a new occupant rather than being silently dropped.
    assert result[FRONT_YARD] == 1
    assert result[LIVING_ROOM] == 1
    assert result[ENTRANCE_HALLWAY] == 1
    assert sum(result.values()) == 3


def test_delivery_arrival_does_not_disturb_two_unrelated_occupants_upstairs() -> None:
    """Two people are already settled upstairs (bedroom, office) when a third
    person arrives via the front door — a direct test of the "known number of
    people" scenario the household-size hint (SPEC.md §6.4) and the
    multi-occupant counting model exist for: the third arrival must be
    additive, not disturb the other two, and not get merged with either.
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=BEDROOM),
        Move(offset=0, area_id=OFFICE),
        Move(offset=3600, connector_id=CONN_FRONT_DOOR),  # an hour later
        Move(offset=3602, area_id=ENTRANCE_HALLWAY),
    ]
    engine = run_scenario(graph, moves)
    end = T0 + timedelta(seconds=3602)

    result = counts(engine, end)
    assert result[BEDROOM] == 1
    assert result[OFFICE] == 1
    assert result[ENTRANCE_HALLWAY] == 1
    assert sum(result.values()) == 3


def test_upstairs_walk_through_the_landing_hub_leaves_no_ghosts() -> None:
    """`landing` is a 5-way junction (stairs, guest_bathroom, bedroom_2,
    office, bedroom) with its own motion sensor — a person walking
    stairs -> landing -> bedroom -> landing -> office (checking on something,
    then going to the office) should pass through landing twice without
    leaving a residual "ghost" occupant there, and without the two landing
    crossings being misread as two different people.
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=ENTRANCE_HALLWAY),
        Move(offset=5, area_id=STAIRS),
        Move(offset=10, area_id=LANDING),
        Move(offset=15, area_id=BEDROOM),
        Move(offset=60, area_id=LANDING),  # heads back out of the bedroom
        Move(offset=65, area_id=OFFICE),
    ]
    engine = run_scenario(graph, moves)
    end = T0 + timedelta(seconds=65)

    result = counts(engine, end)
    assert result[OFFICE] == 1
    assert result[LANDING] == 0
    assert result[BEDROOM] == 0
    assert result[STAIRS] == 0
    assert result[ENTRANCE_HALLWAY] == 0
    assert sum(result.values()) == 1


def qualities(engine: OccupancyEngine, now: datetime) -> dict[str, StateQuality]:
    """Every Area's quality tier as of `now` — the freshness/confirmation
    label, kept separate from `counts()` since a scenario may want to assert
    one without the other (SPEC.md §6.2: the count never decays, only this
    label does).
    """
    return {area_id: state.quality for area_id, state in engine.all_area_states(now).items()}


# -- Unsensored rooms (SPEC.md §5.1: "a sensor-less connecting Area can still
# be a valid pass-through node") --------------------------------------------
#
# `stairs`, `guest_bathroom`, and `bedroom_2` genuinely have no
# activity-evidence entity selected in the real topology (`area_entity_
# selections` — see docs/STATUS.md) — no AreaActivitySignal for them would
# ever exist in reality, unlike front_yard/back_yard used in the egress
# scenarios above (those don't currently have a sensor selected either, but
# COULD; these three are used here specifically because SPEC.md's
# pass-through/terminal-latch guarantees have to hold with *zero* signal ever
# arriving for them, not just "none in this particular scenario").


def test_unsensored_room_is_correctly_skipped_as_a_pass_through() -> None:
    """A person walks entrance_hallway -> stairs (no sensor, no signal at
    all) -> landing (sensored). `stairs` never receives a single Signal in
    this test — the only evidence the engine ever gets is hallway occupied,
    then landing occupied — SPEC.md's pass-through guarantee has to resolve
    this as one continuous transit anyway.
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=ENTRANCE_HALLWAY),
        Move(offset=20, area_id=LANDING),  # no stairs signal in between
    ]
    engine = run_scenario(graph, moves)
    end = T0 + timedelta(seconds=20)

    result = counts(engine, end)
    assert result[LANDING] == 1
    assert result[ENTRANCE_HALLWAY] == 0
    assert result[STAIRS] == 0
    assert sum(result.values()) == 1


def test_unsensored_terminal_room_latches_belief_at_the_last_sensored_area() -> None:
    """A person walks from landing into guest_bathroom, which has no sensor
    at all — no Signal can *ever* place them there directly. Ground truth:
    they're still in the house the whole time (in guest_bathroom, not
    landing) — the engine has no way to know that specifically, so the
    correct, safe behavior is to keep believing they're still in landing
    (its last known location) rather than losing them, matching the same
    "absence of signal never decays the count" latch principle SPEC.md §6.2
    already applies to quiet periods in general. When they come back out to
    landing later, that must not be misread as a second person arriving.
    """
    graph = real_house_graph()
    moves = [
        Move(offset=0, area_id=LANDING),
        # (silently walks into guest_bathroom — no sensor, no signal)
        Move(offset=300, area_id=LANDING),  # 5 minutes later, comes back out
    ]
    engine = run_scenario(graph, moves)

    mid_point = T0 + timedelta(seconds=150)
    mid_result = counts(engine, mid_point)
    assert mid_result[LANDING] == 1  # never lost, even while "actually" in guest_bathroom
    assert mid_result[GUEST_BATHROOM] == 0  # engine has no way to know this specifically
    assert sum(mid_result.values()) == 1  # but the *house* total stays correct throughout

    end = T0 + timedelta(seconds=300)
    end_result = counts(engine, end)
    assert end_result[LANDING] == 1  # still just the one person, not a second "arrival"
    assert sum(end_result.values()) == 1


# -- Area-kind-scaled transit timing (docs/DECISIONS.md's "area-kind
# classification" entry) -----------------------------------------------------


def test_slow_walk_through_an_unsensored_stairwell_no_longer_creates_a_phantom_occupant() -> None:
    """Reproduces the real walk-test failure pattern (docs/DECISIONS.md's
    2026-08-15 "transit inference needs a rework" entry): a person walks
    kitchen -> entrance_hallway -> stairs -> landing -> office, unhurried,
    with none of the intermediate rooms' own sensors firing along the way
    (a real, common outcome — a PIR miss, or simply not lingering long enough
    to trigger one). The total gap (140s) exceeds the flat 90-second
    `transit_confirmation_window` on its own — before the area-kind fix, this
    is exactly the shape of walk that got misread as kitchen staying latched
    at 1 *and* office becoming a second, phantom occupant. With `stairs`
    correctly classified `AreaKind.TRANSIT` (a through-node with no evidence
    entity of its own), the extra `transit_area_hop_extension` (60s default)
    budget is enough to still resolve this as the same person continuing
    their walk.
    """
    graph = real_house_graph()
    engine = OccupancyEngine(graph)
    apply_moves(engine, [Move(offset=0, area_id=KITCHEN)])

    # 140s later, office's own sensor fires — nothing in between ever did.
    apply_moves(engine, [Move(offset=140, area_id=OFFICE)])

    end = T0 + timedelta(seconds=140)
    result = counts(engine, end)
    assert result[OFFICE] == 1
    assert result[KITCHEN] == 0  # correctly drained, not left stranded
    assert sum(result.values()) == 1  # ground truth: still just one person


def test_an_implausibly_long_gap_still_falls_back_to_a_new_occupant() -> None:
    """The area-kind extension is a bounded, real-walking-time budget, not an
    unlimited one — a gap far beyond even the extended window (here: 20
    minutes, well past `transit_confirmation_window` + `transit_area_hop_
    extension`'s combined 150s) must still fall back to "no plausible source,
    new occupant," the same conservative default as before this change. This
    guards against the fix silently becoming "never disambiguate stairs
    again" regardless of how stale the evidence actually is.
    """
    graph = real_house_graph()
    engine = OccupancyEngine(graph)
    apply_moves(engine, [Move(offset=0, area_id=KITCHEN)])

    apply_moves(engine, [Move(offset=1200, area_id=OFFICE)])

    end = T0 + timedelta(seconds=1200)
    result = counts(engine, end)
    assert result[KITCHEN] == 1  # stale, but still latched — not lost
    assert result[OFFICE] == 1  # treated as a genuinely new/unrelated occupant
    assert sum(result.values()) == 2  # the known, accepted overcounting limit


# -- Overnight latching (SPEC.md §6.2: absence of signal never decays a
# count, only the freshness label degrades) ---------------------------------


def test_two_people_sleeping_overnight_stay_counted_through_total_silence() -> None:
    """Two people settle in separate rooms for the night and generate no
    further activity for 8 hours — occupant *counts* must stay exactly 2
    throughout, with quality degrading from CONFIRMED to LATCHED once each
    Area's own freshness window elapses (the default is 10 minutes — trivial
    to blow through overnight), never the other way around.
    """
    graph = real_house_graph()
    bedtime_a = 0
    bedtime_b = 15 * 60  # B stays up a bit later, falls asleep on the couch
    moves = [
        Move(offset=bedtime_a, area_id=BEDROOM),
        Move(offset=bedtime_b, area_id=LIVING_ROOM),
    ]
    engine = run_scenario(graph, moves)  # default config: 10-minute freshness window

    just_after_b_settles = T0 + timedelta(seconds=bedtime_b + 30)
    early_result = counts(engine, just_after_b_settles)
    early_quality = qualities(engine, just_after_b_settles)
    assert early_result[BEDROOM] == 1
    assert early_result[LIVING_ROOM] == 1
    assert early_quality[BEDROOM] == StateQuality.LATCHED  # A's window has long since elapsed
    assert early_quality[LIVING_ROOM] == StateQuality.CONFIRMED  # B only just settled

    for hours in (1, 4, 8):
        checkpoint = T0 + timedelta(hours=hours)
        result = counts(engine, checkpoint)
        quality = qualities(engine, checkpoint)
        assert result[BEDROOM] == 1, f"bedroom count decayed after {hours}h"
        assert result[LIVING_ROOM] == 1, f"living_room count decayed after {hours}h"
        assert sum(result.values()) == 2, f"total occupant count drifted after {hours}h"
        assert quality[BEDROOM] == StateQuality.LATCHED
        assert quality[LIVING_ROOM] == StateQuality.LATCHED

    # A wakes first and heads downstairs. Realistically, a PIR motion sensor
    # catches someone stirring in bed before they actually get up and leave
    # — so the bedroom gets one more refresh close to wake time, then landing
    # (pass-through stairs), then hallway, each within the default 90s
    # transit window of the last. Must not disturb B, still asleep on the
    # couch in an entirely different part of the house.
    wake_time = 8 * 3600
    apply_moves(
        engine,
        [
            Move(offset=wake_time - 30, area_id=BEDROOM),  # A stirs before getting up
            Move(offset=wake_time - 15, area_id=LANDING),
            Move(offset=wake_time, area_id=ENTRANCE_HALLWAY),
        ],
    )
    morning = T0 + timedelta(seconds=wake_time)
    morning_result = counts(engine, morning)
    assert morning_result[BEDROOM] == 0  # A left
    assert morning_result[ENTRANCE_HALLWAY] == 1  # A, now downstairs
    assert morning_result[LIVING_ROOM] == 1  # B, undisturbed
    assert sum(morning_result.values()) == 2


def test_silent_long_gap_wake_is_a_known_overcounting_limitation() -> None:
    """The flip side of the realistic wake-up above, documented rather than
    silently accepted: if A gets up *without* any intermediate stirring
    signal — landing fires "cold," 8 hours after bedroom's last evidence,
    with nothing in between — the gap is far outside the default 90-second
    transit window, so the engine has no basis to attribute landing's
    arrival back to the bedroom occupant specifically. It's not wrong to
    treat it this way (a stale, 8-hour-old motion event genuinely isn't
    strong evidence of a specific ongoing transit — an actual corroborating
    Connector-crossing signal would be needed to resolve it correctly, and
    ordinary Connectors don't carry sensors per SPEC.md §7.3), but it does
    mean the house *overcounts* by one in this exact shape of scenario:
    bedroom stays latched at 1 (correctly — SPEC.md §6.2, never decay
    without contradicting evidence) *and* landing becomes a second, separate
    occupant. This is a real, known trade-off of the timing-window model,
    not something this session attempted to fix — a proper fix would need
    either a much larger overnight-specific window (weakens the "can't be
    the same person" teleport-prevention check elsewhere) or corroborating
    evidence this project's topology doesn't collect for ordinary Connectors.
    """
    graph = real_house_graph()
    engine = OccupancyEngine(graph)
    apply_moves(engine, [Move(offset=0, area_id=BEDROOM)])

    wake_time = 8 * 3600
    apply_moves(engine, [Move(offset=wake_time, area_id=LANDING)])

    morning = T0 + timedelta(seconds=wake_time)
    result = counts(engine, morning)
    assert result[BEDROOM] == 1  # stale, but still latched — not lost
    assert result[LANDING] == 1  # a second, separate occupant — the overcount
    assert sum(result.values()) == 2  # true ground truth is 1; this is the known gap


# -- House empties (multiple people, sequential departures) -----------------


def test_house_empties_completely_as_each_occupant_departs_in_turn() -> None:
    """Three known people, each in a different part of the house, each leave
    through the front door one at a time (not simultaneously — see the
    concurrent-departure stress test below for that). The house must end up
    at exactly 0, with every intermediate room correctly drained, including
    one occupant's path crossing the unsensored `stairs` pass-through.
    Checked in three stages with `apply_moves` (not one `run_scenario` call
    checked retroactively at earlier timestamps — `occupant_count` has no
    notion of "as of an earlier time," only `quality` does, so that would
    silently assert against the *final* post-everyone-left state instead).
    All hop-to-hop gaps stay within the default 90-second transit window.
    """
    graph = real_house_graph()
    engine = OccupancyEngine(graph)
    apply_moves(
        engine,
        [
            # A and C settle in; B (kitchen) deliberately settles later, once
            # A's own hallway transit below has already resolved — kitchen
            # and living_room are *both* directly hallway-adjacent, so
            # occupying both at once would make A's transit genuinely
            # ambiguous between them (the same "more than one candidate,
            # don't guess" rule as everywhere else — not a bug to route
            # around, just not what this scenario is testing).
            Move(offset=0, area_id=LIVING_ROOM),
            Move(offset=0, area_id=BEDROOM),
        ],
    )

    # Person A: living_room -> hallway -> out the front door.
    apply_moves(
        engine,
        [
            Move(offset=10, area_id=ENTRANCE_HALLWAY),
            Move(offset=15, connector_id=CONN_FRONT_DOOR),
        ],
    )
    after_a_leaves = T0 + timedelta(seconds=15)
    result_after_a = counts(engine, after_a_leaves)
    assert result_after_a[ENTRANCE_HALLWAY] == 0
    assert result_after_a[LIVING_ROOM] == 0
    assert sum(result_after_a.values()) == 1  # only C (bedroom) so far — B hasn't arrived yet

    # Person B settles into the kitchen — deliberately more than
    # transit_confirmation_window (default 90s) after bedroom's last
    # evidence, so bedroom (the only other occupied Area, reachable from
    # kitchen via an unbroken empty-Area chain now that multi-hop pass-
    # through exists) isn't mistaken for B's source: it's genuinely too
    # stale to plausibly be the same person by then, so this correctly
    # registers as an independent new occupant instead. Then leaves straight
    # out the kitchen door.
    apply_moves(
        engine,
        [
            Move(offset=300, area_id=KITCHEN),
            Move(offset=350, connector_id=CONN_KITCHEN_DOOR),
        ],
    )
    after_b_leaves = T0 + timedelta(seconds=350)
    result_after_b = counts(engine, after_b_leaves)
    assert result_after_b[KITCHEN] == 0
    assert sum(result_after_b.values()) == 1  # only C (bedroom) remains

    # Person C: bedroom (refreshed) -> landing -> (stairs, unsensored) -> hallway -> out.
    apply_moves(
        engine,
        [
            Move(offset=445, area_id=BEDROOM),  # stirs before getting up
            Move(offset=455, area_id=LANDING),
            Move(offset=465, area_id=ENTRANCE_HALLWAY),
            Move(offset=470, connector_id=CONN_FRONT_DOOR),
        ],
    )
    end = T0 + timedelta(seconds=470)
    final = counts(engine, end)
    assert sum(final.values()) == 0
    assert all(count == 0 for count in final.values())


# -- Known-N simultaneous occupants, tight timing stress test ---------------


def test_known_three_people_moving_simultaneously_stays_at_three() -> None:
    """Three known people active in different parts of the house at once:
    A settled in the kitchen, B settled in the office, C walks
    bedroom -> landing -> (stairs, unsensored) -> hallway, checked in stages
    with `apply_moves` so each checkpoint reflects only what's actually
    happened by that point (see `test_house_empties...`'s docstring for why
    a single `run_scenario` call checked retroactively would be wrong here).
    The known ground truth never exceeds 3 at any point.
    """
    graph = real_house_graph()
    engine = OccupancyEngine(graph)
    apply_moves(
        engine,
        [
            Move(offset=0, area_id=KITCHEN),  # A
            Move(offset=0, area_id=OFFICE),  # B settles in
        ],
    )

    apply_moves(engine, [Move(offset=1000, area_id=BEDROOM)])  # C settles in first
    t_settled = T0 + timedelta(seconds=1000)
    assert sum(counts(engine, t_settled).values()) == 3

    apply_moves(engine, [Move(offset=1015, area_id=LANDING)])  # C heads for the stairs
    t_landing = T0 + timedelta(seconds=1015)
    landing_result = counts(engine, t_landing)
    assert landing_result[BEDROOM] == 0, "landing arrival should drain bedroom, C's true source"
    assert landing_result[LANDING] == 1
    assert sum(landing_result.values()) == 3

    apply_moves(engine, [Move(offset=1035, area_id=ENTRANCE_HALLWAY)])  # C reaches the hallway
    t_hallway = T0 + timedelta(seconds=1035)
    hallway_result = counts(engine, t_hallway)
    assert hallway_result[LANDING] == 0
    assert hallway_result[ENTRANCE_HALLWAY] == 1
    assert sum(hallway_result.values()) == 3


def test_coincidental_neighbor_retrigger_can_cause_a_transient_overcount() -> None:
    """The scenario above, but with one added twist: B *coincidentally*
    shifts in their chair and re-triggers office's already-on motion sensor
    (an extremely common, everyday false-positive-prone real-sensor
    behavior) right as C is walking through landing — landing is
    Connector-adjacent to *both* bedroom (C's true source) and office (B's
    room, now purely coincidentally "recently active" too). With two
    equally-plausible candidates, the engine can't disambiguate and falls
    back to its already-established, deliberately conservative "don't
    guess" rule (see `test_two_plausible_neighbors_at_once_stays_ambiguous_
    not_guessed`) — which in *this* shape of scenario means landing gets
    counted as a new, fourth occupant instead of correctly draining bedroom.
    Documented here as a real, characterized risk rather than silently
    fixed: it's the same conservative-when-ambiguous trade-off the project
    already deliberately chose elsewhere, just newly reachable via ordinary
    sensor noise rather than a genuine second person, and fixing it would
    need distinguishing "just re-confirmed occupancy" from "genuinely fresh
    arrival evidence" — a real design question for the project owner, not a
    quick patch (see docs/DECISIONS.md).
    """
    graph = real_house_graph()
    engine = OccupancyEngine(graph)
    apply_moves(
        engine,
        [
            Move(offset=0, area_id=KITCHEN),  # A
            Move(offset=0, area_id=OFFICE),  # B settles in
        ],
    )
    apply_moves(engine, [Move(offset=1000, area_id=BEDROOM)])  # C settles in first
    apply_moves(engine, [Move(offset=1010, area_id=OFFICE)])  # B shifts in their chair

    apply_moves(engine, [Move(offset=1015, area_id=LANDING)])  # C heads for the stairs
    t_landing = T0 + timedelta(seconds=1015)
    result = counts(engine, t_landing)
    # The known-bug behavior, characterized rather than silently accepted:
    assert result[BEDROOM] == 1  # NOT drained — ambiguous with office, so left alone
    assert result[OFFICE] == 1  # B, untouched
    assert result[LANDING] == 1  # treated as a new arrival instead of C's continuation
    assert sum(result.values()) == 4  # true ground truth is still 3 — this is the overcount
