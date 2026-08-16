"""Occupancy engine: the latch/transit-inference state machine.

Pure Python, no Home Assistant dependency (docs/ARCHITECTURE.md §1.4) —
consumes a `HouseGraph` (a standalone graph, not `topology_store.TopologyData`
or `registry_sync.HouseShape` directly, so this module never imports
Home Assistant even transitively) plus a stream of `Signal`s, and produces
per-Area occupant state (docs/SPEC.md §6.2-§6.5). Building a `HouseGraph`
from the live topology store + registry sync layer is a later phase's job
(docs/ARCHITECTURE.md §1.4-1.5: entity platforms wire the shared engine
instance to those layers).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto

#: Sentinel "area id" for the boundary outside the house (docs/SPEC.md §6.1).
#: Never a real Area, never present in a HouseGraph's `area_ids`, but valid as
#: one endpoint of an egress Connector.
OUTSIDE = "outside"


class AreaKind(Enum):
    """Topology-shape classification of an Area, used only to scale how long
    a multi-hop transit through it can plausibly take (docs/DECISIONS.md's
    "area-kind classification" entry) — never changes what counts as a valid
    graph node or which Areas can hold an occupant (SPEC.md §5.1 already lets
    a sensor-less Area act as a pass-through node regardless of kind).
    Inferred from topology shape, or user-overridden — both `engine_adapter.py`'s
    job, never guessed here.
    """

    #: The default: a space people plausibly linger in (a bedroom, a lounge).
    ROOM = auto()
    #: A passage people cross rather than occupy (a hallway, a stairwell).
    TRANSIT = auto()


@dataclass(frozen=True, slots=True)
class GraphConnector:
    """An edge in the house graph: a passage between two nodes.

    One side may be `OUTSIDE`, representing an egress point (SPEC.md §6.5) —
    the engine treats egress-point crossings like any other Connector, with
    "outside" as an ordinary (if occupant-count-less) graph node.
    """

    connector_id: str
    area_id_a: str
    area_id_b: str

    def other_side(self, area_id: str) -> str:
        if area_id == self.area_id_a:
            return self.area_id_b
        if area_id == self.area_id_b:
            return self.area_id_a
        raise ValueError(f"{area_id!r} is not an endpoint of connector {self.connector_id!r}")

    def touches(self, area_id: str) -> bool:
        return area_id in (self.area_id_a, self.area_id_b)


@dataclass(frozen=True, slots=True)
class HouseGraph:
    """All Areas + Connectors the engine reasons over (SPEC.md §6.1)."""

    area_ids: frozenset[str]
    connectors: tuple[GraphConnector, ...] = ()
    #: Area-kind classification (see `AreaKind`) — an Area absent from this
    #: mapping is treated as `AreaKind.ROOM`, the safe default (no extended
    #: transit budget), so a `HouseGraph` built before this concept existed
    #: (e.g. by a test constructing one directly) still behaves unchanged.
    area_kinds: Mapping[str, AreaKind] = field(default_factory=dict)
    #: Areas the user has flagged as genuinely outdoors (docs/DECISIONS.md's
    #: "outdoor Areas excluded from the whole-house total" entry) — e.g. a
    #: front/back yard kept in the graph on purpose so "lingered outside
    #: before using the door" still works, but not somewhere that should
    #: count toward `total_occupant_count`. Excluded *only* from that sum;
    #: the Area's own count, transit inference, and quality tier are all
    #: completely unaffected — this is purely a house-level reporting concern.
    outside_area_ids: frozenset[str] = frozenset()
    #: Areas whose activity evidence is *entirely* continuous-presence-class
    #: sensors (docs/DECISIONS.md's decay entry) — eligible for
    #: `OccupancyEngine.expire_vacant_area`'s auto-clear-after-quiet
    #: mechanism. An Area absent here (the common case: motion sensors, or no
    #: evidence at all) is never auto-cleared — SPEC.md §6.2's "absence of
    #: signal never decays it" guarantee holds unchanged for everything else.
    decay_eligible_area_ids: frozenset[str] = frozenset()

    def connector(self, connector_id: str) -> GraphConnector:
        for connector in self.connectors:
            if connector.connector_id == connector_id:
                return connector
        raise ValueError(f"Unknown connector: {connector_id!r}")

    def kind_of(self, area_id: str) -> AreaKind:
        return self.area_kinds.get(area_id, AreaKind.ROOM)


class ProvenanceTier(Enum):
    """Automation-vs-manual causal classification of a Signal (SPEC.md §6.6).

    Defined here (not in `provenance.py`) so `occupancy_engine.py` stays
    HA-import-free — resolving a real `homeassistant.core.Context` into one
    of these tiers is `provenance.py`'s job; this module only needs the tag
    itself (see docs/DECISIONS.md).
    """

    #: `Context` chain resolves to a known automation/script — suppressed
    #: entirely by signal ingestion before it ever reaches the engine (high
    #: confidence it's machine-caused, so it never becomes a Signal at all).
    AUTOMATION_SUPPRESSED = auto()
    #: `Context.user_id` present, no automation ancestry — high confidence
    #: it's human-caused.
    USER_CONFIRMED = auto()
    #: Neither an automation ancestor nor a `user_id` — a device acting on
    #: its own most consistent with someone physically operating it, but
    #: weaker evidence than a direct user match.
    AMBIGUOUS_PHYSICAL = auto()


@dataclass(frozen=True, slots=True)
class AreaActivitySignal:
    """Evidence someone is active *in* `area_id` right now."""

    area_id: str
    timestamp: datetime
    source: str
    provenance: ProvenanceTier = ProvenanceTier.USER_CONFIRMED


@dataclass(frozen=True, slots=True)
class ConnectorActivitySignal:
    """Evidence of crossing activity along `connector_id` (SPEC.md §6.3)."""

    connector_id: str
    timestamp: datetime
    source: str
    provenance: ProvenanceTier = ProvenanceTier.USER_CONFIRMED


#: The only shape the engine consumes (docs/ARCHITECTURE.md §1.3).
#: `AUTOMATION_SUPPRESSED` signals are never constructed by signal ingestion
#: in the first place (docs/DECISIONS.md) — the engine only ever sees
#: `USER_CONFIRMED`/`AMBIGUOUS_PHYSICAL` evidence, both accepted, tracked as
#: an inspectable attribute (SPEC.md §6.8) rather than weighted numerically.
Signal = AreaActivitySignal | ConnectorActivitySignal


class StateQuality(Enum):
    """SPEC.md §6.8's confirmed/latched/ambiguous tiers."""

    #: Directly evidenced within the freshness window.
    CONFIRMED = auto()
    #: No recent direct evidence, but retained via the latch — the count
    #: itself never decays (SPEC.md §6.2), only this label does.
    LATCHED = auto()
    #: An unresolved pending transit currently touches this Area.
    AMBIGUOUS = auto()


@dataclass(frozen=True, slots=True)
class AreaState:
    """Point-in-time occupancy belief for one Area."""

    area_id: str
    occupant_count: int
    quality: StateQuality
    last_confirmed: datetime | None
    #: Provenance of the most recent direct evidence for this Area — an
    #: inspectable attribute (SPEC.md §6.8), not a numeric weight the engine
    #: itself branches on (see docs/DECISIONS.md).
    last_provenance: ProvenanceTier | None
    #: True when this Area has been `LATCHED` (not `CONFIRMED`), nonzero, and
    #: not decay-eligible, for longer than `EngineConfig.long_latched_review_
    #: threshold` (docs/DECISIONS.md's decay entry) — a *purely informational*
    #: nudge to manually check a room that's plausibly drifted, never an
    #: automatic count change (SPEC.md §6.2's "never silently changes the
    #: count" guarantee holds; only decay-eligible Areas, see below, ever
    #: auto-clear). Always `False` for a decay-eligible Area, since the
    #: correct action there is `expire_vacant_area`, not a manual nudge.
    needs_review: bool


@dataclass(frozen=True, slots=True)
class UncertainBirth:
    """A currently-pending "was this really a new person" record
    (docs/DECISIONS.md's "uncertain births" entry) — read-only diagnostic
    view (SPEC.md §8) of an internal `_UncertainBirth`. Created when a new
    occupant is inferred from a genuine near-tie among 2+ plausible sources
    (not when there was no plausible source at all — that stays a plain,
    unambiguous new occupant, same as before this feature existed).
    """

    area_id: str
    candidate_area_ids: frozenset[str]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Tunables for the latch/transit model (docs/ARCHITECTURE.md §2's
    "typed, centralized config" extension point). Scoped to what SPEC.md
    §6.2-§6.5 needs; household-size hints, confidence thresholds, and
    near-house zones join this (or a config object that wraps it) in later
    phases as their features land, rather than being stubbed in now.
    """

    #: How long a Connector-activity event waits for destination-Area
    #: corroboration before being discarded unconfirmed (SPEC.md §6.3). Also
    #: doubles as the upper bound on how long after a candidate source Area's
    #: last evidence a sensor-less-Connector direct transit stays plausible.
    transit_confirmation_window: timedelta = timedelta(seconds=90)
    #: How long after direct evidence an Area's quality stays CONFIRMED
    #: before degrading to LATCHED. The occupant *count* never changes from
    #: this — only the freshness label (SPEC.md §6.2, §6.8).
    confirmed_freshness_window: timedelta = timedelta(minutes=10)
    #: Lower bound on how soon after a candidate source Area's last evidence
    #: a sensor-less-Connector direct transit can plausibly be the *same*
    #: person: a gap shorter than this is physically impossible (they can't
    #: teleport), so it's read as a second, independent occupant instead.
    min_transit_time: timedelta = timedelta(seconds=2)
    #: Extra time budget added to the transit-confirmation window for each
    #: `AreaKind.TRANSIT` Area a sensor-less-Connector transit search walks
    #: through en route to a candidate source (docs/DECISIONS.md's "area-kind
    #: classification" entry) — a real hallway/stairwell walk takes longer
    #: than a same-room signal gap, and a single flat window doesn't scale
    #: with that. Zero would reproduce the old flat-window behavior exactly.
    transit_area_hop_extension: timedelta = timedelta(seconds=60)
    #: How much *extra*, tapering-plausibility budget (docs/DECISIONS.md's
    #: "scored transit timing" entry) `_plausible_transit_source` allows past
    #: `transit_confirmation_window` (+ `transit_area_hop_extension`), as a
    #: fraction of that window — e.g. the default 0.5 means a candidate up to
    #: 50% past the window is still considered, at linearly-tapering
    #: plausibility (1.0 at the window's edge, 0.0 at window*(1+this)),
    #: instead of being rejected outright the instant the window elapses. A
    #: gap within the window itself is unaffected (still scores 1.0, same as
    #: the old hard-cutoff behavior). Internal tuning knob, not exposed to
    #: users — unlike the window durations themselves, "how gracefully this
    #: tapers" isn't a concept a non-technical user can meaningfully reason
    #: about; `0.0` reproduces the old hard-cutoff behavior exactly.
    transit_grace_fraction: float = 0.5
    #: How close two candidates' plausibility scores need to be (docs/DECISIONS.md's
    #: "scored transit timing" entry) to still be treated as an unresolvable tie
    #: (SPEC.md's existing "don't guess" rule) rather than picking the higher-scoring
    #: one. Two candidates both scoring 1.0 (both comfortably within the window) are
    #: always exactly tied regardless of this value, matching the old exact-tie
    #: behavior; this only matters for candidates in the tapering zone.
    transit_score_tie_margin: float = 0.05
    #: How long an "uncertain birth" (docs/DECISIONS.md's "uncertain births"
    #: entry) — a new occupant inferred from a genuine near-tie among 2+
    #: plausible sources — sits unresolved before the engine concludes
    #: nothing ever independently confirmed it as a real, separate person,
    #: and reattributes it back to whichever original candidate is still
    #: untouched since the tie (or leaves it alone permanently if none are).
    #: This is the one place a purely *inferred*, never-corroborated guess is
    #: allowed to lose confidence from elapsed time alone — direct evidence
    #: never does (SPEC.md §6.2 is unchanged for anything actually
    #: evidenced). Exposed to users (unlike `transit_grace_fraction`/
    #: `transit_score_tie_margin`) since "how long to wait before settling
    #: an unresolved tie" is a real, explainable concept.
    uncertain_birth_resolution_delay: timedelta = timedelta(minutes=30)
    #: Maximum number of uncertain births tracked at once (docs/ARCHITECTURE.md's
    #: bounded-memory requirement) — the oldest is dropped (becoming a
    #: permanent, unresolved new occupant, same as today's behavior) if a new
    #: one would exceed this. Internal tuning knob, not exposed to users.
    max_uncertain_births: int = 8
    #: Minimum number of clean, unambiguous samples (docs/DECISIONS.md's
    #: "learned transit timing" entry) a specific pair of Areas needs before
    #: its own learned typical transit time is trusted *instead of* the flat
    #: `transit_confirmation_window`-based formula, not just alongside it —
    #: below this, there's nothing real to learn from yet, so the existing
    #: formula is the only reasonable estimate. Internal tuning knob, not
    #: exposed to users.
    transit_learning_min_samples: int = 5
    #: Safety margin (in standard deviations) added on top of a learned
    #: pair's mean transit time to get its effective window, once trusted
    #: (docs/DECISIONS.md's "learned transit timing" entry) — real transit
    #: times vary walk to walk, so using the bare mean alone would reject
    #: roughly half of all genuine transits for that pair. Internal tuning
    #: knob, not exposed to users.
    transit_learning_stddev_margin: float = 2.0
    #: How long a decay-eligible Area's continuous-presence evidence must
    #: stay off, with nothing else explaining continued occupancy, before
    #: `expire_vacant_area` clears it (docs/DECISIONS.md's decay entry).
    #: `signal_ingestion.py` is what actually schedules/cancels the timer
    #: this governs — the engine itself has no clock/timer of its own, only
    #: this duration value and the method that gets called once it elapses.
    decay_grace_period: timedelta = timedelta(minutes=5)
    #: How long a non-decay-eligible Area can stay `LATCHED` (with a nonzero
    #: count) before `AreaState.needs_review` flags it for a human to check —
    #: purely informational, never an automatic count change (see
    #: `AreaState.needs_review`'s own docstring for why).
    long_latched_review_threshold: timedelta = timedelta(hours=12)
    #: Minimum number of times a decay-eligible Area must have been walked
    #: through — found *empty* along a transit search that went on to
    #: actually resolve (docs/DECISIONS.md's "per-Area sensor reliability"
    #: entry) — before that Area's own continuous-presence sensor(s) are
    #: treated as sometimes missing people, and its `decay_grace_period`
    #: widens accordingly (`effective_decay_grace_period`). Below this, one
    #: or two misses could just as easily be a genuine empty pass-through;
    #: only a repeated pattern is trusted. Internal tuning knob, not exposed
    #: to users, same reasoning as `transit_learning_min_samples`.
    sensor_reliability_min_misses: int = 3
    #: Factor `decay_grace_period` is multiplied by for a decay-eligible Area
    #: once it's crossed `sensor_reliability_min_misses` (docs/DECISIONS.md's
    #: "per-Area sensor reliability" entry) — a proven-spotty sensor gets
    #: more benefit-of-the-doubt before its "off" reading is trusted as
    #: genuine vacancy, rather than being treated identically to a
    #: consistently-reliable one. Internal tuning knob, not exposed to users.
    unreliable_sensor_grace_multiplier: float = 3.0
    #: Optional whole-house "typical household size" confidence hint (SPEC.md
    #: §6.4) — deliberately *not* consulted anywhere in the count-inference
    #: logic above (`_handle_area_activity` et al.): SPEC.md is explicit this
    #: "must never reject or cap a count the evidence actually supports," so
    #: it's surfaced only as a separate, purely observational attribute
    #: (`exceeds_household_size_hint`, `sensor.py`'s `TotalOccupantCountSensor`)
    #: rather than woven into the state machine, where it would risk becoming
    #: a de facto cap through some later refactor.
    household_size_hint: int | None = None


@dataclass(slots=True)
class _PendingTransit:
    connector_id: str
    source_area_id: str  # may be OUTSIDE
    dest_area_id: str  # may be OUTSIDE
    expires_at: datetime


@dataclass(slots=True)
class _UncertainBirthRecord:
    """Internal, mutable working state for an `UncertainBirth` — see that
    class's docstring for what it represents.
    """

    area_id: str
    candidate_scores: dict[str, float]
    #: Snapshot of each candidate's own `last_confirmed` at the moment of
    #: the fork, so resolution can tell whether a candidate has had any
    #: evidence of its own since — a candidate that's had fresh, independent
    #: evidence since the fork is no longer a safe thing to silently
    #: reattribute to (see `_resolve_stale_uncertain_births`).
    candidate_last_confirmed_at_fork: dict[str, datetime | None]
    created_at: datetime
    #: True when this birth came from an egress-point arrival that stayed
    #: attributed to OUTSIDE only because of a near-tie among interior
    #: candidates (docs/DECISIONS.md's "uncertain births" entry's egress
    #: extension), not from `_handle_area_activity`'s ordinary interior
    #: fallback. `_note_egress_delta(1)` already ran when this was created
    #: (the door crossing itself is real) — if this later resolves to an
    #: interior candidate instead, that provisional "new arrival" turns out
    #: to have been the same already-counted person, so the egress anchor
    #: needs undoing too, not just the count.
    is_egress_arrival: bool = False


@dataclass(slots=True)
class _TransitTimeStats:
    """Running statistics of how long a real, unambiguous transit between two
    Areas has actually taken in this house (docs/DECISIONS.md's "learned
    transit timing" entry) — updated via Welford's online algorithm (pure
    Python, numerically stable, no numpy/scipy dependency) rather than
    storing every raw sample.
    """

    count: int = 0
    mean_seconds: float = 0.0
    #: Sum of squared differences from the running mean (Welford's "M2") —
    #: the form that's actually numerically stable to update incrementally;
    #: `stddev_seconds` is derived from it, not stored directly, since
    #: storing a derived stddev and *not* M2 would make round-tripping
    #: through persistence lossy.
    m2_seconds: float = 0.0

    def add_sample(self, seconds: float) -> None:
        self.count += 1
        delta = seconds - self.mean_seconds
        self.mean_seconds += delta / self.count
        delta2 = seconds - self.mean_seconds
        self.m2_seconds += delta * delta2

    @property
    def stddev_seconds(self) -> float:
        if self.count < 2:
            return 0.0
        return math.sqrt(self.m2_seconds / (self.count - 1))


class OccupancyEngine:
    """The latch/transit-inference state machine (SPEC.md §6.2-§6.5)."""

    def __init__(
        self,
        graph: HouseGraph,
        config: EngineConfig | None = None,
        learned_transit_times: Mapping[frozenset[str], tuple[int, float, float]] | None = None,
        learned_sensor_reliability: Mapping[str, int] | None = None,
    ) -> None:
        self._graph = graph
        self._config = config or EngineConfig()
        self._counts: dict[str, int] = dict.fromkeys(graph.area_ids, 0)
        self._last_confirmed: dict[str, datetime | None] = dict.fromkeys(graph.area_ids)
        self._last_provenance: dict[str, ProvenanceTier | None] = dict.fromkeys(graph.area_ids)
        self._pending: dict[str, _PendingTransit] = {}
        self._listeners: list[Callable[[], None]] = []
        #: Learned per-Area-pair transit timing (docs/DECISIONS.md's
        #: "learned transit timing" entry), keyed by the unordered pair —
        #: seeded from `learned_transit_times` (persisted by the HA-dependent
        #: layer, this module stays HA-import-free) as `(count, mean_seconds,
        #: m2_seconds)` triples, since the engine has no way to load its own
        #: `Store` data.
        self._transit_time_stats: dict[frozenset[str], _TransitTimeStats] = {
            key: _TransitTimeStats(count=count, mean_seconds=mean, m2_seconds=m2)
            for key, (count, mean, m2) in (learned_transit_times or {}).items()
        }
        #: Whole-house total as reconstructed purely from confirmed door
        #: crossings and confirmed corrections (docs/DECISIONS.md's "whole-
        #: house conservation" entry) — `None` until the first such event
        #: ever happens, deliberately: a fresh install with people already
        #: home must never be flagged as suspicious just because no door has
        #: fired yet.
        self._egress_anchor: int | None = None
        #: Currently-pending uncertain births (docs/DECISIONS.md's
        #: "uncertain births" entry) — bounded by
        #: `EngineConfig.max_uncertain_births`.
        self._uncertain_births: list[_UncertainBirthRecord] = []
        #: How many times each Area has been found empty along a transit
        #: search that went on to resolve (docs/DECISIONS.md's "per-Area
        #: sensor reliability" entry) — only ever incremented for a Area in
        #: `graph.decay_eligible_area_ids`, since only continuous-presence-
        #: class evidence is trustworthy enough to call a silent "off"
        #: reading during a confirmed walk-through a real miss. Seeded from
        #: `learned_sensor_reliability` (persisted by the HA-dependent layer,
        #: same pattern as `learned_transit_times`).
        self._area_miss_counts: dict[str, int] = dict(learned_sensor_reliability or {})

    @property
    def graph(self) -> HouseGraph:
        """The `HouseGraph` this engine was built from — read-only, for callers that need to
        resolve a Connector id (e.g. from `pending_transit_connector_ids`) back to the Areas it
        connects (SPEC.md §7.3's explainability inspector) without duplicating graph-construction
        logic outside `engine_adapter.py`. `HouseGraph` is itself a frozen dataclass, so this
        can't be used to mutate engine state through the back door.
        """
        return self._graph

    @property
    def household_size_hint(self) -> int | None:
        """The configured "typical household size" confidence hint (SPEC.md §6.4), if any."""
        return self._config.household_size_hint

    @property
    def egress_anchor_total(self) -> int | None:
        """The whole-house total as reconstructed purely from confirmed door
        crossings and confirmed corrections since the first such event
        (docs/DECISIONS.md's "whole-house conservation" entry) — `None`
        until at least one has happened.

        Compare against `total_occupant_count()`: if the interior model's
        total is higher, something is currently counted that no door
        crossing (or correction) has ever explained — an informational
        signal only. SPEC.md §6.4's "never reject or cap a count the
        evidence actually supports" stays intact; nothing here ever changes
        `total_occupant_count()`'s own value, this is a separate number to
        compare it against.
        """
        return self._egress_anchor

    def _note_egress_delta(self, delta: int) -> None:
        """Adjust the egress anchor by `delta` (+1 a confirmed arrival from
        OUTSIDE, -1 a confirmed departure) — call *before* applying the
        corresponding change to `self._counts`, so establishing the anchor
        for the first time (see `egress_anchor_total`'s docstring) snapshots
        the interior total as it stood immediately before this event, and
        the two then move together for this event's own effect.
        """
        if self._egress_anchor is None:
            self._egress_anchor = sum(self._counts.values())
        self._egress_anchor = max(0, self._egress_anchor + delta)

    @property
    def decay_grace_period(self) -> timedelta:
        """How long `signal_ingestion.py` should wait, once a decay-eligible
        Area's evidence goes fully quiet, before calling `expire_vacant_area`.

        The flat, config-driven default — `effective_decay_grace_period` is
        what callers should actually schedule against, since it accounts for
        a specific Area's own learned reliability.
        """
        return self._config.decay_grace_period

    def effective_decay_grace_period(self, area_id: str) -> timedelta:
        """The grace period `signal_ingestion.py` should actually schedule
        `area_id`'s decay timer against (docs/DECISIONS.md's "per-Area sensor
        reliability" entry): the flat `decay_grace_period`, widened by
        `EngineConfig.unreliable_sensor_grace_multiplier` once this specific
        Area has been caught, `sensor_reliability_min_misses` times or more,
        sitting silent while a transit search resolved *through* it as an
        empty pass-through — real evidence someone was physically there and
        this Area's own continuous-presence sensor(s) didn't catch it. Areas
        outside `graph.decay_eligible_area_ids`, or without enough of a track
        record yet, get the flat default unchanged.
        """
        if area_id not in self._graph.decay_eligible_area_ids:
            return self._config.decay_grace_period
        if self._area_miss_counts.get(area_id, 0) < self._config.sensor_reliability_min_misses:
            return self._config.decay_grace_period
        return self._config.decay_grace_period * self._config.unreliable_sensor_grace_multiplier

    def learned_sensor_reliability(self) -> dict[str, int]:
        """Snapshot of each Area's accumulated "found empty during a resolved
        transit search" count (docs/DECISIONS.md's "per-Area sensor
        reliability" entry) — for the HA-dependent layer to persist and for
        diagnostics. Feed straight back into a new `OccupancyEngine`'s
        `learned_sensor_reliability` constructor argument to resume tracking
        from where it left off.
        """
        return dict(self._area_miss_counts)

    def learned_transit_times(self) -> dict[frozenset[str], tuple[int, float, float]]:
        """Snapshot of learned per-Area-pair transit timing (docs/DECISIONS.md's
        "learned transit timing" entry), as `(count, mean_seconds, m2_seconds)`
        triples — for the HA-dependent layer to persist (this module has no
        `Store` access of its own) and for diagnostics. Feed straight back
        into a new `OccupancyEngine`'s `learned_transit_times` constructor
        argument to resume learning from where it left off.
        """
        return {
            key: (stats.count, stats.mean_seconds, stats.m2_seconds)
            for key, stats in self._transit_time_stats.items()
        }

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked after any Signal changes belief state.

        Entity platforms (Phase 4) use this to push state updates instead of
        polling (docs/ARCHITECTURE.md §1.5, §9). Time-driven-only transitions
        (a quality tier degrading purely because time passed, with no new
        Signal) do *not* trigger this — see docs/STATUS.md's Phase 4 entry
        for why that's an accepted, documented gap for now rather than a
        scheduled-timer mechanism built speculatively ahead of need.
        """
        self._listeners.append(listener)

        def remove_listener() -> None:
            self._listeners.remove(listener)

        return remove_listener

    def area_state(self, area_id: str, now: datetime) -> AreaState:
        """Return the current belief about `area_id` as of `now`."""
        if area_id not in self._counts:
            raise ValueError(f"Unknown area: {area_id!r}")
        self._expire_pending(now)
        self._resolve_stale_uncertain_births(now)
        quality = self._quality_for(area_id, now)
        return AreaState(
            area_id=area_id,
            occupant_count=self._counts[area_id],
            quality=quality,
            last_confirmed=self._last_confirmed[area_id],
            last_provenance=self._last_provenance[area_id],
            needs_review=self._needs_review(area_id, quality, now),
        )

    def _needs_review(self, area_id: str, quality: StateQuality, now: datetime) -> bool:
        if area_id in self._graph.decay_eligible_area_ids:
            return False
        if quality is not StateQuality.LATCHED or self._counts[area_id] <= 0:
            return False
        last_confirmed = self._last_confirmed[area_id]
        return (
            last_confirmed is not None
            and now - last_confirmed > self._config.long_latched_review_threshold
        )

    def all_area_states(self, now: datetime) -> dict[str, AreaState]:
        """Return every Area's current state as of `now`."""
        return {area_id: self.area_state(area_id, now) for area_id in self._graph.area_ids}

    def total_occupant_count(self, now: datetime) -> int:
        """Whole-house occupant total (SPEC.md §8's house-level entity).

        Excludes any Area in `graph.outside_area_ids` — an outdoor Area (e.g.
        a front/back yard) is still fully tracked (its own count, quality,
        and role in transit inference are all untouched), it just shouldn't
        inflate a total meant to represent people *inside* the house
        (docs/DECISIONS.md).
        """
        self._expire_pending(now)
        self._resolve_stale_uncertain_births(now)
        return sum(
            count
            for area_id, count in self._counts.items()
            if area_id not in self._graph.outside_area_ids
        )

    def pending_transit_connector_ids(self, now: datetime) -> frozenset[str]:
        """Connector ids with an unresolved transit, for diagnostics (SPEC.md §8)."""
        self._expire_pending(now)
        return frozenset(self._pending)

    def uncertain_births(self, now: datetime) -> tuple[UncertainBirth, ...]:
        """Currently-pending uncertain births, for diagnostics (SPEC.md §8) —
        see `UncertainBirth`'s own docstring for what one represents.
        """
        self._resolve_stale_uncertain_births(now)
        return tuple(
            UncertainBirth(
                area_id=record.area_id,
                candidate_area_ids=frozenset(record.candidate_scores),
                created_at=record.created_at,
            )
            for record in self._uncertain_births
        )

    def override_occupant_count(self, area_id: str, count: int, now: datetime) -> None:
        """Directly set an Area's occupant count (SPEC.md §8's manual override service).

        Bypasses the latch/transit machinery entirely — this is for the rare case a person
        corrects a wrong automatic inference on the spot, not a new kind of `Signal` the engine
        reasons about. Clears any pending transit touching this Area, since a manual correction is
        strictly more authoritative than an unresolved automatic guess about the same Area, and
        leaving it in place could otherwise later resolve the count elsewhere out from under it.

        Deliberately does *not* touch the egress anchor (`egress_anchor_total`) — only a confirmed
        door crossing moves it (docs/DECISIONS.md). A manual correction for an occupant who
        genuinely came in through an untracked entrance (no door sensor on it at all) would, if this
        *did* adjust the anchor, permanently hide that specific person from the "unexplained by
        doors" check by folding them into the trusted baseline — but "unexplained by doors" is
        simply, honestly true for them, forever, regardless of how confident the correction is.
        Keeping the anchor door-crossing-only avoids that kind of silent laundering, at the cost of
        the flag potentially staying on after a correction the user just confirmed is right.

        Also discards any uncertain birth (docs/DECISIONS.md's "uncertain births" entry) referencing
        this Area, whether as the ambiguous new occupant itself or as one of its candidate sources —
        same "a manual correction is strictly more authoritative than an unresolved automatic guess"
        reasoning as the pending-transit cleanup above.
        """
        if area_id not in self._counts:
            raise ValueError(f"Unknown area: {area_id!r}")
        if count < 0:
            raise ValueError(f"Occupant count cannot be negative: {count!r}")
        self._expire_pending(now)
        for connector_id in [
            connector_id
            for connector_id, pending in self._pending.items()
            if pending.source_area_id == area_id or pending.dest_area_id == area_id
        ]:
            del self._pending[connector_id]
        self._discard_uncertain_births_referencing(area_id)
        self._counts[area_id] = count
        self._last_confirmed[area_id] = now
        self._last_provenance[area_id] = ProvenanceTier.USER_CONFIRMED
        for listener in list(self._listeners):
            listener()

    def expire_vacant_area(self, area_id: str, now: datetime) -> None:
        """Auto-clear a decay-eligible Area's count to 0 (docs/DECISIONS.md's
        decay entry).

        Only ever called by `signal_ingestion.py`, and only for an Area in
        `graph.decay_eligible_area_ids` — one whose *entire* set of selected
        activity evidence is continuous-presence-class sensors (verified
        `device_class: occupancy`, not an ordinary motion sensor whose "off"
        doesn't mean "empty"). `signal_ingestion.py` is responsible for the
        actual timing (scheduling this once such an Area's evidence has been
        off for `decay_grace_period`, cancelling if it comes back on first) —
        this method itself has no notion of "how long," it just applies the
        already-decided clear. A no-op if `area_id` isn't (or is no longer)
        decay-eligible, or is already 0 — keeps this safe to call from a
        timer callback that might fire after the Area became ineligible or
        was already cleared some other way.

        Deliberately does *not* touch the egress anchor either, same reasoning as
        `override_occupant_count` — the anchor only ever moves on a confirmed door crossing.
        Also discards any uncertain birth referencing this Area, same as `override_occupant_count`.
        """
        if area_id not in self._graph.decay_eligible_area_ids:
            return
        self._expire_pending(now)
        if self._counts[area_id] <= 0:
            return
        for connector_id in [
            connector_id
            for connector_id, pending in self._pending.items()
            if pending.source_area_id == area_id or pending.dest_area_id == area_id
        ]:
            del self._pending[connector_id]
        self._discard_uncertain_births_referencing(area_id)
        self._counts[area_id] = 0
        self._last_confirmed[area_id] = now
        for listener in list(self._listeners):
            listener()

    def process_signal(self, signal: Signal) -> None:
        """Fold one normalized Signal into the engine's belief state."""
        self._expire_pending(signal.timestamp)
        self._resolve_stale_uncertain_births(signal.timestamp)
        if isinstance(signal, AreaActivitySignal):
            self._handle_area_activity(signal)
        else:
            self._handle_connector_activity(signal)
        for listener in list(self._listeners):
            listener()

    # -- internals --------------------------------------------------------

    def _quality_for(self, area_id: str, now: datetime) -> StateQuality:
        if any(
            p.source_area_id == area_id or p.dest_area_id == area_id for p in self._pending.values()
        ):
            return StateQuality.AMBIGUOUS
        last_confirmed = self._last_confirmed[area_id]
        if (
            last_confirmed is not None
            and now - last_confirmed <= self._config.confirmed_freshness_window
        ):
            return StateQuality.CONFIRMED
        return StateQuality.LATCHED

    def _expire_pending(self, now: datetime) -> None:
        # Timing out a pending transit makes no count change at all: "no
        # corroborating Connector activity → occupant is assumed to still be
        # in the [source] Area" (SPEC.md §6.3).
        expired = [
            connector_id
            for connector_id, pending in self._pending.items()
            if now >= pending.expires_at
        ]
        for connector_id in expired:
            del self._pending[connector_id]

    def _discard_uncertain_births_referencing(self, area_id: str) -> None:
        self._uncertain_births = [
            record
            for record in self._uncertain_births
            if record.area_id != area_id and area_id not in record.candidate_scores
        ]

    def _record_uncertain_birth(
        self,
        area_id: str,
        candidates: Mapping[str, float],
        now: datetime,
        *,
        is_egress_arrival: bool = False,
    ) -> None:
        """Remember a genuine near-tie behind a new occupant (docs/DECISIONS.md's
        "uncertain births" entry), so it can potentially self-correct later.
        """
        if len(self._uncertain_births) >= self._config.max_uncertain_births:
            # Bounded memory (docs/ARCHITECTURE.md) — drop the oldest rather
            # than grow without limit; it simply becomes a permanent,
            # unresolved new occupant instead, same as today's behavior.
            self._uncertain_births.pop(0)
        self._uncertain_births.append(
            _UncertainBirthRecord(
                area_id=area_id,
                candidate_scores=dict(candidates),
                candidate_last_confirmed_at_fork={
                    candidate_id: self._last_confirmed.get(candidate_id)
                    for candidate_id in candidates
                },
                created_at=now,
                is_egress_arrival=is_egress_arrival,
            )
        )

    def _record_transit_time_sample(self, area_a: str, area_b: str, gap: timedelta) -> None:
        """Learn one real, unambiguous transit-time observation between two
        Areas (docs/DECISIONS.md's "learned transit timing" entry).
        """
        key = frozenset({area_a, area_b})
        stats = self._transit_time_stats.setdefault(key, _TransitTimeStats())
        stats.add_sample(gap.total_seconds())

    def _effective_transit_window(
        self, area_a: str, area_b: str, generic_window: timedelta
    ) -> timedelta:
        """The timing budget to judge a transit between these two Areas
        against (docs/DECISIONS.md's "learned transit timing" entry): once
        this specific pair has `transit_learning_min_samples` real, learned
        observations, its own learned typical time (mean + a safety margin
        of standard deviations) *replaces* `generic_window` entirely rather
        than just supplementing it — including tightening below it, if
        that's what this house's real walking times actually support. Below
        that sample count, there's nothing real to learn from yet, so
        `generic_window` (the flat, config-driven formula) is the only
        reasonable estimate.
        """
        stats = self._transit_time_stats.get(frozenset({area_a, area_b}))
        if stats is None or stats.count < self._config.transit_learning_min_samples:
            return generic_window
        learned_seconds = (
            stats.mean_seconds + self._config.transit_learning_stddev_margin * stats.stddev_seconds
        )
        min_seconds = self._config.min_transit_time.total_seconds()
        return timedelta(seconds=max(learned_seconds, min_seconds))

    def _resolve_stale_uncertain_births(self, now: datetime) -> None:
        """Settle any uncertain birth that's sat unresolved for at least
        `EngineConfig.uncertain_birth_resolution_delay` (docs/DECISIONS.md's
        "uncertain births" entry) — deliberately lazy, evaluated on any call
        that passes `now`, matching `_expire_pending`'s own established
        pattern (docs/STATUS.md's Phase 4 "freshness label only updates on
        the next signal or property read" precedent) rather than a scheduled
        timer the engine has no way to run on its own (pure Python, no HA).

        Reattributes back to whichever original candidate is still
        untouched since the fork (still occupied, and its own `last_confirmed`
        hasn't changed) — the one case this heuristic can safely conclude
        "this was probably the same person all along, just slow to confirm."
        If every original candidate has since left or gained fresh evidence
        of its own, something genuinely happened that this simple check
        can't safely reinterpret — the birth is dropped *without*
        reattributing, leaving it a permanent, separate occupant rather than
        risking a wrong silent merge.

        For a birth that came from an egress-point arrival (`is_egress_arrival`
        — docs/DECISIONS.md's egress extension to this entry), a successful
        reattribution also undoes the provisional egress-anchor increment
        `_confirm_transit` made when the door crossing was first confirmed:
        it's now understood this wasn't actually a fresh arrival from
        OUTSIDE after all, just the same already-counted person moving
        between two interior Areas near the door.
        """
        still_pending: list[_UncertainBirthRecord] = []
        for record in self._uncertain_births:
            if now - record.created_at < self._config.uncertain_birth_resolution_delay:
                still_pending.append(record)
                continue
            if self._counts.get(record.area_id, 0) <= 0:
                continue  # already resolved some other way — drop silently
            untouched = {
                candidate_id: score
                for candidate_id, score in record.candidate_scores.items()
                if self._counts.get(candidate_id, 0) > 0
                and self._last_confirmed.get(candidate_id)
                == record.candidate_last_confirmed_at_fork.get(candidate_id)
            }
            if untouched:
                # The ambiguous new occupant's Area already got its +1 at
                # fork time — resolving the tie only drains the candidate,
                # exactly like a normal transit's source (`_confirm_transit`
                # does the same thing: -1 source, dest is already +1'd).
                # Decrementing the birth's own Area *too* here would
                # double-subtract one real person.
                best_candidate = max(untouched, key=lambda a: untouched[a])
                self._counts[best_candidate] -= 1
                if record.is_egress_arrival and self._egress_anchor is not None:
                    self._egress_anchor = max(0, self._egress_anchor - 1)
        self._uncertain_births = still_pending

    def _handle_area_activity(self, signal: AreaActivitySignal) -> None:
        area_id = signal.area_id
        if area_id not in self._counts:
            raise ValueError(f"Unknown area: {area_id!r}")

        pending = self._pending_for_dest(area_id)
        if pending is not None:
            self._confirm_transit(pending, signal.timestamp, signal.provenance)
            return

        if self._counts[area_id] == 0:
            # No Connector-sensor-driven pending transit explains this.
            # Most Connectors have no sensor of their own to produce one
            # (SPEC.md §7.3 only lets users bind entities to egress points,
            # not ordinary Connector edges; §5.1 explicitly allows a
            # sensor-less connecting Area) — so for those, destination
            # activity plus topology adjacency is the *only* observable
            # evidence of a transit at all, and it has to double as both
            # "candidate evidence" and "corroboration" in the same event
            # (see docs/DECISIONS.md).
            source, candidates = self._search_plausible_transit_sources(area_id, signal.timestamp)
            if source is not None:
                last_confirmed_source = self._last_confirmed[source]
                if sum(self._counts.values()) == 1 and last_confirmed_source is not None:
                    # The whole house had exactly this one occupant before
                    # this event — an unambiguous, self-labeling real
                    # transit, safe to learn from (docs/DECISIONS.md's
                    # "learned transit timing" entry). last_confirmed[source]
                    # is never actually None here in practice — a
                    # None-evidenced Area is never a candidate in the first
                    # place (see the search below) — the check is just
                    # defensive type-narrowing, not a real runtime case.
                    self._record_transit_time_sample(
                        source, area_id, signal.timestamp - last_confirmed_source
                    )
                self._counts[source] -= 1
                self._counts[area_id] = 1
                self._last_confirmed[source] = signal.timestamp
                self._last_confirmed[area_id] = signal.timestamp
                self._last_provenance[source] = signal.provenance
                self._last_provenance[area_id] = signal.provenance
                return
            # No Connector-adjacent Area plausibly explains this as the same
            # person arriving — new-occupant evidence (SPEC.md §6.4).
            self._counts[area_id] = 1
            if len(candidates) >= 2:
                # A genuine near-tie among 2+ real candidates, not "no
                # candidate scored above zero at all" — worth remembering as
                # an uncertain birth in case it self-corrects later
                # (docs/DECISIONS.md's "uncertain births" entry).
                self._record_uncertain_birth(area_id, candidates, signal.timestamp)

        self._last_confirmed[area_id] = signal.timestamp
        self._last_provenance[area_id] = signal.provenance

    def _plausible_transit_source(self, area_id: str, now: datetime) -> str | None:
        """The single resolved candidate from `_search_plausible_transit_sources`,
        for callers (`_confirm_transit`'s egress-arrival reattribution) that
        don't need the full candidate set an uncertain-birth record would.
        """
        source, _ = self._search_plausible_transit_sources(area_id, now)
        return source

    def _search_plausible_transit_sources(
        self, area_id: str, now: datetime
    ) -> tuple[str | None, Mapping[str, float]]:
        """The one reachable occupied Area whose last evidence is close
        enough in time to `now` to plausibly be the same person arriving on
        foot — not so close it would require teleporting (they can't — a gap
        under `min_transit_time` means it's a second, distinct occupant
        instead), not so long ago it's more likely unrelated (see
        docs/DECISIONS.md).

        "Reachable" walks through any chain of currently-*empty* adjacent
        Areas transparently (SPEC.md §5.1: "an Area with zero devices/
        entities is still a valid graph node... e.g. a hallway with no
        sensors at all, connecting two sensored rooms" — an empty Area here
        isn't necessarily literally sensor-less, just currently unconfirmed,
        but the effect needed is the same either way: it must not block the
        search for a real, occupied source just because it's the empty
        Area in between).

        Breadth-first, nearest candidates only: a real 1-hop neighbor is
        always at least as plausible as some other occupied Area three empty
        rooms away that merely *also* happens to fall inside the same flat
        `transit_confirmation_window` — walking further genuinely takes more
        time, so a closer candidate should never lose out to a coincidentally
        time-plausible farther one. Once any candidate is found at a given
        distance, farther distances are never even considered; ambiguity
        (returning `None`) only applies among ties at that same, nearest
        distance — found via a real scripted scenario where a farther Area
        reachable through an unrelated unoccupied chain otherwise polluted a
        much more obviously correct 1-hop match (see docs/DECISIONS.md).

        Each empty Area's own `AreaKind` (docs/DECISIONS.md's "area-kind
        classification" entry) extends the effective timing budget for
        candidates found beyond it — a stairwell or hallway genuinely adds
        real walking time on top of a flat window, so the budget accumulates
        `transit_area_hop_extension` once per `AreaKind.TRANSIT` Area crossed
        along the (shortest) path to a candidate, not just a fixed amount
        regardless of what's actually being walked through.

        Within a layer, candidates are *scored* (docs/DECISIONS.md's "scored
        transit timing" entry), not just accepted/rejected — a gap just past
        the window is still plausible, only gradually less so
        (`_transit_plausibility_score`), rather than falling off a cliff into
        "must be a stranger" the instant the window elapses. The
        highest-scoring candidate wins, *unless* it's within
        `transit_score_tie_margin` of the next-best — the same "can't
        resolve a direction, don't guess" rule as before, generalized from
        exact ties to near-ties now that scores are continuous.

        The window itself (`_effective_transit_window`) is this specific
        pair's own *learned* typical time (docs/DECISIONS.md's "learned
        transit timing" entry) once enough real, unambiguous observations
        exist for it — replacing the flat, generic formula entirely rather
        than just widening it, since real per-house, per-room-pair data is a
        better estimate than one number shared across the whole house.

        Returns `(winner, candidates)`: `winner` is `None` whenever no
        candidate scored above zero *or* the top two were too close to call;
        `candidates` is every Area actually in contention at the decisive
        (nearest) layer — empty if none were ever found, so callers can tell
        "no real ambiguity" apart from "a genuine near-tie" (docs/DECISIONS.md's
        "uncertain births" entry needs exactly that distinction).

        Whenever this resolves to a real `winner`, every decay-eligible Area
        found empty anywhere along the way is credited a "found silent during
        a resolved walk-through" observation (docs/DECISIONS.md's "per-Area
        sensor reliability" entry) — real evidence someone was physically
        there and that Area's own continuous-presence sensor(s) didn't catch
        it. A deliberately simple, scoped approximation: every empty Area
        visited during the search is credited, not just the ones on the one
        true path to `winner` (this BFS doesn't track individual paths, only
        distance layers) — a parallel dead-end branch occasionally gets
        credited too, but only decay-eligible Areas are tracked at all, and
        only a *repeated* pattern (`sensor_reliability_min_misses`) is ever
        trusted, so a rare false credit doesn't skew the result.
        """
        visited = {area_id}
        frontier: dict[str, timedelta] = {area_id: timedelta(0)}
        passed_through: set[str] = set()
        while frontier:
            candidates: dict[str, float] = {}
            next_frontier: dict[str, timedelta] = {}
            for current, extension in frontier.items():
                for connector in self._graph.connectors:
                    if not connector.touches(current):
                        continue
                    other = connector.other_side(current)
                    if other == OUTSIDE or other in visited:
                        continue
                    visited.add(other)
                    if self._counts.get(other, 0) <= 0:
                        # Empty — not a candidate itself, but keep looking
                        # further out along this chain if nothing closer pans out.
                        if other in self._graph.decay_eligible_area_ids:
                            passed_through.add(other)
                        other_extension = extension
                        if self._graph.kind_of(other) is AreaKind.TRANSIT:
                            other_extension += self._config.transit_area_hop_extension
                        next_frontier[other] = other_extension
                        continue
                    last_confirmed = self._last_confirmed.get(other)
                    if last_confirmed is None:
                        continue
                    gap = now - last_confirmed
                    if gap < self._config.min_transit_time:
                        continue  # can't teleport — a second, distinct occupant instead
                    generic_window = self._config.transit_confirmation_window + extension
                    effective_window = self._effective_transit_window(
                        area_id, other, generic_window
                    )
                    score = self._transit_plausibility_score(gap, effective_window)
                    if score > 0:
                        candidates[other] = score
            if candidates:
                winner: str | None
                if len(candidates) == 1:
                    winner = next(iter(candidates))
                else:
                    ranked = sorted(candidates.values(), reverse=True)
                    if ranked[0] - ranked[1] <= self._config.transit_score_tie_margin:
                        winner = None  # too close to call
                    else:
                        winner = max(candidates, key=lambda a: candidates[a])
                if winner is not None:
                    self._record_pass_through_misses(passed_through)
                return winner, candidates
            frontier = next_frontier
        return None, {}

    def _record_pass_through_misses(self, area_ids: set[str]) -> None:
        for area_id in area_ids:
            self._area_miss_counts[area_id] = self._area_miss_counts.get(area_id, 0) + 1

    def _transit_plausibility_score(self, gap: timedelta, effective_window: timedelta) -> float:
        """How plausible `gap` is for the same person to have walked the distance
        `effective_window` already accounts for (docs/DECISIONS.md's "scored transit
        timing" entry): `1.0` for anything within the window itself (identical to the old
        hard-cutoff behavior's "definitely still plausible" case), tapering linearly down
        to `0.0` over an extra `transit_grace_fraction` of that window beyond it, rather
        than rejecting outright the instant the window elapses.
        """
        if gap <= effective_window:
            return 1.0
        grace = effective_window * self._config.transit_grace_fraction
        if grace <= timedelta(0):
            return 0.0
        overrun = gap - effective_window
        if overrun >= grace:
            return 0.0
        return 1.0 - (overrun / grace)

    def _handle_connector_activity(self, signal: ConnectorActivitySignal) -> None:
        connector = self._graph.connector(signal.connector_id)

        if connector.touches(OUTSIDE):
            self._handle_egress_activity(connector, signal)
            return

        area_a, area_b = connector.area_id_a, connector.area_id_b
        count_a, count_b = self._counts[area_a], self._counts[area_b]
        if count_a > 0 and count_b == 0:
            source, dest = area_a, area_b
        elif count_b > 0 and count_a == 0:
            source, dest = area_b, area_a
        else:
            # Both occupied or both empty: this Connector event alone can't
            # resolve a direction, so it's not actionable evidence on its
            # own (SPEC.md doesn't specify a resolution for this case; a
            # conservative "no inference" is the safe default — see
            # docs/DECISIONS.md).
            return

        self._register_pending(connector.connector_id, source, dest, signal.timestamp)

    def _handle_egress_activity(
        self, connector: GraphConnector, signal: ConnectorActivitySignal
    ) -> None:
        inside = connector.other_side(OUTSIDE)
        if self._counts[inside] > 0:
            # Departure: the egress Area was occupied, so the crossing
            # sensor itself is the confirming evidence — OUTSIDE can never
            # corroborate, so unlike a regular transit this confirms
            # immediately rather than waiting on a pending window (see
            # docs/DECISIONS.md).
            self._note_egress_delta(-1)
            self._counts[inside] -= 1
            self._last_confirmed[inside] = signal.timestamp
            self._last_provenance[inside] = signal.provenance
            self._pending.pop(connector.connector_id, None)
        else:
            # Possible arrival: a door/window crossing alone isn't proof
            # someone came in (wind, a pet, retrieving a parcel) — wait for
            # the egress Area's own activity to corroborate.
            self._register_pending(connector.connector_id, OUTSIDE, inside, signal.timestamp)

    def _register_pending(
        self, connector_id: str, source: str, dest: str, fired_at: datetime
    ) -> None:
        self._pending[connector_id] = _PendingTransit(
            connector_id=connector_id,
            source_area_id=source,
            dest_area_id=dest,
            expires_at=fired_at + self._config.transit_confirmation_window,
        )

    def _pending_for_dest(self, area_id: str) -> _PendingTransit | None:
        for pending in self._pending.values():
            if pending.dest_area_id == area_id:
                return pending
        return None

    def _confirm_transit(
        self, pending: _PendingTransit, now: datetime, provenance: ProvenanceTier
    ) -> None:
        source = pending.source_area_id
        if source == OUTSIDE:
            # An egress-point arrival's source is always modeled as OUTSIDE
            # by the connector itself, since only the door sensor fired —
            # but a Connector-adjacent interior Area that was *also* freshly
            # occupied (e.g. a front-yard motion sensor tripping on the walk
            # up to the door, a few seconds before the door itself) is a
            # more plausible true source than a brand-new arrival: without
            # this check, that Area's occupant is left stranded there
            # forever, phantom-duplicating the same person as both "still in
            # the yard" and "a new arrival" (found via a real scripted
            # walkthrough scenario against the actual house topology, not
            # theorized — see docs/DECISIONS.md). Reuses the same
            # timing+adjacency heuristic the sensor-less-connector path
            # already relies on everywhere else, rather than inventing a
            # second one — if more than one neighbor is plausible, it stays
            # ambiguous and falls back to OUTSIDE, same as that path's own
            # "can't resolve a direction" behavior. A genuine near-tie (not
            # just "no candidates at all") is worth remembering as an
            # uncertain birth too, same as the interior-fallback path
            # (docs/DECISIONS.md's egress extension to that entry) — it may
            # yet turn out this wasn't really a fresh arrival from OUTSIDE.
            alt_source, alt_candidates = self._search_plausible_transit_sources(
                pending.dest_area_id, now
            )
            if alt_source is not None:
                source = alt_source
            elif len(alt_candidates) >= 2:
                self._record_uncertain_birth(
                    pending.dest_area_id, alt_candidates, now, is_egress_arrival=True
                )

        if source != OUTSIDE and self._counts[source] <= 0:
            # The source was already drained by a different confirmed
            # transit while this one was pending (two Connectors both
            # inferring the same Area as source) — the original inference no
            # longer holds. Drop it rather than letting a count go negative
            # (SPEC.md §6.2 requires non-negative counts) or fabricating an
            # ungrounded arrival.
            del self._pending[pending.connector_id]
            return

        if source == OUTSIDE:
            # A genuine arrival from OUTSIDE (not reattributed to an
            # already-counted interior neighbor above) is exactly the kind
            # of confirmed door-crossing evidence the egress anchor tracks —
            # see `egress_anchor_total`'s docstring.
            self._note_egress_delta(1)
        if source != OUTSIDE:
            self._counts[source] -= 1
            self._last_confirmed[source] = now
            self._last_provenance[source] = provenance
        self._counts[pending.dest_area_id] += 1
        self._last_confirmed[pending.dest_area_id] = now
        self._last_provenance[pending.dest_area_id] = provenance
        del self._pending[pending.connector_id]
