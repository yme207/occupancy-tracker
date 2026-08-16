# Decisions Log (ADR-style)

A running record of *why*, not *what* — the code and `SPEC.md`/`ARCHITECTURE.md` already say what.
Add an entry whenever a design decision is made, changed, or reversed, per
`docs/AGENT_WORKFLOW.md` §4. Newest first.

Format:
```
## YYYY-MM-DD — Short title
**Decision:** what was decided.
**Why:** the reasoning / what triggered it.
**Alternatives considered:** (if any)
```

---

## 2026-08-16 — Per-Area sensor reliability: learned decay-grace widening from confirmed pass-throughs
**Decision:** Closes the second scope limit noted after the research-recommendation work (the first,
egress-arrival reattribution, closed earlier the same day — see below). `OccupancyEngine` now learns,
per decay-eligible Area, how often that Area has been found *empty* while a transit search walked
straight through it on the way to a candidate that the search then actually resolved against — real
evidence someone was physically there and that Area's own continuous-presence sensor(s) didn't catch
it. `_search_plausible_transit_sources` already walks transparently through empty Areas
(`docs/DECISIONS.md`'s original transit-inference entries); it now also collects which of those
empty Areas are decay-eligible, and — only once the *overall* search actually resolves to a winner,
never on an unresolved/ambiguous search — credits each of them one "found silent during a resolved
walk-through" observation (`_record_pass_through_misses`). Once a specific Area crosses
`sensor_reliability_min_misses` (default 3) such observations, `effective_decay_grace_period(area_id)`
returns `decay_grace_period * unreliable_sensor_grace_multiplier` (default 3x) for it instead of the
flat default; `signal_ingestion.py`'s `_schedule_decay_timer` now schedules against this per-Area
value rather than the engine-wide flat one. Persisted across restarts via
`learned_timing_store.py` (renamed in spirit, not in module path, to "learned engine-statistics
store" — it already existed for learned transit timing and shares that data's lifecycle exactly:
engine-owned, not user-authored, saved together off the same `add_listener` callback). Seeded at
construction (`OccupancyEngine`'s new `learned_sensor_reliability` argument), snapshotted via
`learned_sensor_reliability()`, surfaced in the topology panel's explainability view as
`decay_grace_widened` per Area.

Deliberately a simple, scoped approximation, consistent with how every other recommendation this
session was scoped down from the full generalized version: the BFS credits *every* empty
decay-eligible Area it visits during a resolved search, not strictly only the ones on the one true
path to the winner (the search tracks distance layers, not individual paths, so a parallel dead-end
branch occasionally gets credited too). This is accepted because only decay-eligible Areas
(verified `device_class: occupancy`, already the most trustworthy evidence class) are ever tracked at
all, and only a *repeated* pattern is ever trusted (`sensor_reliability_min_misses`) — a rare false
credit doesn't skew the result the way it would for a single-observation trigger.

**Why:** The project owner chose the ground-truth signal ("cross-check against transit-confirmed
arrivals") and the effect ("widen `decay_grace_period`") via `AskUserQuestion` after I'd flagged, when
closing scope limit 1, that per-sensor reliability learning has no clean self-labeling ground truth
the way transit timing does (a manual `override_occupant_count` correction doesn't cleanly attribute
blame to one specific sensor in a multi-sensor room) — a genuinely new design decision needing its own
conversation, not something to guess at. A resolved multi-hop search walking straight through an
Area's *own* continuous-presence sensor without it ever firing is the cleanest available signal:
"resolved" already means the engine is confident someone made the trip, so a silent decay-eligible
Area along the way is real, if imperfect, evidence about that Area's specific sensor(s) — not a
guess layered on a guess.
**Alternatives considered:** Per-*entity* (not per-Area) reliability — rejected as more precise but
unbuildable cleanly: the engine reasons in Areas, not individual entity ids, and a multi-sensor Area
has no way to tell which specific sensor should have fired. Down-weighting/exempting the Area from
decay eligibility entirely, or just surfacing a `needs_review`-style flag with no behavior change —
both considered and offered to the project owner; they chose the middle option (widen the grace
period) as the one that actually addresses the failure mode (a proven-spotty sensor's "off" reading
shouldn't be trusted as quickly as a reliable one's) without fully disabling decay for an Area that's
still mostly working.

## 2026-08-16 — Uncertain births extended to egress-arrival reattribution
**Decision:** Closes the deliberate scope limit noted in the original "uncertain births" entry.
`_confirm_transit`'s egress-arrival reattribution (an ambiguous door confirmation that could be a
genuinely new person from OUTSIDE, or actually the same already-counted person in a Connector-
adjacent interior Area — e.g. someone in `front_yard`) now calls `_search_plausible_transit_sources`
directly instead of the single-winner `_plausible_transit_source` wrapper, so a genuine near-tie
among 2+ interior candidates records an uncertain birth the same way the interior-fallback path
already does — instead of just discarding the tie and permanently treating it as a fresh arrival.

The one real wrinkle this path has that the interior one doesn't: `egress_anchor_total` already
provisionally counted the door crossing as a real arrival (`_note_egress_delta(1)` — a confirmed
door crossing genuinely happened, so the anchor treats it as real *unless and until* something says
otherwise). `_UncertainBirthRecord` gained `is_egress_arrival: bool`, and
`_resolve_stale_uncertain_births` now undoes that provisional anchor increment (`-1`) whenever an
egress-flagged birth resolves by reattributing to an interior candidate — it's the same real person
relocating between two interior Areas near the door, not a fresh arrival, so the anchor needs to
reflect that once it's understood, not just the count. A non-egress birth is unaffected (the `if
record.is_egress_arrival` guard), and a genuinely unambiguous egress arrival (a single clear
candidate, or truly zero candidates) behaves exactly as before — this only changes the specific
near-tie case that previously fell through to "permanently attributed to OUTSIDE" with no way to
self-correct. 3 new tests (260 total), `ruff`/`mypy`/`pytest` all clean.

**Why:** The project owner asked to close out both scope limits noted after the original "uncertain
births" entry. This one was a well-defined, bounded extension of already-built, already-tested
machinery — no new product decisions needed, since every relevant trade-off (time-erosion only for
inferred guesses, no persistence, small cap) was already settled for the interior-fallback case and
applies identically here.
**Alternatives considered:** Leaving the egress anchor's provisional `+1` in place even after
resolution (simpler) — rejected as inconsistent with what "the anchor represents purely confirmed
door crossings" is supposed to mean; leaving it would let a resolved, understood-to-be-not-a-real-
arrival permanently inflate the anchor, undermining recommendation #1's whole point.

## 2026-08-16 — Learned transit timing: per-Area-pair, fully replaces the flat default, persisted
**Decision:** Recommendation #4, the last of the four research recommendations, and the one that
depended on #2/#3's machinery existing first (per the research report itself). `OccupancyEngine`
now learns its own per-Area-pair typical transit time from the house's own history, and — per the
project owner's explicit instruction — **fully replaces** the flat, generic
`transit_confirmation_window`-based formula for a pair once it has enough real data, rather than
just widening it (see "Alternatives considered" for the earlier, wrong instinct this corrects).

A new `_TransitTimeStats` (pure Python, Welford's online algorithm — numerically stable running
mean/variance, no numpy/scipy dependency) tracks `(count, mean_seconds, m2_seconds)` per unordered
Area pair. A sample is recorded only for the cleanest possible evidence: `_handle_area_activity`'s
interior-fallback path resolving to a *single, unambiguous* candidate (no near-tie — those go to
"uncertain births" instead, not learning) *and* the whole house's `total_occupant_count` was exactly
1 immediately before the event — completely self-labeling data, no guessing about ground truth
required. Once a pair reaches `EngineConfig.transit_learning_min_samples` (5, not user-facing) real
samples, `_effective_transit_window` uses `mean + transit_learning_stddev_margin (2.0, not
user-facing) * stddev` as that pair's effective window *instead of* the flat formula — including
tightening below it, when a pair's real timing genuinely supports that. Below the threshold, the
flat formula is the only reasonable estimate (an unavoidable bootstrap floor, not a permanent hedge
against the learned data — once real data exists, it takes over completely). The existing scored-
timing tapering (`transit_grace_fraction`) from the earlier "scored transit timing" entry still
applies on top of whichever effective window is chosen, learned or flat, so even a tight learned
window still degrades gracefully rather than hard-cutting off.

**Persisted across restarts**, per the project owner's explicit choice this time (unlike recommendation
#3's uncertain births, deliberately left unpersisted) — genuinely accumulated, house-specific learning
built up over weeks is exactly the kind of state actually worth keeping. New `learned_timing_store.py`
(a separate `Store`/module from `topology_store.py` — different owner, different lifecycle, no reason
to couple their schemas) persists via `Store.async_delay_save` (verified from `helpers/storage.py`
source: `data_func` is called at *actual write time*, not when scheduled, and repeated calls within
the delay window coalesce into one write) rather than `async_save`, so a burst of activity walking
through several rooms doesn't mean a disk write per hop. `__init__.py` loads it before constructing
the engine, seeds `OccupancyEngine(graph, config, learned_timing_store.data)`, and registers an
`engine.add_listener()` callback that schedules a debounced save after every signal (cheap even when
redundant — nothing new to detect). `reconcile()` strips learned pairs referencing a since-deleted
Area, mirroring `TopologyStore`'s own hygiene pattern.

22 new tests (257 total: engine unit tests for Welford correctness, the below-threshold/at-threshold
boundary, both tightening and widening once learned, and constructor-seeding; a new
`tests/test_learned_timing_store.py` for the persistence format and debounced-save contract; an
`__init__.py` integration test proving seeded data survives a reload) plus the store module itself,
`ruff`/`mypy`/`pytest` all clean.

**Why:** The project owner pushed back directly on an initial "blend: use whichever is more generous"
framing, correctly pointing out that hedging against the learned data forever (rather than only
during the unavoidable zero-samples bootstrap period) defeated the actual point of "self-tuning" —
and confirmed they specifically want tightening, not just widening, plus real persistence, both
stronger asks than the original recommendation's own softer framing. Both were implemented exactly
as asked once the request was unambiguous.
**Alternatives considered:** The first framing offered to the project owner — permanently taking
`max(learned, flat_default)`, never allowing a learned value to go below the flat default — was
rejected by them specifically, and rightly: it would have meant a genuinely fast, consistent room
pair could never get a correspondingly tight window, undermining the actual precision "learning"
is supposed to deliver. Learning from *any* transit resolution (including near-ties and
egress-anchor-adjacent cases) instead of only the cleanest whole-house-count-of-1 signal — rejected
to avoid training on data that might itself be wrong, keeping this consistent with the "uncertain
births" entry's own preference for the safest available signal. Per-sensor reliability/miss-rate
learning (the research report's other, separate #4 sub-idea) — out of scope for this pass, a
genuinely different statistical problem (sensor trustworthiness, not room-to-room timing) worth its
own future design conversation, not folded in here.

## 2026-08-16 — Uncertain births: a scoped, bounded implementation of research recommendation #3
**Decision:** Recommendation #3 ("track several weighted possibilities instead of one") is
implemented here as **uncertain-birth tracking**, deliberately narrower than a full generalized
particle-filter rewrite: rather than every Area holding a weighted set of complete alternate
per-house pictures, the engine now specifically remembers the exact moments it already identifies
as genuinely ambiguous — a "new occupant" created because 2+ Connector-adjacent Areas scored a
near-tie (`_search_plausible_transit_sources`, extended from `_plausible_transit_source` to also
return the full candidate set, not just the single winner) — and lets *that specific, uncorroborated
guess* self-correct if nothing ever independently confirms it.

New `_UncertainBirthRecord` (internal) / `UncertainBirth` (public, read-only) tracks: the new
occupant's Area, the tied candidates and their scores, and a snapshot of each candidate's
`last_confirmed` at fork time. `_resolve_stale_uncertain_births(now)` — called lazily everywhere
`_expire_pending` already is (`area_state`, `total_occupant_count`, `process_signal`; matches the
existing "freshness label only updates on the next signal or property read" precedent, since the
engine has no timer of its own) — settles any record older than
`EngineConfig.uncertain_birth_resolution_delay` (default 30 minutes, user-facing): if exactly the
original candidates that are *still* occupied *and* untouched since the fork (same `last_confirmed`)
exist, the highest-scoring one is drained, exactly like an ordinary transit's source (the new
occupant's own Area already got its `+1` at fork time, so resolution only ever decrements the
candidate — an early draft double-decremented both, caught by the very first resolution test
failing). If every original candidate has since left or gained fresh evidence of its own, the record
is dropped *without* reattributing — a genuinely more complex situation this heuristic can't safely
reinterpret, so it leaves the birth a permanent, separate occupant rather than risk a wrong silent
merge. `override_occupant_count`/`expire_vacant_area` both now discard any uncertain birth
referencing the Area they touch (as the birth itself or as a candidate), same "a trusted correction
supersedes an unresolved automatic guess" reasoning the pending-transit cleanup already has. Bounded
at `EngineConfig.max_uncertain_births` (default 8, not user-facing) per `docs/ARCHITECTURE.md`'s
memory-bound requirement — oldest dropped first. Exposed via the websocket explainability API
(`uncertain_births`, alongside the existing `pending_transits`). `SPEC.md` §6.2 updated with an
honest, narrow exception (see that section) — this is the one place a purely *inferred*,
never-corroborated guess is allowed to lose confidence from elapsed time alone; `SPEC.md`'s "never
decay direct evidence" guarantee is otherwise completely unchanged. 12 new engine tests + 1 realistic
scenario test reproducing the exact original overnight-overcounting bug end-to-end + 1 websocket
test (241 total), `ruff`/`mypy`/`pytest` all clean.

**Why:** Direct project-owner decisions from a design conversation held before implementation
(matching how every other rework this session happened): (1) an unconfirmed, inferred guess should
be allowed to gently lose confidence purely from elapsed time — but never anything directly
evidenced — since that's the one mechanism that self-corrects a phantom even on a quiet night with
no other house activity to compare it against (the exact scenario that produced the original bug);
(2) the belief state does *not* need to survive an HA restart — simplest version, no new `Store`
schema/migration; (3) the day-to-day room tile/chip stays exactly as it is today, with the new
concept surfaced only via the explainability API and a diagnostic websocket field, not a new
always-visible confidence number; (4) a small, fixed cap rather than the research report's own
32-256 hypothesis range, since real ambiguity in a house-sized graph rarely branches wide.

Scoping this as "remember and resolve specific uncertain ties" rather than "track N complete
alternate pictures of the whole house" was a deliberate choice made *within* implementing those four
decisions, not a fifth one asked separately — a full generalized rewrite would touch nearly every
method in the engine (every mutation path would need to become hypothesis-parametric), reshape what
`AreaState` and the entity platforms expose even in the non-ambiguous common case, and take
meaningfully longer to get right and test thoroughly. This narrower version delivers exactly the
outcome all four decisions actually called for (self-correcting uncertainty, bounded, no
persistence, simple UI) while keeping the change contained to the specific decision points that were
already conservative "don't guess" moments in the existing code — the 229 pre-existing tests needed
zero changes, only additions, which is itself a signal the scoping was right.

**Alternatives considered:** Extending uncertain-birth tracking to `_confirm_transit`'s
egress-arrival reattribution path too (an ambiguous door arrival that could be OUTSIDE or a
neighbor) — deliberately left out of scope for this pass to keep the change bounded to the one path
(`_handle_area_activity`'s interior fallback) that's both the more common trigger and the one the
original bug actually came from; a natural follow-up, not attempted here. Auto-resolving purely by
elapsed time regardless of whether a candidate got fresh evidence since the fork — rejected in favor
of the "untouched since fork" check, since silently merging two genuinely separate people (one who
happened to still be near a stale-but-real candidate) would trade the original overcounting risk for
a new, undercounting one; leaving it unresolved-forever in that specific case is the safer default.

## 2026-08-16 — Scored transit timing replaces the hard confirmation-window cutoff
**Decision:** Recommendation #2 from the research pass, built as its own bounded slice deliberately
kept *underneath* the current single-guess-per-Area model (recommendation #3, tracking several
weighted possibilities at once, is a separate, larger redesign — not started, needs its own design
pass first per `docs/DECISIONS.md`'s "Whole-house conservation" entry's own "Phone/zone-based
anchoring... out of scope" precedent for staged research-recommendation work).

`_plausible_transit_source`'s per-candidate check changed from a hard boolean (`gap` inside
`transit_confirmation_window` + any `transit_area_hop_extension`, or rejected outright) to a
continuous plausibility score (new `_transit_plausibility_score`): `1.0` for anything within the
existing window (identical to the old behavior for the common case), tapering *linearly* down to
`0.0` over an extra `EngineConfig.transit_grace_fraction` (default `0.5`, i.e. 50% more) of that
window beyond it, instead of falling off a cliff the instant the window elapses. Within a BFS layer,
the highest-scoring candidate wins — *unless* it's within `EngineConfig.transit_score_tie_margin`
(default `0.05`) of the next-best, which still resolves as ambiguous (`None`, no guess), generalizing
the old exact-tie rule to near-ties now that scores are continuous rather than booleans. Both new
config fields are deliberately **not** exposed via the options flow — unlike the window durations
themselves, "how gracefully plausibility tapers" isn't something a non-technical user can meaningfully
reason about, matching the existing precedent of `min_transit_time` also staying a code-only tunable.
6 new/updated engine tests (229 total), `ruff`/`mypy`/`pytest` all clean.

Two things the project owner explicitly decided to *not* change in this pass, keeping this the
smaller, lower-risk half of the research recommendation rather than its fuller design: (1) a
near-tied timing call still refuses to guess, exactly as before — the project owner chose to keep
this over letting scoring always pick a highest-scorer, since it preserves an existing, deliberately
conservative guarantee (`test_coincidental_neighbor_retrigger_can_cause_a_transient_overcount` and
similar scenarios stay exactly as documented); (2) "this is a brand-new occupant" stays a *fallback*
used only when no real candidate scores above zero at all, rather than becoming a fully competing
hypothesis with its own baseline likelihood (the research report's fuller framing) — simpler, and a
smaller behavioral departure from today's model to validate.

**Why:** Direct project-owner decision after reviewing the research report's four recommendations,
resolving to pursue the direction "starting small," picking #2 as the second slice (after #1, whole-
house conservation) specifically because it doesn't require reshaping what the engine stores — the
existing 229-test suite (minus one deliberately-updated boundary test, see below) stays a meaningful
regression check rather than needing a simultaneous rewrite.

**Alternatives considered:** A Gamma or log-normal plausibility curve (the research report's own
suggested shape, closer to how a real walking-time distribution actually looks) — rejected for this
pass in favor of a simple piecewise-linear taper: no new statistical machinery/dependencies, easy to
reason about and explain, and recommendation #4 (learning real per-edge timing distributions from the
house's own history) is where a more realistic curve shape would actually pay for itself, once there's
real data to fit it from — building a more sophisticated curve now, with no data to calibrate it
against, would just be guessing at different constants instead of the current ones.
**Test note:** `test_direct_transit_not_inferred_when_source_evidence_is_stale` previously asserted a
gap exactly 1 second past a 90s window read as unrelated — that's now the *intended*, changed
behavior (a near-miss should degrade gracefully, not reject outright), so the test was updated to
probe a gap genuinely beyond the new tapering zone (200s, past the 135s cutoff) instead, and a new
test (`test_direct_transit_still_inferred_just_past_the_window_at_reduced_plausibility`) directly
proves the old boundary case now resolves correctly — the exact shape of the original overnight
walk-test bug this whole line of work started from.

## 2026-08-16 — Whole-house conservation: an egress-anchored total, flagged not capped
**Decision:** New `OccupancyEngine.egress_anchor_total` property (backed by `self._egress_anchor:
int | None`): a whole-house total reconstructed *purely* from confirmed door crossings, starting at
`None` ("unanchored" — no real evidence yet) until the first one happens. `_note_egress_delta()` is
called (before the corresponding count mutation, so the two move in lockstep) from exactly two
places: `_handle_egress_activity`'s confirmed-departure branch (-1), and `_confirm_transit`'s
confirmed-arrival branch when the source genuinely stays `OUTSIDE` (+1) — *not* when it gets
reattributed to an already-counted interior neighbor (docs/DECISIONS.md's 2026-08-15 "egress-arrival
confirmation" entry), since that isn't a new person. The anchor deliberately does **not** move for a
manual `override_occupant_count` correction or a `expire_vacant_area` decay-clear, even though both
are trusted evidence — see "Alternatives considered" below for why that turned out to be the wrong
instinct on first pass. `sensor.py`'s `TotalOccupantCountSensor` gained two new attributes,
`door_confirmed_occupant_count` (the anchor, when established) and `unexplained_by_doors` (`True`
when the interior-inferred total exceeds it) — purely informational, exactly the same "confidence
signal, never a cap" shape `exceeds_household_size_hint` already has. 10 new engine unit tests plus
one end-to-end entity test (226 total), `ruff`/`mypy`/`pytest` all clean.

**Why:** This is recommendation #1 from a background research pass into how this class of problem
(multi-room occupant counting from sparse binary sensors on a known topology) has been solved
elsewhere — see that session's conversation for the full report. The report's framing was a genuine
*hard* constraint ("occupants can only be created/destroyed at a door or via a phone"), but a literal
reading of that directly reverses `SPEC.md` §6.4's deliberate "never reject or cap a count the
evidence actually supports" — a hard version would either invent a fake anchor or silently refuse to
count a real, evidenced occupant (e.g. an existing occupant present at first install, before any
door has ever fired). Project-owner decision, made explicitly before implementation: flag, never
reject — and treat the pre-first-door-event state as "unanchored, no flag," not "assume 0 people,"
so a fresh install with people already home doesn't immediately read as suspicious. Phone/zone-based
anchoring (the report's other suggested anchor source) is out of scope for this slice — this is
egress-crossings only; extending it to `zone_fusion.py`'s tracked-person state is a natural,
separate follow-up, not attempted here.

**Alternatives considered:** The first implementation draft *did* adjust the anchor on
`override_occupant_count`/`expire_vacant_area`, on the theory that trusted corrections shouldn't
leave a just-confirmed total looking "unexplained." Writing the test for `expire_vacant_area`
caught a real bug in that version before it shipped: clearing a phantom occupant that was *never*
anchored in the first place (its birth never touched the anchor) would incorrectly *decrement* the
anchor too, breaking a total that was already correctly in sync. More fundamentally, on reflection,
"unexplained by doors" is simply, permanently true for an occupant who came in through an untracked
entrance — silently folding a correction into the anchor's baseline would hide that fact rather than
report it honestly. Reverted to the simpler rule (anchor only ever moves on a confirmed door
crossing) before implementation was considered done — a case of the test-first discipline in
`docs/TESTING.md` catching a real design flaw, not just a coding bug.

## 2026-08-16 — Topology-panel UI for area-kind override and outdoor-area flag; outdoor Areas get a dotted halo ring, not a stroke reuse
**Decision:** Built the topology-panel UI for the two backend-only fields the same day's earlier
"Decay for continuous-presence sensors..." and "Area-kind classification..." entries below left
unreachable (`area_kind_overrides`, `outside_area_ids`). Two decisions worth recording beyond just
following the plan already agreed with the project owner:
1. **Outdoor-area visual cue is a second, larger dotted ring drawn as its own SVG circle
   (`.outside-ring`), not a reuse of `.node--egress`'s dashed main-stroke treatment.** An Area can
   plausibly be both outdoors *and* a real access point at once (e.g. a front porch with its own door
   sensor) — SPEC.md §5.1's "lingered outside before using the door" scenario is exactly this shape —
   so the two cues need to be visually simultaneous without fighting over one stroke property. A halo
   ring at `NODE_RADIUS + 5` with a distinct dot pattern (`1 4` vs. egress's `3 2`) keeps both legible
   at once and needed no new HA CSS token — reuses `--secondary-text-color`, already verified/used
   elsewhere in this file. Documented in the graph legend alongside the existing egress entry, per this
   session's brief.
2. **`websocket_api.py`'s `_engine_state_json` gained a new `"area_kind"` field per Area** — the
   *effective* (override-or-inferred) `AreaKind`, needed so the panel's "Auto" selector state can show
   what "Auto" currently resolves to. Checked first whether this already existed anywhere in the
   websocket API (it didn't) before adding it, per this task's explicit instruction not to assume a
   backend change was needed. Deliberately minimal: `engine_adapter.py` already computes this at
   graph-build time (`HouseGraph.kind_of`), so this is pure exposure of an existing value, not new
   inference logic — one line, one new test assertion on the existing
   `test_get_engine_state_returns_area_states_and_total`, `ruff`/`mypy`/full `pytest` (216 passed) all
   re-verified clean.
3. **A real, independently-shippable bug found while wiring the new controls' save payload**:
   `topology-panel.js`'s `_saveTopology()` never included `area_kind_overrides`/`outside_area_ids` in
   its websocket call at all, even though `websocket_save_topology`'s message schema has required both
   on every save since the same day's earlier backend session — meaning every topology save from the
   panel (a node drag, any checklist toggle, auto-arrange) would have failed schema validation for any
   user who happened to load the panel after that backend change landed, regardless of whether they
   ever touched the two new fields. Fixed as part of extending the same save-payload object the task
   instructions pointed at, not as a separate change.

The 3-way Auto/Room/Passage selector (project owner's explicit prior choice, not decided this session)
and the outside checkbox both reuse plain, native, already-keyboard-accessible controls (`<button>`,
`<input type="checkbox">`) styled with this file's existing `.tool-btn`/`.checklist-item` classes,
rather than introducing a new HA custom element — consistent with the already-logged 2026-08-09
"`ha-checkbox` swap investigated, deliberately not made" decision (below), and for the same underlying
reason: no browser available to verify an unfamiliar component's event/property contract against the
installed frontend bundle.
**Why:** Direct task brief from the project owner (relayed via a dedicated frontend sub-agent session):
both fields were fully backend-complete and tested but had no UI path at all — the same "tunable with
no UI" gap class Phase 8 was burned by once already (see `docs/STATUS.md`'s Phase 8 entry).
**Alternatives considered:** Reusing `.node--egress`'s dashed main-stroke for the outdoor cue too
(distinguished only by dash spacing) — rejected once the porch/access-point-outdoors overlap case was
considered, since both cues would then compete for the same `stroke`/`stroke-dasharray` CSS property on
the same element. Coloring the outdoor node's fill instead of adding a ring — rejected as harder to
keep legible across both light/dark themes without a dedicated, unverified new color token, whereas a
neutral-toned dotted ring using an already-proven token sidesteps that entirely.
**Not verified in-browser this session** — no browser tool available; `node --check` passes and the
code was re-read for correctness, but per `CLAUDE.md`'s standing UI rule this needs the project owner's
live confirmation before being considered fully done (see `docs/STATUS.md`'s "Thirteenth" entry).

## 2026-08-16 — Decay for continuous-presence sensors, and outdoor Areas excluded from the total
**Decision:** Resolves the remaining half of the 2026-08-15 "transit inference needs a rework" entry
below, plus a separately-raised open item, both per the project owner's explicit direction on the
trade-offs (a design conversation held before implementation, not guessed):

1. **Decay, gated strictly on verified continuous-presence sensors.** `registry_sync.py`'s
   `EntitySnapshot` now carries `device_class` (`entry.device_class or entry.original_device_class`
   — the same precedence Home Assistant's own state-attribute logic uses, verified from
   `entity_registry.py` source, not guessed). `engine_adapter.py` computes `decay_eligible_area_ids`:
   an Area whose *entire* selected evidence list is `binary_sensor` entities with
   `device_class: occupancy` — verified from the installed `BinarySensorDeviceClass` source to
   specifically mean "On means occupied, Off means not occupied," unlike `motion` ("no motion,"
   which says nothing about a still-but-present person) or `presence` (a *person's* home/away state,
   the same concept `zone_fusion.py` already covers — not room-level occupancy; this distinction
   matters and was checked, not assumed). `occupancy_engine.py` gained `expire_vacant_area(area_id,
   now)` (a no-op unless `area_id` is decay-eligible and nonzero) and a `needs_review` flag on
   `AreaState` — purely informational, `True` once a *non*-decay-eligible Area has been `LATCHED`
   and nonzero for longer than `EngineConfig.long_latched_review_threshold` (default 12h). Timing
   itself lives in `signal_ingestion.py`, not the engine (`decay_grace_period`, default 5 minutes,
   exposed via `OccupancyEngine.decay_grace_period`): a new, independent listener per decay-eligible
   Area schedules `homeassistant.helpers.event.async_call_later` once *every* selected entity is
   confirmed `"off"` (verified `async_call_later` signature/return from `helpers/event.py` source —
   its return value is the cancel callable), cancels it on any return to `"on"`, and re-checks live
   state both when scheduling and when the callback actually fires before calling
   `expire_vacant_area` — deliberately *not* "anything that isn't on": an entity going
   `"unavailable"`/`"unknown"` is absence of *data*, not positive vacancy evidence, and treating it
   as such would silently reintroduce the exact failure mode `SPEC.md` §6.2's "never decay on
   absence of signal" rule exists to prevent. `needs_review` is surfaced via the websocket
   explainability API and as a new `sensor.py` per-Area attribute; no automatic count change happens
   for it, `SPEC.md` §6.2's "never silently changes the count" guarantee holds unchanged for every
   Area except a verified decay-eligible one.
2. **Outdoor Areas excluded from `Total Occupant Count`.** `TopologyData.outside_area_ids` (storage
   schema 1.4 → 1.5, validated/reconciled/websocket-exposed the same way as `area_kind_overrides`)
   flags an Area (e.g. `front_yard`/`back_yard`) as genuinely outdoors. `HouseGraph.outside_area_ids`
   excludes it from `OccupancyEngine.total_occupant_count()`'s sum only — the Area's own count,
   quality, and role in transit inference (SPEC.md §5.1's "lingered outside before using the door"
   case) are completely untouched; this is purely a house-level reporting concern.

Both are backend-complete and tested (30 new tests, 216 total — engine unit tests for
`expire_vacant_area`/`needs_review`/`total_occupant_count` exclusion, `engine_adapter` tests for
`device_class`-based eligibility inference, `registry_sync` tests for the device-class resolution
precedence, `topology_store` round-trip/reconcile/validate/migration tests for both new fields,
`websocket_api` save/reject tests, and `signal_ingestion` tests for the timer scheduling/
cancellation/firing/stale-callback/unavailable-handling logic — the last of these using
`unittest.mock.patch` on `async_call_later` specifically, the first mock in this project's test
suite; deliberately scoped to controlling a genuinely real-time async primitive deterministically,
not to faking any HA registry/State/Context object, which `docs/TESTING.md`'s "not a hand-rolled
mock with a different shape than production" rule is actually about). `ruff`/`ruff format --check`/
`mypy`/full `pytest` all clean.

**Explicitly not built this pass:** the topology panel has no UI for either `outside_area_ids` or
`area_kind_overrides` yet — both are backend/storage/websocket-complete but unreachable from the
panel itself, the same class of gap Phase 8 was burned by once already (`docs/STATUS.md`). Delegated
to a dedicated frontend sub-agent session immediately following this entry, since neither this
session nor that one has a browser to verify a Home Assistant panel change against — real
verification still needs the project owner.
**Why:** Direct project-owner decisions, made explicitly before implementation began (not guessed):
decay should auto-clear *and* flag, but only for sensors verified to mean "not occupied" when off;
grace period defaults to 5 minutes precisely because a continuous-presence sensor's "off" is
comparatively trustworthy almost immediately, unlike a PIR's; outdoor Areas should get a real
first-class flag (not a "just don't select evidence for them" convention) since it's the more
correct, durable fix and this project already has the exact same shape of mechanism
(`area_kind_overrides`) to mirror.
**Alternatives considered:** Decaying any Area once all its evidence entities go "off," regardless
of sensor type — rejected, this is precisely the failure mode `SPEC.md` §6.2's original "latch, not
decay" decision (2026-08-08) was designed to prevent, since an ordinary motion sensor's "off" doesn't
mean a still, silent occupant left. Treating `presence`-class entities the same as `occupancy` for
decay eligibility — rejected once the actual HA source was checked: `presence` means a *person's*
home/away state (the same concept zone fusion already covers), not "someone is physically in this
room," so including it would have been a real correctness bug caught only by verifying instead of
assuming both classes meant the same thing.

## 2026-08-16 — Area-kind classification (room vs. transit) added; transit timing now scales with it
**Decision:** Partially resolves the 2026-08-15 "transit inference needs a rework" entry below (the
timing-scaling half; the "nothing ever self-corrects" half is still open, see that entry). A new
`occupancy_engine.AreaKind` enum (`ROOM` / `TRANSIT`) is attached per-Area to `HouseGraph`
(`area_kinds`, default `ROOM` if absent — a `HouseGraph` built before this existed still behaves
identically). `engine_adapter.py` infers it from topology shape alone: an Area is `TRANSIT` only if
it's a through-node (2+ Connectors, including a synthesized egress crossing) **and** has no
activity-evidence entity selected of its own — exactly `SPEC.md` §5.1's own example ("a hallway with
no sensors at all, connecting two sensored rooms"). A dead-end Area never qualifies regardless of
evidence, so an unconfigured-but-genuinely-a-room Area isn't misclassified as a passage just because
nothing's selected yet. `TopologyData.area_kind_overrides` (storage schema 1.3 → 1.4, with a
migration) lets the user manually override the inference per Area — validated (known area, value in
`{"room", "transit"}`) the same way every other topology field is, and treated as engine-relevant
(triggers a reload) since — unlike `area_positions`/`outside_position` — it changes real transit-
timing behavior, not just display.

The engine's own `_plausible_transit_source` (the breadth-first, nearest-candidate-first search from
the 2026-08-15 "multi-hop, nearest-candidate-first" entry) now accumulates a new
`EngineConfig.transit_area_hop_extension` (default 60s) budget once per `TRANSIT`-classified empty
Area it walks through en route to a candidate, instead of checking every candidate against one flat
`transit_confirmation_window` regardless of what's actually being walked through. A same-room or
short adjacent-room gap is unaffected (extension only ever adds to the budget, never subtracts);
a walk through a real hallway/stairwell gets a proportionally larger, still-bounded budget. Exposed
as a new options-flow `DurationSelector` tunable (`transit_area_hop_extension`), same pattern as the
other timing windows.

**Why:** Direct project-owner report: overnight, with one real person in the house the entire time,
`Total Occupant Count` latched at 4 — the same root cause the 2026-08-15 entry already diagnosed
(a flat 90s window doesn't scale with a real, multi-hop, unhurried walk; a missed window strands a
phantom occupant that then never self-corrects, since `SPEC.md` §6.2's latch-not-decay rule is
deliberate and not being reversed). The project owner separately proposed classifying Areas by
topology shape (connectivity/edge-vs-intermediate) specifically so a stairwell/hallway could be
treated differently from a room people actually linger in — this is that classification, scoped
first to the lower-risk, purely-timing-affecting half of the fix (the pure-Python engine layer,
testable against the existing real-house scenario harness) rather than attempting the full rework
(which also touches decay semantics, see below) in one pass, per `docs/AGENT_WORKFLOW.md`'s slice-
sizing guidance.

A new scenario test
(`tests/test_engine_scenarios_realhouse.py::test_slow_walk_through_an_unsensored_stairwell_no_longer_creates_a_phantom_occupant`)
reproduces the reported failure shape directly: kitchen → (unhurried, 140s, no intermediate sensor
firing) → office, a 4-hop walk through `landing`/`stairs`/`entrance_hallway`, of which only `stairs`
classifies as `TRANSIT` in the real topology (the other two have their own selected evidence).
Before this change this exceeded the flat 90s window and produced the reported phantom-occupant
shape; after, the extra 60s budget (150s effective) resolves it correctly as one continuous transit.
A paired guardrail test confirms an implausibly long gap (20 minutes) still correctly falls back to
"new occupant," not an unbounded budget.

**Explicitly not built this pass, tracked for a follow-up session:**
- **The topology panel has no UI to set `area_kind_overrides` yet** — the backend/storage/websocket
  plumbing is complete and tested, but there's no way to invoke it from the panel itself. This
  project has been burned before by a tunable existing in code with no UI path to it (`docs/STATUS.md`'s
  Phase 8 entry) — recording this explicitly rather than letting it go unnoticed is the whole point
  of that lesson. Needs a browser-testable frontend slice, not attempted here (no browser available
  this session either).
- **The decay half of the 2026-08-15 rework is still open.** Project-owner direction (this session):
  when an Area's presence evidence goes quiet, do both — flag a long-`LATCHED` Area for review by
  default, and additionally auto-clear to 0 after a grace period, but *only* for Areas whose selected
  evidence is restricted entirely to continuous-presence-class sensors (e.g. mmWave/radar
  `device_class: occupancy`) — never for ordinary motion sensors, whose "off" doesn't mean "empty"
  (that distinction is exactly why `SPEC.md` §6.2 rejected time-based decay in the first place; see
  the 2026-08-08 "latch, not decay" decision). This needs new plumbing this session didn't build:
  `registry_sync.py` doesn't capture `device_class` at all today (verify against installed HA source
  before use, per `CLAUDE.md`'s hard rule 1 — not yet done), and turning "all qualifying evidence has
  been off long enough" into an actual count change needs a real scheduled deadline per Area (a
  legitimate `async_call_later`-style timer, not a polling loop) plus wiring through
  `signal_ingestion.py` to notice "off" transitions at all (today it only ever reacts to a transition
  *to* "on"). Scoped as its own follow-up slice, not attempted here — it spans registry sync,
  topology store, signal ingestion, and the engine, more than one architectural layer at once per
  `docs/AGENT_WORKFLOW.md`'s slice-sizing guidance.
**Alternatives considered:** Scaling the window by raw hop count regardless of what's being crossed
(the simpler alternative the 2026-08-15 BFS entry already flagged) — rejected in favor of the
area-kind-aware version, since the project owner specifically wants a real room (however many
Connectors it happens to have) to *not* get extra budget just for being well-connected, only a
genuine passage should. Making `AreaKind` a third, engine-owned concept requiring its own explicit
user declaration (like egress points) — rejected as unnecessary manual burden for the common case;
inference-with-override (matching this project's existing "derive, don't hand-declare" pattern
everywhere else) covers it with an escape hatch for the cases shape alone gets wrong.

## 2026-08-16 — Plain-language pass over all end-user-facing text (translations, panel copy, README)
**Decision:** Applied the newly-added `docs/UX_GUIDELINES.md` §5.1 standard across every string an
actual Home Assistant user reads: `translations/en.json`, `www/topology-panel.js`'s string literals
(headings, button/notice/legend/tooltip text — JS logic untouched), and `README.md`'s "What it does"/
"Installation"/"Getting started" sections. `en.json` was already fully compliant from the 2026-08-09
rewrite — verified term-by-term against §5.1's banned-jargon list, no changes made. In
`topology-panel.js`, the two jargon terms with no obvious one-word plain equivalent: "connector" →
"connection" everywhere it was shown as prose or an `aria-label` (the button itself, "Draw
connector"/"Drawing connector…", became the verb phrase "Connect rooms"/"Connecting…" instead of a
direct noun swap, since "Draw connection" read awkwardly as a button label); "transit" (as in "pending
transit") → "move" ("2 moves being confirmed" / "Still confirming a move with Kitchen") since
"transit" has no single plain-English synonym that keeps the "walking between rooms" meaning without
sounding like public transportation. Also fixed a real inconsistency found while auditing: the ring-
color legend hardcoded raw enum-ish words ("confirmed / latched / ambiguous") instead of reusing the
already-correct `QUALITY_LABELS` plain-language map used everywhere else in the same file — changed to
"confirmed / probably occupied / checking" to match.
**Why:** Direct project-owner feedback that the product's language was "very obtuse and complex" with
too much jargon — the same category of feedback that produced the 2026-08-09 options-flow rewrite and
this session's new §5.1 standard, now applied as a full pass rather than left to accumulate
piecemeal. `node --check` confirms `topology-panel.js` still parses (mandatory for this file per the
2026-08-09 "backtick in a CSS comment" entry above); the full pytest suite (181 tests) stayed green,
unaffected since no Python was touched. Not yet browser-verified — no browser tool available to this
pass; see `docs/STATUS.md`'s "Eleventh" Next-action entry.
**Alternatives considered:** For "connector," keeping the noun ("connection") in the button label too
("Draw connection") — rejected as reading like a typo/awkward translation next to "Draw"; the verb-
phrase button label ("Connect rooms") reads more naturally and matches the toolbar's other short
action-verb buttons ("Auto-arrange," "Fit view"). Leaving `docs/*.md` and Python comments/docstrings
untouched — not an alternative actually considered, it's explicitly out of scope per §5.1 itself
(different audience: developers/future coding sessions, not end users).

## 2026-08-15 — [OPEN, needs design work] Real-house walk-test: transit inference needs a rework,
## not a tuning tweak
**Status:** Not fixed. Findings and candidate directions recorded here for a deliberate design
session, per the project owner's explicit call ("I think it will need a rework") rather than a quick
patch during live testing.

**What was observed:** Walking the project owner's real house (single person, ground truth = always
1 occupant) produced a `Total Occupant Count` of 2 on two separate occasions: (1) after
`stairs` → `lounge` (`stairs` stayed latched at 1, `lounge` separately became 1 — a genuinely new
occupant, not a moved one); (2) after `kitchen` → (via `hallway`/`stairs`/`landing`) → `office`
(`kitchen` stayed latched at 1, `office` separately became 1). In both cases the *destination* room's
arrival signal fired but `_plausible_transit_source` found no plausible source, so
`_handle_area_activity` fell through to its "no Connector-adjacent Area plausibly explains this —
new-occupant evidence" branch (`occupancy_engine.py`) instead of correctly draining the room the
person actually walked from.

**Root cause (two compounding structural issues, not a single bug):**
1. **A flat `transit_confirmation_window` (default 90s) doesn't scale with hop count or real
   walking time.** Both failing routes are multi-hop (`lounge` is two hops from `stairs` via the
   sensor-less `hallway`; `office` is four hops from `kitchen` via
   `hallway`/`stairs`/`landing`) — `docs/DECISIONS.md`'s 2026-08-15 nearest-first BFS entry already
   flagged this exact risk under "Alternatives considered": *"Scaling the effective timing window by
   hop count... a real alternative worth keeping in mind if nearest-first turns out to be insufficient
   in practice, not implemented since nearest-first already resolves every scenario tested this
   session."* Every scripted scenario used short, precisely-chosen synthetic timings; a real, unhurried
   multi-room walk — plus real PIR/mmWave sensor detection latency stacked on top of actual walking
   time — routinely exceeds a single flat 90-second budget for anything more than 1-2 hops. This is
   exactly the predicted failure mode, now confirmed in practice, not a new class of bug.
2. **Once a phantom occupant is created, nothing ever corrects it — errors only accumulate, never
   self-heal.** SPEC.md §6.2 is explicit and deliberate: *"Absence of signal never decays it — a room
   only empties when there's positive evidence of an exit."* This is correct and must not be casually
   reversed — it's exactly what makes a stationary, silent occupant (asleep, working at a desk for
   hours) representable at all, and the project's own 2026-08-08 "latch, not decay" decision rejected
   time-based decay for good reason. But it also means a *missed* transit window doesn't just cause a
   momentary blip — it permanently strands a phantom occupant in the room they left, with no
   mechanism to ever clear itself short of a fresh confirmed departure signal from that exact room or
   a manual `set_occupant_count` override. Over real daily use, with many transits and no window ever
   being large enough to guarantee 100% capture, `Total Occupant Count` will drift upward
   indefinitely rather than self-correct — the timing-window issue above determines *how often* this
   happens, but the lack of any self-correction is what makes each occurrence permanent.

**Candidate directions for the rework (not decided, needs the project owner's input on trade-offs):**
- Scale the effective timing budget by hop count/distance instead of one flat window — the
  previously-considered, not-yet-implemented fix from the BFS entry above. Needs a principled way to
  choose the per-hop increment for an unknown house size/sensor mix.
- Per-connector (not just global) tunable windows, since a real hallway-to-lounge walk and a
  four-room traverse plausibly need different budgets, and one global number can't fit both.
- Surface likely-phantom latched rooms to the user for review rather than auto-clearing them — e.g.
  flag a room that's been LATCHED (not CONFIRMED) for an unusually long time, especially if it makes
  `Total Occupant Count` implausible relative to the household-size hint, as a candidate to manually
  correct — this stays consistent with the "never auto-decay" rule (nothing changes automatically)
  while still giving the user a way to notice and fix drift instead of it being invisible.
- Any fix here must **not** reintroduce time-based auto-clearing of a room's count — that's the
  exact thing the 2026-08-08 latch-not-decay decision already rejected, for reasons that still hold.
**Why:** Recorded rather than immediately patched because the project owner explicitly wants this
treated as a real rework, not a tuning tweak — this is the core inference algorithm, and getting the
trade-off between "confirms real transits promptly" and "doesn't fabricate phantom occupants on a
real, imperfectly-timed house" wrong in either direction has real consequences for a system meant to
run unattended.

## 2026-08-15 — LATCHED-with-zero-occupants no longer displayed as "probably occupied"
**Decision:** `topology-panel.js` no longer colors a room node's ring (or labels the detail panel's
quality chip) as LATCHED when that Area's `occupant_count` is 0 — it falls back to the plain default
ring / a "Not occupied" label instead. `occupancy_engine.py`'s actual `StateQuality` enum and
`_quality_for` logic are untouched — SPEC.md §6.8 defines exactly three tiers, and this is a
presentation-layer fix, not a change to that contract.
**Why:** Found live, on the project owner's real house, within minutes of the previous entry's
live-stats change shipping: `_quality_for` only tracks *freshness* of the last confirmation, never
whether the current count is actually nonzero — so a room that's never had any evidence at all
(`last_confirmed is None`, `occupant_count == 0`) reports LATCHED identically to a room that's
genuinely latched *occupied*. Two visible symptoms, same root cause: (1) the new quality-colored node
ring (previous entry) replaced every fresh, still-empty tracked room's plain "this room is tracked"
ring color with the same muted gray as an untracked room, since LATCHED-by-default is the near-universal
starting state — the project owner had been reading that plain ring color as "has evidence selected"
and its disappearance read as a regression; (2) the detail panel showed "Probably occupied" directly
next to "0 occupants" for the Office after its count cleared — a direct, confusing contradiction in
wording. Confirmed and AMBIGUOUS tiers don't have this problem (neither's label/color inherently
claims occupancy in a way a 0 count would contradict), so only LATCHED needed the count-aware guard.
**Alternatives considered:** Adding a fourth `StateQuality` tier (e.g. `UNOBSERVED`) to the engine
itself — rejected for this pass as a larger, SPEC-contract-affecting change that deserves the project
owner's explicit sign-off rather than being folded into an urgent live-testing fix; the presentation-
layer guard fixes both visible symptoms without touching `SPEC.md`'s three-tier definition.

## 2026-08-15 — Live Total Occupancy/quality surfaced on the main graph page, not just per-room
**Decision:** The topology panel's "Areas & connections" page now shows a live Total Occupancy
count and a pending-transits count in the card header, and colors each room node's ring by its
current quality tier (green/gray/orange for confirmed/latched/ambiguous) — reusing the exact same
`--success-color`/`--secondary-text-color`/`--warning-color` tokens the per-room detail panel's
quality chip already uses, so there's nothing new to learn between the two views. A legend entry
explains the ring colors, per `docs/UX_GUIDELINES.md` §6 (don't rely on color alone without a label).
**Why:** The project owner, mid-real-house-deployment, wanted to walk around and watch occupancy
state change live without clicking into each room individually to check it. All of this data
(`total_occupant_count`, `pending_transits`, per-Area `quality`) was already being fetched and kept
live via the existing `occupancy_tracker/engine/subscribe_updates` websocket subscription
(`_engineState`, already a reactive Lit property) — it just wasn't rendered anywhere outside the
per-room detail click-through, so this is a rendering-only change, no new backend surface.
**Alternatives considered:** A separate always-visible per-room quality chip (like the detail
panel's) instead of a ring color — rejected as too visually busy on a graph meant to also show
several rooms and their connections at once; a ring-color change layers onto the existing node
without adding new on-graph text.

## 2026-08-15 — Pre-deployment code audit: evidence-domain gap and stale-registry-name gap fixed
**Decision:** Ahead of the project owner's first real-house deployment, did a full read-through of
every backend module and the frontend panel against `SPEC.md`/`ARCHITECTURE.md`, not just the
passing test suite. Found no crash-causing or data-corruption bugs, but two real
effectiveness/requirements gaps, both fixed:

1. **Evidence checklists now filter to domains that actually work.** `signal_ingestion.py`'s
   `_ACTIVE_STATE` only ever matches a literal `state == "on"` transition, but the topology panel's
   "what counts as activity" and egress-crossing checklists let the user tick *any* entity in a room,
   with copy that explicitly suggested "a TV switching on" as an example (SPEC.md §5.2 itself uses
   "this TV" as an example too). A real `media_player` reports `playing`/`paused`/`idle`, not `on` —
   picking one the way the UI's own copy suggested would silently never register, forever, with zero
   error anywhere. `topology-panel.js` now filters both checklists to
   `SELECTABLE_EVIDENCE_ENTITY_DOMAINS` (`binary_sensor`, `switch`, `light`, `input_boolean` — the
   domains that actually report "on"/"off"), with a distinct "none of this room's entities are
   supported yet" message when a room has entities but none in a selectable domain (as opposed to
   "this room has no sensors at all"). The "what counts as activity" copy's misleading TV example was
   replaced with "a smart plug switching on." No automated test covers frontend JS (per
   `docs/STATUS.md`'s standing note); manual in-browser verification is the check for this one.
2. **A live Area rename now reloads the entry; an unrelated one no longer does.**
   `TopologyStore.reconcile()` only strips references that became outright *invalid* (an Area/entity
   removed or moved) — a pure rename changes nothing it tracks, since `area_id` is stable across a
   rename and only `.name` changes. But `AreaOccupantCountSensor`/`AreaOccupiedBinarySensor` capture
   `area_name` once, at entity-creation time, into `_attr_name` — so a real Area rename (almost
   certainly the single most common registry edit an actual household ever makes) left the HA UI
   showing the *old* room name indefinitely, silently violating SPEC.md §5.3's "must not require the
   user to notice and manually fix things." `__init__.py`'s `_handle_house_shape_changed` now tracks
   the previous `HouseShape` snapshot and triggers `hass.config_entries.async_reload` whenever
   reconciliation actually removed something *or* an `active_area_ids`-tracked Area's name changed —
   scoped to tracked Areas specifically so a rename of some *other*, untracked Area in a busy house
   doesn't cost a reload for no visible effect. Two new tests
   (`test_renaming_an_active_area_reloads_so_entity_names_stay_current`,
   `test_renaming_an_untracked_area_does_not_reload`) cover both the fix and the scoping. 166 tests
   passing (was 164), `ruff`/`ruff format --check`/`mypy` all clean.

Also confirmed, not fixed (documented, deliberate, or genuinely low-risk — see the project owner's
audit-request conversation for the full list): quality-tier freshness and pending-transit expiry only
recompute on the next signal or property read, never on a timer (deliberate anti-polling trade-off,
Phase 4); SPEC.md §8's "diagnostic listing of open ambiguous transits" exists on the websocket API but
isn't exposed as an HA entity/attribute; the frontend panel doesn't live-refresh its house-shape
snapshot after initial load. None of these block a first real-sensor test.
**Why:** The project owner explicitly asked for a thorough pre-deployment audit against requirements,
efficiency, effectiveness, and stability before testing against their real Home Assistant instance —
both fixed issues are exactly the kind of gap that stays invisible against `input_boolean` test
fixtures (which never have a "not on/off" state, and are never renamed mid-session) but would surface
immediately against real devices and real day-to-day HA usage.
**Alternatives considered:** For the evidence-domain gap, extending `signal_ingestion.py` with a
richer per-domain/device-class "is this state positive activity evidence" classifier (e.g. treating a
`media_player`'s `playing`/`paused` as evidence) — rejected as the larger, riskier change for this
pass; restricting the checklist to what already works is the honest, low-risk fix, and the module's
own docstring already flags richer classification as deliberate future work. For the rename gap,
reloading on *any* house-shape change unconditionally — rejected as unnecessary churn (brief
entity-unavailable window) for registry activity this integration doesn't even track.

## 2026-08-15 — `SPEC.md` §13 Q1 resolved: topology *editing* is admin-only, *viewing* is not
**Decision:** `services.py`'s `import_topology` service (a full-topology overwrite, reachable via
Developer Tools or an automation, not just the panel) now requires the calling user to be an admin,
raising `homeassistant.exceptions.Unauthorized` otherwise. `export_topology` (read-only) stays open
to any authenticated user. This makes the policy consistent across every topology-mutation path:
the panel (`panel.py`, `require_admin=True`), the websocket save command (`websocket_api.py`'s
`websocket_save_topology`, already `@require_admin`), and now the plain service. The per-room manual
occupant-count override (`sensor.py`'s `set_occupant_count`, an *entity* service) is deliberately left
at HA's normal per-entity control permission, not admin — it's an operational action scoped to one
sensor a non-admin user may legitimately be allowed to control, not a structural change to the whole
house's topology.
**Why:** Resolves `SPEC.md` §13 Q1 ("should topology editing be restricted to admin users, or open to
any user who can access integration configuration?"), open since before Phase 7. Investigating it
surfaced a real, previously-undiscovered gap: the panel and websocket-save were already admin-gated,
but `import_topology` — an equally destructive action — was not, so a non-admin household member
could bypass the panel's restriction entirely by calling the service directly. `helpers.service.
verify_domain_control` was considered and rejected: it checks per-entity domain *control* permission,
not true admin status, and is deprecated for removal in HA 2026.10 (about two months out) — verified
by reading `helpers/service.py`'s actual source (`.venv-wsl`'s installed `homeassistant` package), not
assumed. The fix instead mirrors that same decorator's own internal admin-lookup pattern
(`hass.auth.async_get_user(call.context.user_id)` + `user.is_admin`) directly, without the deprecated
wrapper. Two new tests (`test_import_topology_rejects_non_admin_caller`,
`test_import_topology_allows_admin_caller`, using pytest-homeassistant-custom-component's
`hass_read_only_user`/`hass_admin_user` fixtures) cover both sides. 164 tests passing (was 162),
`ruff`/`ruff format --check`/`mypy` all clean.
**Alternatives considered:** Leaving `import_topology` ungated on the theory that anyone with access
to call HA services already has meaningful access — rejected because it directly contradicts the
already-shipped, deliberate `require_admin` on the panel and websocket path; the service was simply an
overlooked bypass of that existing policy, not a considered exception to it.

## 2026-08-15 — MIT license added; CI's manifest key order and HACS repo-metadata gate resolved
**Decision:** Added a root `LICENSE` file (MIT, copyright `yme207`), resolving `SPEC.md` §13 open
question 3 in favor of a permissive license — the norm for HACS community integrations, with no
reuse restrictions that would conflict with HACS distribution. Also reordered
`custom_components/occupancy_tracker/manifest.json`'s keys to `domain`, `name`, then alphabetical
(`codeowners`, `config_flow`, `dependencies`, `documentation`, `integration_type`, `iot_class`,
`issue_tracker`, `single_config_entry`, `version`) — `issue_tracker` was previously placed before
`integration_type`/`iot_class`, violating hassfest's required ordering.
**Why:** The `hassfest` and `hacs` CI jobs were both red. `hassfest` failed outright on the manifest
key ordering. The `hacs` job failed on two independent things: `check-repository` (missing
description/topics — real GitHub repo settings, not files this repo controls, left for the project
owner to set via the GitHub UI) and `check-license` (no `LICENSE` file — fixed here) plus a
`check-manifest`/`integration_manifest` error ("expected a dictionary. Got None") whose likely cause,
per a matching upstream report (`hacs/integration` issue #5252, "HACS manifest validation failing
incorrectly"), is the same key-order defect crashing the hacs action's internal hassfest-equivalent
call rather than an actual problem with `manifest.json`'s contents (which already contained every
field HACS requires: `domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version`) —
expected to resolve alongside the ordering fix; confirm on the next CI run before assuming otherwise.
**Alternatives considered:** Apache-2.0 — considered, but MIT is more standard for this integration's
category and audience; the project owner chose MIT directly.

**2026-08-15 follow-up:** The project owner pasted the next Actions run: `hassfest` and `lint-and-test`
both went green (confirming the key-order fix), and `hacs`'s `check-license` cleared (confirming the
`LICENSE` fix, 5/8 → 4/8 checks failed). `check-manifest`/`hacsjson` ("expected a dictionary. Got
None") is still failing, unaffected by either fix — so that error was never actually caused by the
key-order bug, despite the plausible-looking upstream report this entry originally cited. Re-searched
and found the exact match: `hacs/integration` issue #5252, same error on an equally minimal
`hacs.json`, closed by the reporter two days after filing with "It appears to be working again" and no
maintainer-identified root cause — a real, previously-documented intermittent flake in HACS's own
validation service, not a defect in this repo's files. Added `github_token:
${{ secrets.GITHUB_TOKEN }}` explicitly to `.github/workflows/ci.yml`'s `hacs` job as a cheap,
non-destructive belt-and-braces change (the action's own default is already `${{ github.token }}`, so
this may not change anything, but a couple of HACS's own past fixes were specifically about
token/ref handling in this exact manifest-fetch path). `check-repository`'s missing description/topics
remain, unchanged — still a GitHub repo-settings action for the project owner, not a file this repo
controls.

**2026-08-15 second follow-up:** The project owner set the repo's description and topics via the
GitHub UI; a re-run (same commit `aca401c`, no code change) confirmed `check-repository` now passes
(4/8 → 2/8 failed). `check-manifest`/`hacsjson` failed identically on the re-run, weakening the
pure-transient-flake read. Traced HACS's actual `validate_repository` source
(`hacs/integration`'s `repositories/integration.py`): it resolves the integration folder by scanning
the repo tree under `custom_components/` (this repo has exactly one folder there, `occupancy_tracker`
— unambiguous, not the cause), then fetches and JSON-decodes `manifest.json` via GitHub's API. Since
`hassfest` — HA's own first-party manifest validator — parses that same file cleanly with every
required key present, the defect is somewhere in HACS's fetch/decode path itself, not in this repo's
files; deeper tracing wasn't possible without literal source access. Importantly, HACS's own docs
describe `check-manifest`/`check-repository` as part of the *default-repository submission* review
process, not a gate on custom-repository (add-by-URL) installs — this project's near-term
distribution path per the 2026-08-08 "HACS validation ignores the brand-assets check" entry. Treating
this as a known, non-blocking CI gap rather than continuing to guess at further local changes with no
new evidence; revisit only if/when actually pursuing HACS default-repository submission (`SPEC.md`
§13 Q3, still open) or if a future CI run surfaces new information.

## 2026-08-15 — `_plausible_transit_source` now searches multi-hop, nearest-candidate-first, through empty Areas
**Decision:** `occupancy_engine.py`'s `_plausible_transit_source` no longer only checks an Area's
*direct* Connector-adjacent neighbors. It now does a breadth-first search outward through any chain of
currently-*empty* Areas (treating them as transparent), stopping at the first "layer" of the search
where any occupied, timing-plausible candidate exists — a real 1-hop neighbor always wins over a
farther one that merely also happens to fall inside the same flat `transit_confirmation_window`.
Ambiguity (returning `None`, the existing "don't guess" rule) only applies among ties at that same,
nearest distance.
**Why:** `SPEC.md` §5.1 explicitly requires this: *"An Area with zero devices/entities is still a valid
graph node... e.g. a hallway with no sensors at all, connecting two sensored rooms."* The real house
topology has exactly this shape (`stairs`, connecting `entrance_hallway` and `landing`, has no sensor of
its own), and a scripted scenario against it
(`tests/test_engine_scenarios_realhouse.py::test_unsensored_room_is_correctly_skipped_as_a_pass_through`)
showed the *previous* single-hop-only version simply couldn't satisfy this at all — `landing`'s own
direct neighbors don't include `entrance_hallway`, only `stairs` does, and `stairs` itself is never
occupied (nothing ever signals for it), so the old code found zero candidates and treated every
hallway-to-landing walk as a brand-new, unrelated occupant instead of a continuous transit.

The *first* version of this fix (plain unbounded depth-first search, landing every reachable occupied
Area into one candidate set regardless of distance) introduced a new, real regression, also caught by a
scripted scenario before it shipped: a person walking `living_room` → `entrance_hallway` could get
spuriously blocked because `bedroom`, three empty hops away via `stairs`/`landing`, *also* fell inside
the same 90-second window purely by coincidental timing — an obviously-implausible farther candidate
was allowed to tie with (and thus veto) an obviously-correct 1-hop one. The nearest-first,
stop-at-the-first-non-empty-layer redesign fixes this: a real 1-hop candidate is now always preferred,
and the search only continues outward when *nothing* closer exists at all.

**A related, deliberately un-fixed finding from the same testing round**: even with the above working
correctly, an *unrelated* occupant merely re-confirming their own already-occupied room (a common,
everyday PIR-sensor behavior — someone shifts in their chair, retriggering a motion sensor with no
actual movement between rooms) can still create a spurious tie against a genuinely separate, unrelated
transit happening nearby at the same time, if both rooms are equidistant Connector-neighbors of the
same destination
(`tests/test_engine_scenarios_realhouse.py::test_coincidental_neighbor_retrigger_can_cause_a_transient_
overcount`). This falls back to the *same*, already-established "ambiguous, don't guess" behavior — it's
not a new failure mode, just a newly-characterized trigger for an existing, accepted trade-off — but it
does mean the model is more sensitive to ordinary sensor noise than the current tests previously probed
for. Not fixed this session: distinguishing "genuinely fresh arrival evidence" from "just a
re-confirmation of someone who was already there" would need tracking more state per Area (e.g. a
separate "became occupied at" timestamp, distinct from "last confirmed at") — a real design question for
the project owner about how much of that complexity is worth adding, not a quick patch.

**A second, separately-documented finding from the same round**: an 8-hour-later "cold" motion event
(someone waking up with no intermediate stirring signal) similarly can't be attributed back to a
long-latched source, for the same reason (the gap is far outside any realistic transit window) —
`tests/test_engine_scenarios_realhouse.py::test_silent_long_gap_wake_is_a_known_overcounting_limitation`
documents this as a known, accepted trade-off of the timing-window model rather than something to chase
here.
**Alternatives considered:** Bounding the search to a fixed maximum hop count instead of "nearest layer
wins" — rejected as an arbitrary number with no principled way to choose it for an unknown house size,
where nearest-first has no such tunable and degrades gracefully on its own. Scaling the effective timing
window by hop count (a further-away candidate needs proportionally more elapsed time to qualify) —
a real alternative worth keeping in mind if nearest-first turns out to be insufficient in practice, not
implemented since nearest-first already resolves every scenario tested this session.

## 2026-08-15 — Egress-arrival confirmation now prefers a freshly-active interior neighbor over OUTSIDE
**Decision:** `occupancy_engine.py`'s `_confirm_transit` no longer unconditionally attributes a
confirmed egress-point arrival to `OUTSIDE`. When the pending transit's source is `OUTSIDE`, it now
first checks `_plausible_transit_source(dest_area_id, now)` — the same timing+adjacency heuristic
already used for ordinary sensor-less-Connector inference — and, if exactly one Connector-adjacent
interior Area has fresh, plausible evidence, decrements that Area instead of treating the arrival as a
genuinely new occupant.
**Why:** Found via a scripted, known-ground-truth walkthrough scenario against the real house topology
(`tests/test_engine_scenarios_realhouse.py`, built this session specifically for this kind of
algorithm-iteration testing — see `docs/STATUS.md`'s Phase 8/9 boundary entry), not by inspection: a
single person triggering `front_yard`'s motion sensor a few seconds before opening the front door was
being counted as **two** people — `front_yard` stayed at 1 (its own destination-only inference had no
way to know a door-crossing was coming and no later signal ever cleared it) while the door's own
pending-transit confirmation *always* attributes its source to `OUTSIDE` unconditionally, adding a
second, independent occupant. `front_yard`/`back_yard` being real, sensor-equipped Areas adjacent to an
egress point (not the egress point itself) rather than a purely synthetic "outside" node is exactly the
real topology's own shape (see the 2026-08-09 "Outside node removed" entry — access points were always
just Areas with a crossing-entity list, never a special node type), so this wasn't a contrived edge
case.

The fix reuses `_plausible_transit_source` rather than a new mechanism, so it inherits that function's
existing safety properties for free, each separately verified by its own scenario test: a genuinely
unrelated occupant with stale (outside the confirmation window) evidence is left alone rather than
misattributed as the door's source, and if more than one neighbor is simultaneously plausible, the
result stays ambiguous (a new arrival, no neighbor drained) rather than guessing — the same "can't
resolve a direction, don't guess" rule the ordinary Connector path already applies. A deliberately
*not*-fixed, closely-related case, left as a documented limitation rather than chased further: if the
neighboring Area's activity fires *after* the egress arrival is already confirmed (person arrives,
*then* triggers an adjacent Area's sensor on their way further in), the existing sensor-less-connector
heuristic can misread that as "walking backward" out of the just-arrived Area — but that's a pre-existing
property of the timing+adjacency heuristic in general (it has no directional/sequence awareness), not
something this fix introduces, and fixing it would need a larger, sequence-aware redesign of that
mechanism, not a targeted change to the egress path alone.
**Alternatives considered:** Always trusting the door sensor's `OUTSIDE` attribution unconditionally
(the prior behavior) — rejected, it's the confirmed bug. Inventing a separate, egress-specific
adjacency heuristic instead of reusing `_plausible_transit_source` — rejected as needless duplication of
logic that already exists and is already tested for exactly this "timing+adjacency, single-candidate-
only" judgment call.

## 2026-08-15 — Panel `topology-panel.js` needs a cache-busting URL, and JS regex `\b` doesn't split on `_`
**Decision:** `panel.py`'s registered `module_url` now has a `?v=<file mtime>` query string appended
(computed once at panel registration, from the file's own `stat().st_mtime`). Separately, the "Use it"
suggestion's name-matching regex (see the chip-redesign entry above) was rewritten from `/\bmotion\b/i`
to `/(?:^|_)(?:motion|occupancy|presence)(?:_|$)/i`.
**Why:** Two independent, real bugs surfaced during this session's live browser-testing loop with the
project owner (this session had no browser tool of its own — same "project owner tests, reports back"
loop prior Phase 7/8 sessions used). First, the suggestion simply never appeared, in a way that
persisted through a hard refresh *and* a fresh incognito window — traced to HA's static-path serving
defaulting to aggressive, long-lived caching (`cache_headers=True`, verified from
`homeassistant/components/http/server.py`'s `StaticPathConfig`) combined with the HA frontend being a
PWA with its own service worker, neither of which a plain reload reliably bypasses. `topology-panel.js`
had no versioning in its URL at all, so a browser that had ever loaded it could keep serving a stale
copy indefinitely — true for local dev iteration and for a real end user's browser after a HACS update.
Fixed with the mtime-based query string above (chosen over keying it to `manifest.json`'s `version`,
which would require remembering to bump it on every change — exactly the kind of discipline-dependent
process this bug came from in the first place).

Second, *even once the caching issue was fixed*, the suggestion still didn't appear — a second,
separate root cause found by actually testing the regex in Node rather than reasoning about it (the
original version shipped without that check): `\b` in JavaScript treats `_` as a word character, so
`/\bmotion\b/` never matches inside
`landing_motion` — there's no boundary between "landing" and "motion" since both sides of the `_` are
word characters. This would have silently broken the suggestion for every real HA entity, not just
this dev instance's `input_boolean` fixtures, since snake_case is the near-universal HA entity-id
convention. The replacement pattern was verified against 10 real test cases (including the false-
positive risk "emotion"/"promotion") via an actual Node run before shipping.
**Alternatives considered:** For the cache-bust: `manifest.json`'s `version` field (rejected — requires
manual-bump discipline, doesn't fix the class of bug that caused this session's confusion in the first
place). For the regex: splitting the object id on `_` and checking for an exact-match token (equivalent
result, chosen the anchored-regex form instead since it reads more directly next to the pattern it's
replacing).

## 2026-08-15 — Quality chips redesigned as a neutral pill + color dot, not a colored pill + white text
**Decision:** The three engine-state quality chips (`sensor.py`/panel detail view — Confirmed/Probably
occupied/Checking…) now render as a neutral pill (`--secondary-background-color` /
`--primary-text-color`, the same tokens `.badge` already used elsewhere in the panel) with a small
solid-color dot carrying the state color, instead of a solid colored pill (`--success-color`/
`--warning-color`/`--secondary-text-color`) with white text.
**Why:** `docs/UX_GUIDELINES.md` §6/§7 require sufficient color contrast in both themes, checked off as
still-open in `docs/STATUS.md`'s Phase 8 follow-up list. Actually checked this session (verified
against the real color values in the installed `home-assistant-frontend` 20260729.6 bundle, not
guessed): white text on `--success-color` (`#43a047`) is ~3.3:1, on `--warning-color` (`#ffa600`) is
~2:1 — both fail WCAG AA's 4.5:1 for small text in *both* themes, since those tokens are theme-invariant.
White text on `--secondary-text-color` was worse than it looked: it passes in light theme (`#5e5e5e`,
~6.5:1) but is nearly invisible in dark theme (`#ccc`, ~1.6:1), since that token deliberately inverts
brightness by theme. A neutral pill sidesteps per-token contrast tuning entirely, since
`--primary-text-color` on `--secondary-background-color` is a pairing HA's own themes already
guarantee stays legible — the color moves to a small decorative dot instead of carrying the text.
**Alternatives considered:** Picking a different literal text color per chip per theme — rejected,
since it would need `prefers-color-scheme`-style forking that fights this project's rule of theming
exclusively through HA custom properties, and would need re-tuning against every custom HA theme, not
just the two built-in ones just checked.

## 2026-08-15 — House-level entities need an explicit `entity_id`, or HA silently joins the device name into it
**Decision:** `TotalOccupantCountSensor` (`sensor.py`) and `PreArmedBinarySensor` (`binary_sensor.py`)
now set `self.entity_id` explicitly (`"sensor.total_occupant_count"` /
`"binary_sensor.pre_armed"`) in `__init__`, alongside their existing `_attr_device_info`.
**Why:** Phase 8's device-grouping feature (`_attr_device_info` linking both entities to the
virtual `DeviceEntryType.SERVICE` device) had a real, undiscovered side effect: for a **brand-new**
entity (no pre-existing registry entry to match by `unique_id`), HA's entity-id generation does not
skip the device-name join just because `has_entity_name` is `False`. Traced through the installed
`homeassistant` 2026.8.1 source (not guessed, per `CLAUDE.md`'s hard rule): `entity_platform.py`'s
`_async_derive_object_ids` only routes an entity's suggested name into `entity_registry.py`'s
`suggested_object_id` parameter (documented, and verified in `_async_get_full_entity_name`, to skip
the device-name join) when `entity.internal_integration_suggested_object_id` is already set — which
only happens when the entity's own `entity_id` was set explicitly before being added. Without that,
the name flows into `object_id_base` instead, which gets joined with the device's name regardless of
`has_entity_name` whenever there's no existing prefix to strip — silently producing
`sensor.occupancy_tracker_total_occupant_count` instead of the documented, spec'd
`sensor.total_occupant_count`. Caught by 6 failing tests in the full suite this session, not by
inspection — this project's own dev instance was never exposed to it (its two entities were created
2026-08-08, before device-grouping existed, and HA doesn't rename an existing entity's `entity_id`
just because a later reload's naming logic would suggest something different), but any fresh install
from this point forward would have hit it.
**Alternatives considered:** Dropping `_attr_device_info` entirely (loses the "Visit" link back to the
panel from Settings → Devices & Services — a deliberate, project-owner-verified Phase 8 feature, not
worth reverting). Setting `_attr_has_entity_name = True` (would also change the entities' *friendly
names* to "Occupancy Tracker Total Occupant Count" / "Occupancy Tracker Pre-Armed" — a real
user-visible naming change beyond what this bug required fixing). Setting
`internal_integration_suggested_object_id` directly — its own docstring says "Only handled
internally, never to be used by integrations," ruled out on inspection.

## 2026-08-15 — Test bug: faking a topology-selected entity via `hass.states.async_set` isn't enough
**Decision:** `test_setup_entry_skips_entities_for_untracked_areas`,
`test_reload_removes_entities_for_areas_deselected_entirely` (`tests/test_init.py`), and
`_setup_entry_with_tracked_kitchen` (`tests/test_services.py`) now register their test motion entity
properly via `entity_registry.async_get_or_create(...)` +
`entity_registry.async_update_entity(..., area_id=kitchen.id)` before selecting it as activity
evidence, matching the pattern every other test in the suite already used (see e.g.
`tests/test_entities.py`).
**Why:** `RegistrySync._build_house_shape()` builds its `HouseShape` purely from the entity registry
(`registry_sync.py`), never from bare `hass.states`. These three call sites instead did
`hass.states.async_set("binary_sensor.kitchen_motion", "off")` with no registry entry at all, so
`TopologyStore.reconcile()` correctly (per its own dangling-reference-stripping logic) dropped the
selection on every save/reload, and the room was never actually tracked — the assertions failed not
because of a product bug, but because the test never gave the code a real entity to find. This was
caught the same way as the entity-id bug above: running the full suite surfaced 3 of the 6 failures
this session, unrelated to that other bug, both predating this session (zero Python was touched before
this fix) despite `docs/STATUS.md`'s Phase 8 entry claiming these exact three `test_init.py` tests
were added and verified "clean" — that claim did not hold up against re-running the suite.
**Alternatives considered:** None — this is a straight correction to match the already-established,
correct pattern used everywhere else in the suite, not a new testing approach.

## 2026-08-09 — Reverted the `@yme207` placeholder: it was the project owner's real handle all along
**Decision:** `manifest.json`'s `codeowners`/`documentation`/`issue_tracker` now point at
`@yme207`/`github.com/yme207/occupancy-tracker` again, and `README.md`'s "Installation" section has a
concrete `git clone https://github.com/yme207/occupancy-tracker.git` instead of a URL-less "clone
this repository."
**Why:** Earlier this session's Phase 8 audit found `@yme207` in these fields and assessed it as "a
previous *real-looking* but wrong GitHub handle," swapping in a `TODO-set-your-github-username`
placeholder on the assumption the project owner hadn't set up a real repo yet. That assumption was
never actually verified against anything — it was inferred from the handle simply looking unfamiliar/
unrelated. Running `git remote -v` while pushing this session's other work showed `origin` already
set to exactly that repo, already synced, with prior real commits authored by the project owner —
who then directly confirmed it's their actual GitHub identity. The original find wasn't a bug at all.
**Alternatives considered:** None — this is a straight revert of an overcorrection, not a new design
choice. The general lesson (recorded here rather than left implicit): an identifier "looking
unfamiliar" is not evidence it's wrong — for anything with a real, checkable source of truth nearby
(here, `git remote -v`, sitting one command away), check it before overwriting real user data with a
placeholder, the same standard this project already holds itself to for HA APIs.

## 2026-08-09 — Removed the synthetic "Outside" graph node and its edges entirely
**Decision:** The topology panel no longer draws a shared "Outside" node or the dashed edges
connecting every access-point Area to it. Access points are shown purely via the existing per-node
dashed ring (`.node--egress`), now the sole visual cue, with the legend updated to describe it
("Dashed ring = this room has an access point"). `OUTSIDE_ID`, `_defaultOutsidePosition()`, and all
outside-node positioning/dragging logic were deleted from `topology-panel.js`. `_saveTopology()` now
always sends `outside_position: null` rather than tracking a position for a node that no longer
exists — the websocket save schema's `outside_position` field stays `vol.Required` but already
accepts `None` (`_OUTSIDE_POSITION_SCHEMA = vol.Any(None, _AREA_POSITION_SCHEMA)`), so no backend
change was needed at all; the field is simply unused going forward rather than removed via a storage
migration.
**Why:** Project owner flagged (with screenshots) that every access-point Area converging on one
shared, arbitrarily-positioned "Outside" node produced messy crossing diagonal lines once a house had
more than one or two access points, and asked what value the shared node actually added over just
marking each access-point room individually. The engine's own `OUTSIDE` concept
(`engine_adapter.build_house_graph()`'s synthesized outside-facing connector per egress point, used
for asymmetric departure/arrival transit confirmation) is entirely independent of this UI node — it's
derived straight from `egress_points`, never from anything the graph draws or from `outside_position`.
The visual node had no other function (no detail panel, no state, click-to-drag only), so it was pure
decoration duplicating information the per-node dashed ring already conveyed — and confusingly so,
once a user's house has its own real "outside" Areas (e.g. a Front Yard/Back Yard the user created
themselves), which now look like a second, competing representation of the same idea.
**Alternatives considered:** Keeping the node but only drawing it once regardless of egress count
(already how it worked) and just cleaning up the auto-layout to reduce line crossings — rejected,
since the node itself added no information beyond what the dashed ring already shows; simplifying the
data model (delete the node) is a clearer fix than better-arranging a node that shouldn't exist.
Removing `outside_position` from the storage schema entirely — rejected as unnecessary churn (a new
migration, a backend test update) for a field that's already harmless to leave unused; revisit only if
schema cleanup is undertaken for other reasons.

## 2026-08-09 — Connector/egress lines trimmed to node edge; live occupant count in active nodes
**Decision:** `topology-panel.js`'s connector and egress-to-outside lines are now drawn between each
node's circle *edge* (a new `pointTowardsEdge(from, to, radius)` helper computes the trimmed
endpoint), not between the two node centers. Every active Area's node also now shows its live
occupant count as a `<text>` label inside the circle, sourced from the same `_engineState` the detail
panel's explainability inspector already subscribes to — no new backend call, no new subscription.
**Why:** Project owner reported (with a screenshot) that a connector line was visibly cutting straight
through a room's circle rather than stopping at its edge. Root cause: the line's endpoints were always
the node centers, which only stays hidden inside the circle if the circle is fully opaque and painted
on top — true in the common case, but not for a dimmed/inactive node (see the `.node--inactive` fix
below) or any theme where `--card-background-color` isn't fully opaque. Trimming to the circle's edge
removes the dependency on either of those assumptions rather than patching just the one case that was
visibly broken. The occupant-count label was requested in the same message as a lower-friction way to
see a room's current count without opening its detail panel — inactive/untracked nodes deliberately
don't get one, matching this project's existing engine-vs-entity display split (an inactive node has
no real entity backing a count, so showing "0" for it would misrepresent it as tracked-and-empty
rather than not-tracked-at-all).
**Alternatives considered:** Fixing only the `.node--inactive` opacity rule (see the entry immediately
below) without also trimming the lines — rejected as treating a symptom rather than the cause; a
translucent theme background could reproduce the same visible-line bug on an otherwise "fully active"
node with no dimming involved at all.

## 2026-08-09 — Real JS syntax checking is available after all: Node.js via the Windows path
**Decision:** Frontend JS changes to `topology-panel.js` can now be syntax-checked with
`node --check <file>` before shipping, using the Windows-side Node install reachable from WSL at
`/mnt/c/Program Files/nodejs/node.exe` (or, from a plain Windows shell/Git Bash, just `node` — it's
on the Windows `PATH`). Not on WSL's own `$PATH` (a separate environment), which is why earlier
sessions concluded no Node.js was available at all and fell back to manual re-reading of every
template-literal edit.
**Why:** A real regression this session (see the entry directly below) shipped past manual review
because nothing actually executed the file before it reached the browser. Once the bug was live,
`node --check` caught it immediately and unambiguously — a five-second command that manual re-reading
had already missed twice in a row. `pip install esprima` was tried first and produces a false
positive on this file (its parser predates class-field syntax like `static properties = {...}`,
which this file has used since Phase 7b-i) — worth remembering so a future session doesn't waste time
on esprima again or wrongly distrust a real error it reports elsewhere in this file.
**Alternatives considered:** Installing Node inside the WSL venv via `apt` — unnecessary now that a
working install is already reachable at the path above; simpler to just call it by its full path (or
add it to WSL's `$PATH` once, e.g. via `.bashrc`, if this comes up often enough to be worth the
one-time setup).

## 2026-08-09 — Fixed: a literal backtick inside a CSS comment broke the whole styles block
**Decision:** `topology-panel.js`'s `static styles = css\`...\`` block is one single JS template
literal (lit's `css` tag is just a function called on a tagged template — there's no special parsing
that protects backtick characters inside a `/* CSS comment */` written inside it). A markdown-style
inline-code backtick in a comment (`` `opacity` ``) terminated the literal early, and everything after
it parsed as broken JS. Removed the backtick from that comment; `node --check` (see the entry above)
now confirms clean syntax.
**Why:** Shipped as part of the connector-line/occupant-count-label fix below, then found live by the
project owner ("the Areas and connection window no longer loads... it is just blank") — a *fully*
blank panel (not one of the panel's own coded loading/error/empty states) is the signature of the
whole custom element failing to even parse, not a runtime logic bug; confirmed directly from HA's own
server-side capture of the browser's console error (`frontend.js.modern` logs client JS errors that
reach it) rather than guessed: `SyntaxError: unexpected token: identifier` at the exact broken line.
**Alternatives considered:** None — this was a straightforward typo-class bug once actually diagnosed;
the only real lesson is the tooling one captured in the entry above (verify before shipping, not just
re-read).

## 2026-08-09 — Topology panel UI/UX pass: legend placement, alignment fix, plain language
**Decision:** In `topology-panel.js`'s main card: the graph legend (line/dashed-edge meaning) moved
out of the top explainer text into a new `.graph-footer` block below the graph, alongside the
caption; the caption's CSS `text-align: center` was removed so it aligns left like everything else on
the card; quality/provenance labels and all card copy (subtitle, empty/loading/error states, section
headings, checklist descriptions) were rewritten in plain language, matching the standard already
applied to the options-flow translations (see the entry below). A new inactive-room notice (reusing
the existing `.empty-topology-notice` style) was added to the detail panel for any Area with nothing
selected, and inactive Area nodes are now visually dimmed in the graph (`.node--inactive`).
**Why:** Project owner reviewed the panel live and sent an annotated screenshot: the legend text was
mixed into the top explainer paragraph rather than living with the caption at the bottom, and
alignment was inconsistent across the card (centered caption against an otherwise left-aligned
layout) — both were real, specific, pointed-to defects, not general polish requests. The plain-
language rewrite was requested separately but at the same time ("carry out a full ui and ux review of
this page... apply same simplified language") — the same "assume no technical background" standard
already used for the options-flow field descriptions. The inactive-room visual cue is required by the
per-area entity pruning decision below: an Area that no longer has any entities needs a visible reason
why, not just silently-missing sensors.
**Alternatives considered:** Introducing a new synonym ("way outside") for "access point" while
rewriting the legend — rejected mid-edit in favor of using "access point" consistently everywhere it
already appeared elsewhere in the panel, since two terms for one concept adds confusion rather than
removing jargon.

## 2026-08-09 — Per-Area entities are created only for Areas the user has actually configured
**Decision:** New `topology_store.active_area_ids(topology)` returns the set of Area ids with either
a non-empty `area_entity_selections` entry or an `EgressPoint` — i.e., an Area the user has bound at
least one piece of activity evidence or access-point entity to. `sensor.py`/`binary_sensor.py` now
only construct `AreaOccupantCountSensor`/`AreaOccupiedBinarySensor` for Areas in that set. A new
`__init__.py` helper, `_prune_inactive_area_entities()`, runs at every setup/reload and actively
removes (`entity_registry.async_remove`, not just skips re-creating) any previously-registered
per-Area entity whose Area has since dropped out of the active set — e.g., the user deselected its
last piece of evidence. House-level entities (`sensor.total_occupant_count`,
`binary_sensor.pre_armed`) are unaffected — they always exist. The occupancy **engine**'s own graph
(`engine_adapter.build_house_graph()`) still includes every Area regardless of activity, unchanged —
this decision only ever affects which HA entities get created, never what the engine reasons over
(SPEC.md §5.1 permits a sensor-less Area as a valid pass-through node for transit inference). The
topology panel gained a matching `_isAreaActive()` check so an inactive Area is visibly dimmed in the
graph and its detail panel shows an explanatory notice instead of an empty checklist result.
**Why:** Project owner feedback after using the panel on a real house: since Areas can't be "opted
out" of the topology at all (every HA Area is a node), a house with many rooms produced a
same-sized wall of `sensor.*_occupant_count`/`binary_sensor.*_occupied` entities regardless of whether
the user had configured anything for most of them — "not efficient and unnecessary... this is good
house keeping and avoids unnecessary clutter." Entity creation/removal naturally piggybacks on the
pre-existing reload-on-topology-change mechanism (`TopologyStore.async_replace_topology()`'s
`engine_relevant_change` check already triggers `hass.config_entries.async_reload()` whenever
`area_entity_selections`/`egress_points` change), so no new live-patching machinery was needed.
**Alternatives considered:** Leaving the entities registered but marking them "unavailable" —
rejected per the project owner's explicit ask for the entities to be *removed*, not just hidden, once
fully deselected; a registered-but-permanently-unavailable entity is exactly the clutter being
complained about, just relabeled.

## 2026-08-09 — Options-flow/service translations rewritten for non-technical users
**Decision:** `translations/en.json`'s config/options-flow/services text was fully rewritten to avoid
any assumption of technical or algorithmic background — e.g. `transit_confirmation_window` is now
described in plain cause-and-effect terms ("this is how long it gives the next room to prove that
guess right before giving up... turn this up if people in your home often take a while getting
between rooms") rather than referencing internal mechanics.
**Why:** Project owner read the live options form after the Phase 8 tunables batch shipped and gave
explicit, direct feedback: "the language and explanation needs to assume the user doesn't have an
understanding of the code or technical methods used by the algorithm... needs to dumb it down to
simple concepts that they can understand." This is a durable standard, not a one-off — the same bar
was applied to the topology panel's own copy in the UI/UX pass above.
**Alternatives considered:** None — this was a direct, unambiguous correction of existing text rather
than a design choice with real alternatives.

## 2026-08-09 — Device registration + `homeassistant://` link restores a navigation path to the panel
**Decision:** `__init__.py`'s `async_setup_entry` now registers one virtual `DeviceEntryType.SERVICE`
device per config entry (`device_registry.async_get_or_create`), named "Occupancy Tracker," with
`configuration_url=f"homeassistant://{DOMAIN}"`. The two house-level entities
(`sensor.total_occupant_count`, `binary_sensor.pre_armed`) are now attached to this device via
`DeviceInfo(identifiers={(DOMAIN, entry_id)})`.
**Why:** Removing `config_panel_domain` (see the entry above) fixed the options flow but reopened a
real gap the project owner then hit directly: "how do I navigate from the Occupancy Tracker screen to
the Areas and connections panel, if it isn't in the sidebar? I would have thought there'd be a
navigation path via the configuration interface?" With `config_panel_domain` gone, Settings → Devices
& Services → Occupancy Tracker's gear icon now only opens the options form — there's no longer any
link from that screen back to the topology panel except the (easily-missed, and not guaranteed
present for every user's sidebar configuration) permanent sidebar entry. A Device's own page has a
"Visit" link driven by `configuration_url`, and HA supports a `homeassistant://` scheme specifically
for linking to an internal frontend route without making a real network request — verified from
`device_registry.py`'s `CONFIGURATION_URL_SCHEMES = {"http", "https", "homeassistant"}` and real core
usage (`homeassistant://config/backup` in the core `backup` component). This gives a second,
always-present path back to the panel: Settings → Devices & Services → Occupancy Tracker → the device
page → Visit.
**Alternatives considered:** Re-adding `config_panel_domain` — rejected, since it makes the gear icon
and the device-page link mutually exclusive with the options flow being reachable at all (that's the
exact bug that was just fixed). A second, separate config entry purely to host a device — rejected as
needless complexity when the existing entry can own a device directly.

## 2026-08-09 — `manifest.json`'s `integration_type` changed from `helper` to `hub`
**Decision:** `manifest.json`'s `integration_type` is now `"hub"`, not `"helper"` (set at Phase 0 and
never revisited since).
**Why:** Project owner reported being unable to find "Occupancy Tracker" at all under Settings →
Devices & Services after successfully using it (topology panel and services both fully working) —
not a "where's the gear icon" question but "I can't see it as an integration." Traced to the actual
frontend source rather than guessed: `ha-config-integrations.ts` (the main "Integrations" tab)
subscribes to config entries via `subscribeConfigEntries(hass, cb, { type: ["device", "hub",
"service", "hardware"] })`; `ha-config-helpers.ts` (a separate "Helpers" tab) subscribes with `{
type: ["helper"] }` — mutually exclusive sets, verified at the exact installed frontend version
(`20260729.6`). An entry whose integration declares `integration_type: "helper"` **only** appears
under Helpers, never under Integrations — so this wasn't a bug in the sense of broken code, but a
year-old categorization choice that silently made the entire integration undiscoverable to anyone
looking where `SPEC.md`/`README.md`/`STATUS.md` themselves all say to look ("Settings → Devices &
Services → Add Integration"). Real HA "helper" integrations (`input_boolean`, `threshold`,
`utility_meter`, `derivative`) are narrowly-scoped, produce one or a small tightly-related set of
derived entities, and are typically created via the dedicated "+ Helper" flow rather than "Add
Integration" — Occupancy Tracker (many entities across many rooms, a dedicated full-page topology
panel as its primary configuration surface, its own services) doesn't fit that shape. `"hub"` is also
`loader.py`'s own documented fallback (`manifest.get("integration_type", "hub")`) for an integration
that doesn't specify one at all, reinforcing it as the safe, conventional default here.
**Alternatives considered:** `"service"` (also shows on the main Integrations tab, so functionally
equivalent for this bug) — no clear tie-breaker either way; `"hub"` was picked as the more
conservative choice since it's HA's own default. Omitting `integration_type` entirely (also defaults
to `"hub"` per the same fallback) — rejected in favor of setting it explicitly, so
`test_manifest.py`'s existing `test_required_fields_present` (which asserts the key is present at
all) keeps enforcing a deliberate choice rather than silent omission.

## 2026-08-09 — Removed `config_panel_domain`: it made the options flow completely unreachable
**Decision:** `panel.py`'s `async_register_panel()` call no longer passes `config_panel_domain=DOMAIN`.
The topology panel remains reachable via its permanent sidebar entry (Phase 7b-ii); Settings →
Devices & Services → Occupancy Tracker's gear icon now opens the options flow again, as it would for
any integration that doesn't set this parameter.
**Why:** Found during a Phase 8 requirements/UX audit (project owner's explicit request to check the
whole product against `SPEC.md` and scrutinize the UI from an average user's perspective) that this
session was adding options-flow tunables (household size hint, transit/confirmation windows) to a
form that turned out to be **entirely unreachable from the Home Assistant UI**. Traced this to
`config_panel_domain`, added in Phase 7b-i specifically so the "Configure" gear would open the
topology panel instead of a form. Verified from the actual frontend source at the exact installed
version (`home-assistant-frontend==20260729.6`, matched by git tag) rather than assumed:
`ha-config-entry-row.ts` renders the gear as *either* a link to the config panel *or* the
options-flow button — `configPanel && !stateText ? <link-to-panel> : item.supports_options ?
<options-button> : nothing` — and the row's overflow ("...") menu has no separate "Options" entry as
a fallback (`show-dialog-options-flow.ts` → `show-dialog-data-entry-flow.ts` confirms the options
dialog is only ever opened via that one gear-icon code path, `fireEvent(element, "show-dialog", ...)`
with no other trigger). This means the zone-fusion options flow (`tracked_persons`/
`near_house_zones`, added Phase 6) has been silently unreachable through the normal UI since Phase
7b-i shipped, and nothing since then happened to re-test that specific path — every subsequent
session's browser verification focused on the topology panel itself.
**Alternatives considered:** Reverse-engineer the frontend's internal `"show-dialog"` custom-event
contract so the topology panel could open the options dialog itself with a button of its own —
rejected: that event's payload (`dialogImport`, a dynamic-import function reference) is an
undocumented internal implementation detail of the frontend's own settings pages, not a stable
custom-panel API the way `hass.connection`/`hass.callWS` are (verified project convention, e.g. the
live-subscription work earlier this session, is to build on documented contracts only) — building on
it would risk exactly the kind of invented/unverified-API failure mode `CLAUDE.md` calls out as this
project's worst historical failure mode, just one layer removed. Moving the tunables into the
topology panel's own UI (a new websocket-backed settings section) was also considered — SPEC.md §7.3
explicitly allows exposing tunables "here or in the options flow," but simply not suppressing the
already-working, already-tested options flow is far less work for the same result, and is also more
consistent with users' general HA experience (gear icon = settings form, sidebar = the big
graphical tool) than the inverted setup `config_panel_domain` produced.

## 2026-08-09 — Services (manual override, topology export/import) fill a real SPEC.md §8 gap
**Decision:** `occupancy_tracker.set_occupant_count` is an **entity** service (registered via
`entity_platform.async_get_current_platform().async_register_entity_service()` from `sensor.py`,
targeting `AreaOccupantCountSensor` specifically) rather than a plain service taking an `area_id`
field — the user picks the room via HA's own entity/target picker, the same idiom core's own
`utility_meter.calibrate` uses (verified from its actual `sensor.py`/`services.yaml`). It calls a new
`OccupancyEngine.override_occupant_count(area_id, count, now)`, which bypasses the latch/transit
machinery entirely, clears any pending transit touching the Area (a manual correction is more
authoritative than an unresolved automatic guess about the same Area), and tags the result
`ProvenanceTier.USER_CONFIRMED`. `export_topology`/`import_topology` are plain (non-entity) services
— `export_topology` is `supports_response=SupportsResponse.ONLY` (returns the topology as response
data, verified this is a real, current `hass.services.async_register` capability, not assumed);
`import_topology` takes a `selector.ObjectSelector()` field and reuses the exact validate/save/
reload logic the websocket save command already had, via a new shared
`topology_store.async_replace_topology()` (the old `websocket_api._topology_validation_errors` was
promoted to a public `topology_store.validate_topology()` in the same move, so neither caller
re-derives it — `docs/ARCHITECTURE.md`'s anti-duplication rule).
**Why:** `SPEC.md` §8 explicitly requires both ("manual occupant-count override, topology
export/import... registered via `hass.services.async_register` with a `services.yaml`") and neither
existed at all before this session's requirements audit — a plain, unambiguous gap, not a judgment
call.
**A side effect worth noting**: moving the websocket save command's logic into a shared
`async_replace_topology()` changed its exact timing slightly — the websocket `send_result` ack now
arrives *after* any triggered reload completes, not before. This actually fixes a latent, previously
unnoticed race: the topology panel's `_resubscribeEngineState()` (this session's earlier live-refresh
work) fires right after receiving that ack, and could previously have re-subscribed to the
about-to-be-replaced engine instance if the reload hadn't finished yet, silently going stale with
nothing left to re-trigger a second resubscribe. Not treated as a bug fix in its own right (never
observed/reported), just recorded here since it's a real behavior change from the refactor.
**Alternatives considered:** A plain `area_id` field for `set_occupant_count` (rejected — an entity
target is the more idiomatic, more discoverable HA pattern and avoids inventing a lookup users would
need `entry_id`/`area_id` slugs for, which they generally don't know off-hand).

## 2026-08-09 — Household-size hint surfaces as an attribute, never touches count inference
**Decision:** `EngineConfig.household_size_hint: int | None` is *not* read anywhere in
`OccupancyEngine`'s count-inference branches. It's surfaced purely as a new
`exceeds_household_size_hint` boolean attribute on `sensor.total_occupant_count` (true when the
current total exceeds the hint), computed in `sensor.py` from a new `OccupancyEngine.
household_size_hint` read-only property.
**Why:** `SPEC.md` §6.4 is unusually explicit and emphatic that this hint "must never reject or cap a
count the evidence actually supports" — it's phrased almost as a warning against a plausible-sounding
mistake. Wiring it into the engine's internal state machine at all — even just to affect a `quality`
tier, not the count — was judged too risky: today's `StateQuality` enum (confirmed/latched/ambiguous)
has a specific, narrow, already-tested meaning (freshness / pending-transit status), and overloading
it with a second, unrelated "statistically unlikely" signal would blur that meaning and create a
plausible path for some future change to accidentally start treating "over the hint" as "less real,"
i.e. a de facto cap through the back door. A separate, clearly-named, purely observational attribute
has no such path.
**Alternatives considered:** A new `StateQuality` value or a numeric per-Area confidence score
(rejected — larger engine surface change than SPEC.md's own "may be used" language justifies, and
`SPEC.md` §6.4's example — "an unexplained 6th simultaneous occupant... lower confidence than a
2nd" — is about the *house total*, which the chosen house-level attribute already covers directly).

## 2026-08-09 — Entity friendly names via `entity_registry.async_get_full_entity_name`
**Decision:** `registry_sync.py`'s `EntitySnapshot` gained a `name: str` field, computed as
`entity_registry.async_get_full_entity_name(hass, entry) or entry.entity_id` — the same public,
documented HA helper the frontend itself uses to compute an entity's display name (device + entity
name composition, user overrides, `has_entity_name` handling all included for free), not a bespoke
re-derivation of that logic.
**Why:** Found during the Phase 8 UX audit as the single biggest "looks like a dev tool, not a Home
Assistant feature" issue: the topology panel's entity checklists (access points, per-area selection)
showed raw entity ids (`binary_sensor.bedroom_1_motion`) with no way to tell what a checkbox actually
represented without cross-referencing Settings → Entities separately. `SPEC.md` §5.2's own example
phrasing ("this motion sensor," "this TV," "this door contact") implies human-readable labels were
always the intent.
**Alternatives considered:** Re-deriving a display name from `entry.name`/`entry.original_name`
directly in `registry_sync.py` (rejected — `async_get_full_entity_name` already handles device-name
composition, `has_entity_name`, and prefixing correctly and is what the real HA UI itself calls; a
bespoke version would inevitably drift from it in some edge case).

## 2026-08-09 — `ha-checkbox` swap investigated, deliberately not made
**Decision:** The topology panel's checklists still use plain `<input type="checkbox">` with custom
CSS, not HA's native `ha-checkbox`/`ha-formfield`.
**Why:** Raised in the Phase 8 UX audit as a "borrow HA's own components" (`UX_GUIDELINES.md` §1)
finding. Investigated properly this time (a previous session's attempt gave up after an unreliable
bundle-grep check) by fetching `ha-checkbox.ts` from the `home-assistant/frontend` repo at the exact
tag matching this environment's installed `home-assistant-frontend` version (`20260729.6`) — found it
now wraps `WaCheckbox`, a "Web Awesome" web component from a separate npm package
(`@home-assistant/webawesome`), not the Material checkbox expected. Could not verify that
component's event/property contract (does toggling fire a plain `change` event with `target.checked`,
or something Shoelace/Web-Awesome-idiomatic like a `wa-change` custom event?) — that package's source
wasn't reachable from here. Swapping without that verification risks silently breaking the working,
tested, user-approved checklist interactions for a cosmetic-only gain, which is exactly the class of
mistake `CLAUDE.md`'s hard rule 1 exists to prevent.
**Alternatives considered:** None attempted — this is a "verify before proceeding" deferral, not a
considered rejection. Revisit if a future session can reach the `@home-assistant/webawesome` source,
or if HA's own devtools/docs publish the component's event contract directly.

## 2026-08-09 — Per-area entity selection reuses the access-point checklist pattern verbatim
**Decision:** The detail panel's "Selected entities" display (previously read-only) became an
editable checklist over an Area's `entity_ids`, structurally identical to Phase 7b-iii's
access-point checklist: `_setAreaEntitySelections`/`_toggleAreaEntitySelection` mirror
`_setEgressEntities`/`_toggleEgressEntity` almost line-for-line, saving through the same existing
`occupancy_tracker/topology/save` command. No backend or schema changes — `area_entity_selections`
already existed and was already consumed by `signal_ingestion.py` since Phase 4.
**Why:** This was flagged as an "Open follow-up" gap in the previous two sessions' `STATUS.md`:
`SPEC.md` §5.2 requires a UI to pick which entities count as occupancy evidence per room, and
without it a real house has zero signal anywhere — Connectors/access points/the explainability
inspector all had editable UIs, but this one didn't, making the whole panel not actually usable
end-to-end despite every other piece being done. The access-point checklist was the obvious template
to reuse rather than design a new interaction pattern, since it already solved the identical UI
problem (a per-Area checklist over that Area's entities, saved on every toggle).
**Deliberately not made mutually exclusive with the access-point checklist:** an entity can appear
checked in both lists at once (e.g. a door sensor could reasonably be both an access-point crossing
sensor and general activity evidence for its own room). `websocket_api.py`'s save validation checks
the two lists independently with no cross-list constraint, so inventing an exclusivity rule in the
frontend that the backend doesn't enforce would just be UI complexity with no real backing.
**Alternatives considered:** A single unified checklist with a third state or separate "evidence
kind" per entity (rejected — over-engineered for what SPEC.md actually asks for, and the two
concerns already have independent, working UI real estate in the same detail panel).

## 2026-08-09 — Explainability inspector uses a push subscription, not poll-on-select
**Decision:** The detail panel's live engine state (`occupancy_tracker/engine/get_state`) is backed
by a second websocket command, `occupancy_tracker/engine/subscribe_updates`, that pushes a fresh
snapshot on every `OccupancyEngine` signal via the existing `add_listener()` hook (the same one
`sensor.py`/`binary_sensor.py` use). The panel subscribes exactly once per panel lifetime — not once
per Area selection — via `hass.connection.subscribeMessage()`, and keeps whichever Area is currently
selected in sync automatically as pushes arrive.
**Why:** Project-owner live-testing feedback: toggling a device while a room's detail panel was open
didn't update the panel's chips until it was deselected and reselected, because the original
implementation only fetched engine state at selection time. A poll-on-an-interval fix was
considered and rejected outright — `CLAUDE.md` hard rule 4 bans polling loops standing in for real
subscriptions, and this integration already has a working example of the correct pattern
(`OccupancyEngine.add_listener()` → entity push-updates) to extend rather than a poll to bolt on.
Implemented the websocket-subscription side by verifying the exact contract against real source
rather than recalling it from memory, since this is the first websocket-push (as opposed to
websocket-request/response) command in this codebase: `connection.subscriptions[msg_id]` +
`connection.send_event(msg_id, payload)` from HA core's `websocket_api/connection.py`, and
`hass.connection.subscribeMessage(callback, msg, options)` from `home-assistant-js-websocket`'s
`connection.ts` (callback fires only on subsequent push events, not the initial `result` — a
one-shot `get_state` call is still needed for the first paint).
**Cleanup correctness:** registered via *both* `connection.subscriptions[msg["id"]]` (fires on
browser disconnect/explicit unsubscribe) and `entry.async_on_unload()` (fires on config-entry
reload), guarded by a `nonlocal removed` flag so whichever fires first is a no-op for the other —
a config-entry reload replaces the engine instance the listener was registered against, and the
websocket connection itself outlives any single reload, so relying on only one of the two cleanup
paths would leak a listener on a stale engine after every reload. The frontend also resubscribes
unconditionally after every topology save, since a structural save triggers exactly that kind of
reload.
**Alternatives considered:** Polling `get_state` on an interval while a panel is open (rejected —
banned pattern, see above). Re-fetching only the selected Area's state on a coarser timer (rejected
for the same reason, and it wouldn't fix the underlying "stale until reselect" bug being reported,
just shrink the staleness window).

## 2026-08-09 — "Egress point" renamed to "Access point" in UI copy only
**Decision:** User-facing text in the topology panel (detail-panel labels, legend, hints, empty-state
copy) now says "access point" instead of "egress point". Internal naming — the `EgressPoint`
dataclass, `egress_points`/`entity_ids` storage fields, the websocket schema, `occupancy_engine.py`'s
"egress confirmation" logic, and `SPEC.md`/`ARCHITECTURE.md` — is unchanged and still says "egress".
**Why:** Project-owner live-testing feedback: "egress" strictly means *leaving*, but this concept
covers both arrival and departure (the engine's own asymmetric-confirmation logic treats them
differently precisely because both directions matter) — "access point" matches what it actually
does. A full rename was explicitly offered and declined for this session: it touches `SPEC.md` (the
product source of truth), a persisted storage field name (would need a schema migration on top of
the one this session already shipped for `outside_position`), and every test referencing
`egress_points`/`EgressPoint` — real effort with real risk, not something to fold into a copy fix
without a deliberate choice to do so.
**Alternatives considered:** Full rename (offered, declined — see above, revisit later if wanted).
Leaving "egress" in the UI too (rejected — the terminology genuinely is misleading to an end user,
per the direct feedback that triggered this).

## 2026-08-09 — "Outside" is a real, draggable, persisted node (`outside_position`, schema 1.2→1.3)
**Decision:** `TopologyData` gained `outside_position: tuple[float, float] | None`, alongside
`area_positions` (storage schema 1.2→1.3, with a migration defaulting it to `None`). The panel now
tracks the synthesized "Outside" node's position the same way it tracks any Area's — draggable,
grid-snappable, and included in `_computeViewBox`/"Fit view" — rather than recomputing a fresh
position for it on every render. It's added to the tracked positions the moment the first access
point is created and removed the moment the last one is removed, so a stale position never lingers
to skew fit-view math for a node that isn't even being shown.
**Why:** Project-owner live-testing feedback: Outside couldn't be repositioned at all, and "Fit
view" clipped it since the view-fitting math never knew it existed — both were symptoms of the same
root cause (Outside's position was purely derived at render time, never stored anywhere). A separate
top-level field was needed rather than reusing `area_positions`, because that field's key must be a
real Area (the websocket API validates every `area_positions` key against the live house shape) and
Outside isn't one.
**Alternatives considered:** Storing Outside's position keyed inside `area_positions` under a
sentinel id — rejected, since it would require special-casing that one key out of the "must be a
real Area" validation rather than just giving it its own field with no such constraint to begin
with.

## 2026-08-09 — Topology panel gets its own sidebar item, not just a Devices & Services entry point
**Decision:** `panel.py`'s `async_register_panel()` call now passes `sidebar_title`/`sidebar_icon`,
giving the panel a permanent, always-visible sidebar link, in addition to the existing
`config_panel_domain`-based Devices & Services → Configure entry point.
**Why:** Found via live browser testing — the project owner could not locate the integration under
Settings → Devices & Services → Integrations at all (this integration's `manifest.json` declares
`integration_type: "helper"`, which HA's own developer docs describe as the same category as
`derivative`/`input_boolean`/`group` — those normally surface as named cards under the *Helpers*
tab, not *Integrations*, but that didn't resolve the discoverability complaint either). Rather than
keep chasing exactly which sub-tab a `helper`-type config entry is supposed to render under, or
reconsidering `integration_type` itself (out of scope for this slice — it was a deliberate,
verified Phase 0 choice), a sidebar item sidesteps the question entirely: the panel is HA's primary
user-facing surface for this integration per `SPEC.md` §7.3 ("independently re-openable at any
time"), so it deserves a first-class, unambiguous entry point regardless of how config-entry
categorization happens to render elsewhere.
**Alternatives considered:** Changing `integration_type` away from `"helper"` — rejected without
re-verifying against source why `"helper"` was chosen originally (see this file's 2026-08-08 Phase 0
entry); not something to change as a side effect of a discoverability bug fix. Digging further into
`home-assistant-frontend`'s tab-filtering logic to fix the *root* categorization question — deferred
as unnecessary once the sidebar item made the panel reliably reachable either way.

## 2026-08-09 — Connector drawing: click-two-nodes-in-sequence, not drag-node-to-node
**Decision:** Phase 7b-ii's connector-drawing interaction is a toolbar "Draw connector" mode: click
one Area node, then another, to create a Connector between them (with a live dashed preview line
following the pointer in between the two clicks); clicking the same node again, or pressing Esc,
cancels. Removing a connector is hover-or-tap-to-reveal a small delete control at the edge's
midpoint, plus a keyboard path (Tab to the edge, Enter/Delete/Backspace).
**Why:** `docs/STATUS.md` had flagged this as an open design question worth resolving deliberately
rather than guessing (drag vs. click-sequence). Node-dragging in this panel already means
"reposition," so overloading drag with a second, conflicting meaning ("connect") would be ambiguous
UX; a click sequence keeps the two gestures unambiguous, works identically on touch (no drag target
precision needed), and is straightforwardly keyboard-accessible (`docs/UX_GUIDELINES.md` §6's
accessibility-fallback requirement) in a way a drag-based connector tool is not. The live preview
line during the click sequence keeps the "drawing a connection" feel `SPEC.md` §7.3 describes
without requiring the commit gesture itself to be a drag.
**Alternatives considered:** Drag-from-node-to-node (rejected: conflicts with existing
reposition-drag, harder on touch, harder to make keyboard-accessible). A separate small connector
"handle" on each node's edge that only *that* handle can be dragged from (rejected as unnecessary
added complexity for a hand-rolled SVG canvas, given the click-sequence approach already satisfies
the UX requirements without it).

## 2026-08-09 — Connector line color and grid-dot alignment, fixed after live testing
**Decision:** Connector/egress edges are now stroked with `rgba(var(--rgb-primary-color), 0.5)` (a
translucent tint of the same color used for Area node outlines) instead of
`var(--divider-color)`. The grid-dot pattern's dot moved from local `(cx=1, cy=1)` to `(cx=0, cy=0)`
within its tile, and `_autoLayout()` now snaps its computed positions to `GRID_SIZE` when the grid
toggle is on.
**Why:** Live browser feedback: the grey divider-color line read as an unrelated, generic UI line
rather than something connecting the (blue-outlined) rooms it's between — a lighter tint of the same
primary color reads as "belongs to the same object system" instead. Separately, drawn connectors
looked visibly offset from the background dot grid even with grid-snap on; root cause was two
compounding bugs, not one: (1) the grid dot was drawn 1 unit off the tile's corner, while a
grid-snapped node sits exactly on multiples of `GRID_SIZE` — moving the dot to the tile origin makes
snapped coordinates and visible dots coincide exactly; (2) `AUTO_LAYOUT_CELL` (140) is not a multiple
of `GRID_SIZE` (40), so an auto-arranged room was never actually grid-aligned in the first place
regardless of the dot-position bug — fixed by snapping auto-arrange's own output when the grid
toggle is on, so auto-arranged and manually-dragged nodes always agree with each other and with the
dot grid, not just internally consistent with themselves. `--rgb-primary-color` was verified as a
real HA theme token by grepping the installed `hass_frontend` bundle for it (found in multiple
bundle chunks) rather than assumed from memory, per this project's HA-API verification rule (it
technically isn't a Python-side HA API, but the same discipline applies to frontend theme contracts).

## 2026-08-09 — SVG pan/zoom requires the viewBox and its container to share one fixed aspect ratio
**Decision:** The topology panel's graph viewport (`.graph-wrap`) is pinned to a fixed CSS
`aspect-ratio: 640 / 460`, and every computed SVG `viewBox` (`_computeViewBox()`, and the
wheel-zoom handler) is kept at that exact same ratio (`VIEWPORT_ASPECT = 640 / 460`) at all times.
**Why:** Found via live browser testing (project owner: "the zoom feels off... almost like it is
panning as well as zooming"). Root cause: the container previously had a *fixed pixel height*
(`460px`) with a *responsive width* (`100%` up to `640px`), while the viewBox was fit tightly to
whatever Areas existed — the two almost never matched aspect ratio. SVG's default
`preserveAspectRatio="xMidYMid meet"` responds to a mismatch by letterboxing (padding one axis to
preserve the viewBox's own proportions) rather than stretching — which silently broke the
screen-pixel-to-canvas-unit conversion (`viewBox.w / rect.width`) that the pan, zoom-to-cursor, and
node-drag handlers all depend on, since that conversion is only valid when the rendered content
truly fills `rect` on both axes. Locking both sides of the equation to the same ratio makes
`viewBox.w / rect.width` exactly equal `viewBox.h / rect.height` always, eliminating the class of
bug entirely rather than special-casing the letterbox math.
**Alternatives considered:** Computing the actual `preserveAspectRatio` letterbox offset/scale and
correcting for it in the coordinate math — technically possible but meaningfully more error-prone
code for a problem a fixed aspect ratio avoids outright. Not verified in an automated test (no
browser automation available this session) — this was caught and fixed through the project owner
manually testing in a real browser, which is exactly the verification step `docs/STATUS.md` had
flagged as still outstanding.

## 2026-08-09 — Node layout: persisted per-area positions, draggable with grid-snap, plus auto-arrange
**Decision:** `TopologyData` gained `area_positions: Mapping[str, tuple[float, float]]` (topology
store schema 1.1 → 1.2, with a migration defaulting it to `{}` for older saved data). It's a pure
display concern the engine/signal-ingestion/entity platforms never read. The panel lets a user drag
any Area node (optionally snapping to a subtle, always-zoom-scaled dot grid), auto-saves the new
position on drag-end, and offers a one-click "Auto-arrange" that computes a deterministic,
overlap-free, floor-aware grid layout (Areas grouped by `floor_id`, bands ordered by the floor's own
`level` — a new field added to `FloorSnapshot`/`_house_shape_json`, verified against
`helpers/floor_registry.py`'s real `FloorEntry.level` — with unfloored Areas last) and saves it.
**Why:** Directly requested by the project owner: both a "sort logically, no overlaps" auto-arrange
*and* manual dragging with a subtle snap grid that scales with zoom. A force-directed/physics layout
was considered and rejected as unnecessary complexity — a fixed-spacing per-floor grid is
deterministic, trivially overlap-free by construction (verified with a standalone Node script
checking pairwise node distances across several synthetic house shapes before shipping it), and
"logical" in the specific sense the project owner asked for (grouped and ordered by floor). The grid
"auto-scales with zoom" for free: it's drawn as SVG content in the same coordinate space as
everything else, so it necessarily gets denser/sparser on screen exactly as the viewBox zooms,
without any extra code.
**Alternatives considered:** Making auto-arrange a non-destructive *preview* that doesn't overwrite
saved positions until confirmed — rejected as unnecessary extra UI state for a v1; auto-arrange
already only runs on explicit user action, and manual dragging afterward can always undo any
individual placement. Per-node "auto vs. manual" mode flags — rejected in favor of the simpler
"has a saved position, or doesn't" model `area_positions` already gives for free (an Area with no
entry is auto-laid-out; dragging it just adds one).

## 2026-08-09 — Topology save skips the entry reload when only positions changed
**Decision:** `occupancy_tracker/topology/save` compares the incoming `connectors`/`egress_points`/
`area_entity_selections` against what's currently stored before deciding whether to call
`hass.config_entries.async_reload()` — a change to `area_positions` alone no longer triggers one.
**Why:** The topology panel now saves on every node drag-end, to keep dragging feeling responsive
with no explicit "Save" button (`docs/UX_GUIDELINES.md` §2, optimistic UI). The original "topology
save always reloads" decision (see that entry above) was written before drag-to-reposition existed,
when every save was an infrequent, deliberate structural edit. Reloading the whole config entry on
every drag would mean the engine/signal-ingestion/entity platforms tear down and rebuild repeatedly
for a field none of them ever read — pure churn, plus a real UX cost (a brief entities-unavailable
window) for zero benefit.
**Alternatives considered:** Debouncing the save call instead (wait until dragging settles, save
once) — doesn't address the root issue, since even one reload per drag gesture is still unnecessary
when nothing engine-relevant changed; the compare-before-reload approach fixes it for auto-arrange's
many-positions-at-once save too, not just single-node drags.

## 2026-08-08 — Vendor a self-contained Lit build instead of loading it from a CDN
**Decision:** `www/vendor/lit-core.min.js` is a locally-built, fully self-contained ES module
bundle (`LitElement`, `html`, `css`, `svg`, `nothing` — built via `esbuild --bundle --format=esm`
from a two-line entry file re-exporting those names from the `lit` npm package, then verified to
contain zero remaining `import`/`from` statements), committed into the repo and served by our own
static path. The panel imports it via a relative path, never a CDN URL.
**Why:** Put to the project owner as an explicit choice rather than assumed: many real Home
Assistant installs are LAN-only or otherwise have no outbound internet access from the browser
tab viewing them, and HA's own official custom-panel documentation example (which loads Lit from
`unpkg.com`) would silently break the panel on exactly those installs with no useful error, only a
blank page. A naive attempt at "bundling" via a CDN's own bundling query parameter
(`esm.sh/lit@3?bundle`) still turned out to re-import a second chunk from the same CDN at runtime —
not actually self-contained — so this needed a real local build, not just picking a different CDN
URL.
**Alternatives considered:** Load Lit from a CDN (`unpkg`/`jsdelivr`), matching HA's own docs
example exactly — rejected for the offline/LAN-only breakage above. A plain-`HTMLElement`,
no-framework implementation with hand-rolled DOM diffing — rejected as meaningfully more code and a
manual re-render-correctness burden for no real benefit once a vendored bundle solves the
offline concern. The vendored file needs to be manually rebuilt/re-committed on future Lit version
bumps (no automatic update path) — accepted as the cost of the offline guarantee.

## 2026-08-08 — Panel resolves its config entry via `config_entries/get`, not a baked-in id
**Decision:** `www/topology-panel.js` calls the core `config_entries/get` websocket command
(filtered to `domain: "occupancy_tracker"`) at load time to find this integration's entry id,
rather than `panel.py` passing `entry_id` into the panel's static `config` at
`panel_custom.async_register_panel()` registration time.
**Why:** Panel registration is guarded to happen at most once per HA runtime (see the next entry) —
if the entry id were baked in at that first registration and the user later removed and re-added
the integration within the same running HA instance, the panel would keep calling
`occupancy_tracker/topology/get` with a now-stale, nonexistent entry id, and every load would
silently fail. Resolving it live avoids that entirely, at the cost of one extra websocket
round-trip per panel load — negligible.
**Alternatives considered:** Bake the entry id in at registration time — simpler, but has the
stale-id failure mode above for a `single_config_entry: true` integration whose one entry can still
be removed and re-added. Re-registering the panel (with a fresh config) on every entry setup instead
of guarding against it — rejected, see the next entry for why that specifically can't work.

## 2026-08-08 — Panel/static-path registration is guarded to run at most once per HA runtime
**Decision:** `panel.py`'s `async_setup()` checks a `hass.data` sentinel and returns immediately if
already set, before calling `hass.http.async_register_static_paths()` or
`panel_custom.async_register_panel()`. It's still called unconditionally from every
`async_setup_entry()` run (including every reload).
**Why:** `async_setup_entry` re-runs on every config-entry reload, and Phase 7a's
`occupancy_tracker/topology/save` deliberately triggers a reload on every successful save (see that
phase's own reload decision). `panel_custom.async_register_panel()` calls
`frontend.async_register_built_in_panel()` without `update=True` — a parameter `panel_custom`'s
wrapper never exposes — and that function raises `ValueError` if the same `frontend_url_path` is
already registered (verified directly from `frontend/__init__.py`). Without this guard, the very
first topology save after setup would crash the reload it triggers. This mirrors the precedent set
by `websocket_api.py`'s own registration (also hass-global, also re-invoked on every setup), except
panel registration actually needs the explicit guard where websocket command registration didn't
(overwriting a dict entry is naturally idempotent; `panel_custom.async_register_panel()` is not).
**Alternatives considered:** Call `frontend.async_remove_panel()` in `entry.async_on_unload` and
re-register fresh on every setup — works, but adds teardown/re-creation churn (and a brief window
where the panel URL 404s) for a benefit — running config always reflecting "this setup's" state —
that doesn't matter here, since the panel's own content is entirely dynamic per-load (see the
previous entry) and never depends on anything fixed at registration time. Not implementing
`async_remove_panel` at all (not even on integration removal) was accepted as an explicit, scoped
gap, matching Phase 7a's websocket-command registration having the same property already.

## 2026-08-08 — Topology save reloads the entry rather than live-patching the engine
**Decision:** `occupancy_tracker/topology/save` validates and persists the new topology, sends the
websocket result, and then calls `hass.config_entries.async_reload(entry.entry_id)`.
**Why:** The engine's `HouseGraph` and signal ingestion's subscriptions are built once at
`async_setup_entry` time from that moment's topology (Phase 4's documented scope limit), and
`SPEC.md` §7.3 explicitly allows topology edits to take effect "immediately (or on next reload)."
Reload is the mechanism that already exists for the second half of that allowance — Phase 6's
options flow triggers one automatically on every options change — so reusing it for a topology save
delivers a working "changes take effect" story today without duplicating Phase 4's setup wiring into
some new live-patch path that would need its own correctness argument (partial teardown/rebuild of
just the graph and signal subscriptions, while leaving registry sync/zone fusion/entities alone, is
meaningfully more code than an entry reload and has more ways to leave the engine in an inconsistent
state).
**Alternatives considered:** A `rebuild()` method on the engine/signal-ingestion pair that patches
the running instances in place without a full reload — would satisfy the "immediately" half of
SPEC's allowance more precisely (no brief entities-unavailable window during reload), but is
meaningfully more code and a new correctness surface for a benefit `SPEC.md` doesn't require today.
Revisit if a reload's brief entity-unavailable window turns out to matter in practice once the
frontend (Phase 7b) is actually driving this in a browser.

## 2026-08-08 — Websocket API imports HA symbols from their defining submodule, not the aggregator
**Decision:** `websocket_api.py` imports `ActiveConnection`, `ERR_NOT_FOUND`, `ERR_INVALID_FORMAT`,
`websocket_command`, `require_admin`, and `async_response` directly from
`homeassistant.components.websocket_api.{connection,const,decorators}`, rather than off the
aggregating `homeassistant.components.websocket_api` package the way core integrations' own code
does (e.g. `components/config/area_registry.py`).
**Why:** `homeassistant.components.websocket_api/__init__.py` re-exports these via
`from .connection import ActiveConnection  # noqa: F401`-style imports without declaring `__all__`.
Under this project's `mypy --strict` config (`no_implicit_reexport` is part of `strict`), importing
such a name off the aggregator and using it fails with "does not explicitly export attribute X."
This doesn't affect HA core's own mypy run (their config apparently tolerates it, or the internal
consumer files are treated differently), but it does affect ours checking against the installed
package as a third-party dependency. Importing from the actual defining submodule — verified by
reading `connection.py`/`const.py`/`decorators.py` directly, not guessed — sidesteps the re-export
check entirely and is exactly as correct, since those are the real, stable locations these symbols
are defined at.
**Alternatives considered:** Suppressing with `# type: ignore[attr-defined]` at each call site —
rejected as noisier than fixing the import path once, and it would hide a real "this symbol moved"
signal if a future HA version actually changed where these live.

## 2026-08-08 — Zone fusion kept out of the engine's counting logic entirely
**Decision:** `ZoneFusion` never calls into `OccupancyEngine` and never produces an engine `Signal`.
Its two outputs — `house_zone_corroboration()` and `is_pre_armed()` — are read-only, surfaced as an
attribute on `sensor.total_occupant_count` and a new `binary_sensor.pre_armed`, and don't change any
occupant count or quality tier the engine computes.
**Why:** `SPEC.md` §6.7 is explicit and repeated: "zone presence alone must never silently change
[the occupant count]," "doesn't by itself place someone in a specific Area," "should not increment
the house occupant count." Two of the richer behaviors §6.7's prose describes — zone corroboration
"helping resolve ambiguous transits," and `not_home` "decaying the confidence behind a stale
occupant token" — would require identifying *which* occupant token a specific tracked person
corresponds to. The engine's model is a per-Area integer count, not per-occupant identity (a
deliberate Phase 3 choice, see that phase's "stale pending transits" decision) — there is no token
for a zone signal to attach to. Implementing either behavior now would mean either quietly
introducing per-token identity through the back door (a much larger redesign than this phase's
scope) or building something that looks like it does what SPEC describes but doesn't actually
resolve anything (e.g. corroboration nudging a pending transit's confirmation with no principled
tie-break rule). Neither is better than clearly not implementing it yet.
**Alternatives considered:** Adding a numeric "confidence" field to `AreaState` that zone
corroboration nudges up/down — rejected as the same kind of ungrounded-numeric-threshold problem the
Phase 5 "dropped `Signal.confidence`" decision already argued against; there's no principled value
to nudge it by without a demonstrated need. Revisit both deferred behaviors together if/when a
concrete reason to add per-token identity to the engine emerges (this would be a significant enough
change to warrant its own dedicated design pass, not a rider on zone fusion).

## 2026-08-08 — Near-house zone matching uses `in_zones`, not the legacy zone-name state string
**Decision:** `classify_zone_membership()` determines "near-house zone" membership by checking the
tracked entity's `in_zones` attribute (a list of real zone entity ids) against the user's configured
near-house zone ids — it does not attempt to match the entity's plain `state.state` string against a
zone's slug or name for this purpose.
**Why:** Verified against real HA core source before assuming a match strategy (per `CLAUDE.md` rule
1): traced `components/device_tracker/legacy.py`'s `async_update` (`self._state = zone_state.name`
for a non-home zone) and confirmed via `components/person/__init__.py` that `person` entities copy
their source tracker's `state.state` directly. `zone_state.name` is the zone's *display* name — an
arbitrary string (could be "Front Yard", could be anything a user typed), not a stable slug
guaranteed to match the zone entity's `object_id`. Reconstructing an entity_id from that string
(e.g. `f"zone.{slugify(state.state)}"`) would be fragile and unverified guessing exactly like the
API-invention failure mode `CLAUDE.md` rule 1 exists to prevent. `in_zones`
(`DeviceTrackerEntityStateAttribute.IN_ZONES` / `PersonEntityStateAttribute.IN_ZONES`, both the
string `"in_zones"`) instead carries actual zone entity ids directly — an exact, reliable match
against the options flow's `EntitySelector(domain="zone")`-sourced configuration.
**Alternatives considered:** String-matching `state.state` against zone names/slugs anyway —
rejected as unreliably fragile per the above. Computing zone membership from GPS coordinates via
`zone.async_active_zone()` — would require every tracked entity to expose `latitude`/`longitude`
attributes, which connectivity-based (non-GPS) trackers don't have, so `in_zones` (which both GPS
and connectivity-based *modern* trackers populate) is the more broadly applicable choice. **Known
limitation, not fixed:** legacy trackers that report a zone name in `state.state` without an
`in_zones` attribute won't be detected as "near house" by this integration. Not addressed now because
`SPEC.md` §6.7's stated use case is specifically the companion app, which is `in_zones`-populating;
see `docs/STATUS.md`'s Phase 6 open-follow-up note.

## 2026-08-08 — Options flow: plain `OptionsFlow` subclass, not `SchemaConfigFlowHandler`
**Decision:** The new options flow (`OccupancyTrackerOptionsFlow(OptionsFlow)`) is a hand-written
class with an explicit `async_step_init`, added alongside the existing Phase 0 `ConfigFlow` via
`async_get_options_flow` — not a rewrite of `config_flow.py` into the declarative
`SchemaConfigFlowHandler` framework (`derivative`/`threshold` use this to combine config+options
flows into one class from shared voluptuous schemas).
**Why:** Phase 0 already deliberately chose the plain `ConfigFlow` style over
`SchemaConfigFlowHandler` for the confirmation-only initial flow (see that phase's decision entry).
Extending that same integration's options flow with `SchemaConfigFlowHandler` would mean restructuring
the already-working, already-tested `ConfigFlow` class to fit that framework's combined shape —
unnecessary churn to introduce a second flow-authoring style into one small integration, for a form
with exactly two fields. The plain `OptionsFlow` subclass needs no restructuring of existing code,
is simpler to read end-to-end without knowing the schema-flow framework's conventions, and is a
fully current, non-deprecated HA pattern (confirmed via source: `OptionsFlow.config_entry` is
auto-resolved by the framework in current HA core, no manual `__init__` storage needed —
`OptionsFlowWithConfigEntry`, the older explicit-storage pattern, is the one HA core's own comments
say "should not be referenced in new code").
**Alternatives considered:** `SchemaConfigFlowHandler` — rejected per the above; would be worth
adopting only if this integration's config/options surface grows enough fields/steps that hand-written
flow classes become genuinely harder to maintain than the declarative alternative, which two fields
does not.

## 2026-08-08 — Options changes trigger an automatic config-entry reload
**Decision:** `entry.add_update_listener(_async_reload_entry)` is registered during setup, so
changing zone-fusion options (tracked persons, near-house zones) via the options flow immediately
triggers `hass.config_entries.async_reload()` — unlike topology edits, which need a manual reload
per the Phase 4 "or on next reload" precedent.
**Why:** This is the standard, well-established HA pattern for options that affect setup-time wiring
(verified: `ConfigEntry.add_update_listener` exists and takes an `UpdateListenerType =
Callable[[HomeAssistant, ConfigEntry], Coroutine[Any, Any, None]]`). Unlike the topology snapshot
(which changes via a not-yet-built live editor with no natural "save" moment to hook a reload to
until Phase 7 exists), options-flow changes have an exact, well-defined completion event — the
flow's final `async_create_entry` call — that HA core already surfaces as an update event. There's
no reason to defer live-updating here the way Phase 4 deferred it for topology; the mechanism is
already there and costs nothing extra to wire up.
**Alternatives considered:** Requiring a manual reload for options changes too, for consistency with
the topology precedent — rejected because the two cases aren't actually analogous (one has a clean
reload hook available for free, the other doesn't yet), and forcing avoidable friction onto the user
isn't a virtue in itself.

## 2026-08-08 — Provenance resolution: id-equality matching, not parent_id-chain walking
**Decision:** `provenance.resolve_provenance()` matches a state change's `Context` against the
known-automation-context set primarily by **`context.id` equality**, with `context.parent_id`
equality as a one-hop fallback — not by walking an arbitrary-depth `parent_id` chain.
**Why:** `ARCHITECTURE.md` §4 says to "walk the `Context.parent_id` chain (bounded depth)," which
reads as implying `parent_id` is the primary link. Verified against the actual installed
`homeassistant` 2026.8.1 source before implementing (per `CLAUDE.md` rule 1) — tracing
`components/automation/__init__.py`'s trigger path through `helpers/script.py` (the automation
creates a `trigger_context`, fires `EVENT_AUTOMATION_TRIGGERED` with it, then runs the resulting
script actions *with that same `Context` object*, which HA's `core.py:2881`
`ServiceCall`/`entity.py`'s `async_set_context`/`async_set_internal` pass through unchanged to the
entities it updates) shows the common case is **not** a parent/child relationship at all — the
state-changed event's `context.id` is *literally identical* to the `EVENT_AUTOMATION_TRIGGERED`
event's `context.id`. A `parent_id` link only appears when something deliberately mints a *new*
child `Context`, which does happen (verified: `core.py:2881`'s `context = context or Context()`
default, and some multi-target/group entities), but is the exception, not the rule. Home Assistant's
own Logbook (`components/logbook/processor.py`) confirms this shape too: it never walks beyond one
hop (`context_id` → `context_parent_id`) in its `_humanify`/`ContextAugmenter` resolution — there's
no evidence anywhere in Logbook's source of a deeper walk. Matching only by a (possibly-absent)
`parent_id` would have missed the common case entirely.
**Alternatives considered:** Recursive/unbounded `parent_id` walking as `ARCHITECTURE.md`'s wording
suggested — rejected once source-tracing showed it wouldn't have matched the common case anyway, and
that HA's own reference implementation (Logbook) never walks more than one hop either. `ARCHITECTURE.md`
§4 could be read as slightly imprecise here; not editing it retroactively since it captured the
right *intent* (bounded, source-grounded chain resolution) even if "chain" undersold how shallow that
resolution actually needs to be — this entry is the correction of record.

## 2026-08-08 — Automation/script context tracking via live event listening, not Logbook's DB approach
**Decision:** `AutomationContextTracker` builds its known-automation-context set by listening live
for `EVENT_AUTOMATION_TRIGGERED`/`EVENT_SCRIPT_STARTED` and keeping a bounded (256-entry) in-memory
LRU of context ids — it does not use, or attempt to replicate, the Recorder-database-backed approach
Home Assistant's own Logbook actually uses for its primary (historical) query path.
**Why:** Verified `components/logbook/processor.py`: Logbook's main resolution path queries the
Recorder's Events/States tables (joined on `context_id_bin`/`context_parent_id_bin`) and only falls
back to a small live cache to bridge historical→live streaming. That's the right design for a
component whose job is rendering history, but wrong for this integration: Occupancy Tracker only
ever needs to classify *live* signals as they arrive (there's no "look up what happened yesterday"
requirement anywhere in `SPEC.md`), and depending on the Recorder would mean depending on it being
enabled/configured at all (it's optional and user-disableable) plus doing DB queries on what needs
to be a fast, synchronous-feeling event-processing path. A live event-listener cache is simpler,
has no Recorder dependency, and is sufficient for the actual requirement.
**Alternatives considered:** Querying the Recorder directly (Logbook's approach) — rejected as
unnecessary infrastructure for a live-only need, and a new optional-component dependency this
integration shouldn't require. Not tracking automation context at all and relying solely on
`user_id` presence/absence — rejected because it collapses the automation-suppressed and
ambiguous-physical tiers into one, discarding exactly the distinction `SPEC.md` §6.6 asks for.

## 2026-08-08 — `ProvenanceTier` lives in `occupancy_engine.py`, not `provenance.py`
**Decision:** The `ProvenanceTier` enum (`AUTOMATION_SUPPRESSED`/`USER_CONFIRMED`/
`AMBIGUOUS_PHYSICAL`) is defined in `occupancy_engine.py`, even though the code that actually
*produces* a tier from a real `Context` (`resolve_provenance`) lives in the new `provenance.py`.
**Why:** `provenance.py` needs `homeassistant.core.Context` to do its job, so it necessarily depends
on `homeassistant`. If `ProvenanceTier` lived there instead, `occupancy_engine.py` would need to
import it from `provenance.py` to type its `Signal.provenance` field — reintroducing exactly the
transitive-HA-import problem the Phase 3 "standalone graph types" decision was written to prevent
(`occupancy_engine.py` must stay HA-import-free for `docs/TESTING.md` layer 1 to mean what it
claims). Defining the enum where it's *consumed* (the engine) and having the resolver module depend
downward on it — the same "adapter depends on the pure core, never the reverse" shape already
established by `engine_adapter.py` — keeps that property intact. The enum itself needs nothing from
`Context`; only the function that classifies a `Context` into one does.
**Alternatives considered:** Defining `ProvenanceTier` in `provenance.py` and accepting the
transitive HA import in `occupancy_engine.py` — rejected for the reason above. A third, shared
"tags" module with zero HA imports that both depend on — unnecessary for a single two-line enum;
revisit only if more shared, HA-independent types accumulate.

## 2026-08-08 — Dropped the unused `Signal.confidence: float`, replaced with `provenance`
**Decision:** `AreaActivitySignal`/`ConnectorActivitySignal`'s `confidence: float = 1.0` field
(added in Phase 3) is removed and replaced with `provenance: ProvenanceTier =
ProvenanceTier.USER_CONFIRMED`, not kept alongside it.
**Why:** `confidence` was never actually read anywhere in the engine after Phase 3 — a genuinely
dead field, present only because `ARCHITECTURE.md` §1.3 describes the Signal shape as "(source,
value, confidence, provenance, timestamp)." Now that real provenance classification exists, keeping
an unused numeric field alongside it would be exactly the kind of half-finished, speculative surface
`CLAUDE.md` warns against — `ProvenanceTier`'s three discrete tiers already carry the confidence
distinction `SPEC.md` §6.6 asks for (suppress / accept / weak-positive) more precisely than an
arbitrary float would, without inventing numeric thresholds nothing in the spec justifies yet. If a
genuinely continuous confidence dimension turns out to be needed later (e.g. Phase 6 zone-fusion
"raise confidence" language), it can be added then, grounded in an actual consumer.
**Alternatives considered:** Keeping both fields — rejected, since nothing would have populated or
read `confidence` differently from before (still dead weight). Computing a derived float from
`ProvenanceTier` for display purposes — deferred; no current consumer needs it.

## 2026-08-08 — Entity platforms: topology snapshot fixed at setup, not live-updated
**Decision:** `engine_adapter.build_house_graph()` runs once during `async_setup_entry`, and
`SignalIngestion.async_start()` subscribes to that same snapshot's selected entities once. A later
edit to the topology (new Connector, new egress point, changed entity selection) does not rebuild
the engine's graph or the signal subscriptions while the entry keeps running.
**Why:** `SPEC.md` §7.3 explicitly says topology-editor changes take effect "immediately (**or on
next reload**)" — the parenthetical is a deliberate escape hatch. Building live graph/subscription
updates now would be speculative: Phase 7 (the topology editor itself) doesn't exist yet, so there's
no real trigger to wire this to, and no way to test it against actual user edits rather than a
synthetic one. Scoping Phase 4 to "correct as of setup, reload to pick up topology changes" keeps
the slice bounded and matches what the spec already permits, rather than building a live-update path
speculatively ahead of the feature that would exercise it.
**Alternatives considered:** Rebuilding the graph and resubscribing on every `TopologyStore` save —
deferred to whichever phase actually adds a live topology-editing surface (Phase 7), where it can be
designed and tested against the real editing flow instead of guessed at now.

## 2026-08-08 — Engine push-updates via a listener hook, not a DataUpdateCoordinator
**Decision:** `OccupancyEngine` gained `add_listener()` (fires after any `process_signal()` call);
entity platforms register a callback there that calls `self.async_write_ha_state()`, with
`_attr_should_poll = False`. No `DataUpdateCoordinator` is used.
**Why:** `docs/ARCHITECTURE.md` §9 bans polling loops standing in for real update mechanisms, and
`DataUpdateCoordinator` is fundamentally poll-oriented (even push-triggered coordinator patterns add
a layer of indirection this doesn't need) — the engine already knows the instant its belief state
might have changed (every `process_signal()` call), so a direct listener hook is the more precise,
lower-overhead mechanism and mirrors the identical pattern already established for
`RegistrySync.async_add_listener()` in Phase 1.
**Alternatives considered:** n/a — `DataUpdateCoordinator` was never seriously in the running given
the explicit no-polling architectural constraint; noting it here mainly to record that the omission
is deliberate, not an oversight.

## 2026-08-08 — No device grouping for entities in Phase 4
**Decision:** `sensor.py`/`binary_sensor.py` entities don't set `device_info` or
`has_entity_name` — each entity has a fully descriptive, standalone `_attr_name` (e.g. "Kitchen
Occupant Count") instead of being grouped under a shared "Occupancy Tracker" device with
short per-entity names.
**Why:** Modern HA convention favors `has_entity_name = True` plus a shared device for exactly this
"one integration, many related entities" shape, but that pattern's naming/translation-key
interactions couldn't be confirmed against a clear precedent in the installed HA core source in the
time this slice had budgeted (a real HACS "helper"-type integration producing many entities without
an underlying physical device wasn't found to check against), and getting it wrong would be a
visible, annoying-to-fix-later UX regression. Standalone descriptive names are unambiguous, fully
correct HA API usage today, and don't block adding device grouping later — `unique_id`s don't need
to change to add a `device_info` retroactively.
**Alternatives considered:** Creating a synthetic "Occupancy Tracker" device now — deferred rather
than guessed at; revisit in Phase 8 (Polish & packaging) with time to verify the pattern properly
against HA developer docs.

## 2026-08-08 — Occupancy engine: standalone graph types, not shared with topology_store/registry_sync
**Decision:** `occupancy_engine.py` defines its own `HouseGraph`/`GraphConnector` types rather than
importing `topology_store.Connector`/`TopologyData` or `registry_sync.HouseShape` directly. Building
a `HouseGraph` from the live topology store + registry sync layer is left for a later phase's
adapter code, not built now.
**Why:** `docs/ARCHITECTURE.md` §1.4 requires the engine have "no Home Assistant import dependency
it doesn't strictly need" so it's "testable as plain Python... independent of HA being running at
all." `topology_store.py` imports `homeassistant.core`/`homeassistant.helpers.storage` and
`registry_sync.py` (which itself imports all four HA registry helper modules) — importing either
into the engine would drag those in transitively at module-load time, even though neither module's
*types themselves* need HA. That would mean `docs/TESTING.md` layer 1 (fast, no-HA-dependency
engine tests) could never actually be HA-independent in practice, and — confirmed while verifying
this — even a test file with zero HA imports still can't run under plain `pytest` if
`pytest-homeassistant-custom-component` happens to be installed anywhere in that Python environment,
since it autoloads as a global pytest plugin. Keeping the engine's own types import-clean is the
only way layer 1 delivers on its actual purpose.
**Alternatives considered:** Reusing `topology_store.Connector` directly in the engine — rejected for
the transitive-import reason above. A shared "graph types" module with zero HA imports that both
`topology_store.py` and `occupancy_engine.py` depend on — a reasonable option to reconsider once
Phase 4's engine-from-topology adapter is actually written and the duplication (if any) is real
rather than hypothetical; not done now since the two `Connector` shapes (persisted vs. engine-graph)
happen to serve different concerns (topology_store's carries no "OUTSIDE" sentinel handling, for
instance) and forcing them into one shape prematurely risks coupling that isn't there yet.

## 2026-08-08 — Occupancy engine: timing-gated direct transit inference for sensor-less Connectors
**Decision:** Most Connectors have no sensor of their own to fire a `ConnectorActivitySignal` at all
(`SPEC.md` §7.3's topology editor only lets users bind entities to egress points, never to an
ordinary Connector edge; §5.1 explicitly allows a sensor-less connecting Area). For these, when a
previously-empty Area gets an `AreaActivitySignal`, the engine looks at every Connector-adjacent
Area that's currently occupied and checks the gap between that neighbor's last confirmed evidence
and this new signal's timestamp against two bounds on `EngineConfig`: it must be at least
`min_transit_time` (a person can't teleport between rooms — a near-simultaneous pair of signals in
two different Areas is read as two distinct people, not one moving) and at most
`transit_confirmation_window` (stale evidence is more likely unrelated than "shortly after," per
§6.3's own wording). If exactly one neighbor's gap falls in that window, the engine treats it as a
direct, immediately-confirmed transit from that neighbor (no pending step — there's no separate
"candidate evidence" event to wait on when destination activity *is* the only evidence). Otherwise
(zero or multiple neighbors qualify) it's read as a new occupant.
**Why:** An earlier version of this rule inferred a transfer whenever exactly one adjacent Area was
occupied, with no timing check at all. Project-owner review caught the real product cost: two people
in adjacent connected rooms could never both be counted, because the second person's arrival was
always read as the first person walking over — motion sensors in both rooms firing at essentially
the same instant is physically impossible for one person, and should read as two. The fix (timing
gate, contributed by the project owner) resolves this directly: implausibly-fast gaps become
evidence *for* a second occupant rather than being silently misattributed. The alternative of never
inferring a transfer at all for sensor-less Connectors was rejected earlier in the same review
(see "no occupied neighbor" test coverage in `tests/test_occupancy_engine.py`) because it leaves a
stale occupant behind in every room someone exits through an unsensored doorway, which is a worse,
more visible defect given per-room accuracy is the product's headline feature (`SPEC.md` §1).
**Alternatives considered:** Classifying signals by device class/manual-vs-automated origin (e.g. "a
manually-toggled light with no corresponding hallway motion implies presence") to help disambiguate
these cases further — a good idea, but it belongs to signal ingestion/provenance resolution (Phase
5, `SPEC.md` §6.6), which decides *whether and at what confidence* to emit a `Signal` in the first
place; by the time a `Signal` reaches the engine it's already domain-agnostic, so the engine itself
has no way to know what kind of entity produced it, nor should it. Picking the timing-plausible
neighbor "closest" to the ideal walking gap when multiple neighbors qualify, rather than treating
multiple qualifying neighbors as ambiguous — rejected as unnecessary added complexity without a
demonstrated need; revisit if real-world testing shows the all-or-nothing ambiguity rule discards
too many resolvable cases.

## 2026-08-08 — Occupancy engine: transit direction inferred from occupancy asymmetry
**Decision:** When a Connector between two real Areas fires, the engine infers transit direction by
occupancy alone: whichever adjacent Area currently has `occupant_count > 0` while the other has `0`
is presumed the source, the other the destination, and a pending transit is registered awaiting
destination-Area corroboration within `EngineConfig.transit_confirmation_window`. If both Areas are
occupied or both are empty, the Connector event alone is treated as inconclusive — no pending
transit is registered, no count changes.
**Why:** `SPEC.md` §6.3 describes the *behavior* ("a Connector's sensor firing shortly after an
occupied Area goes quiet is candidate evidence of an exit via that path... destination Area is
watched for corroboration") but not the exact algorithm — §10 explicitly calls the component
breakdown "rough... to be refined during implementation, not frozen here." Occupancy asymmetry is
the simplest signal that's actually derivable from the engine's own state (no extra "was this area
just active" bookkeeping needed beyond what latching already tracks) and degrades safely: when it
can't resolve a direction, it does nothing rather than guessing, consistent with the product's
general bias toward lower confidence over confidently-wrong inference.
**Alternatives considered:** Tracking a literal "quiet period" (Area had activity, then N seconds of
silence) as the trigger condition instead of/in addition to occupancy — rejected for now as an extra
tunable and extra state without a clear behavioral difference in the common case (an Area that just
lost its only occupant already reads as "went quiet" the moment the count would drop, which doesn't
happen until the transit confirms anyway). Revisit if real-world testing shows occupancy-asymmetry
alone produces too many missed/false transit candidates.

## 2026-08-08 — Occupancy engine: asymmetric egress confirmation (departure immediate, arrival waits)
**Decision:** For an egress Connector (one side `OUTSIDE`), a departure (egress Area occupied, then
its crossing sensor fires) confirms **immediately** — no pending window. An arrival (egress Area
empty, crossing sensor fires) instead registers a pending transit awaiting a corroborating
`AreaActivitySignal` in that same Area, exactly like a regular transit.
**Why:** The destination-corroboration mechanism inherently can't apply to a departure, because
`OUTSIDE` is never tracked and can never itself emit a corroborating signal — waiting for
"corroboration that will never come" would mean departures never confirm at all, which contradicts
`SPEC.md` §6.5's plain statement that egress-point activity "drives arrival/departure changes to the
whole-house occupant total." Since the egress Area was already known-occupied, the crossing sensor
itself (a door/window specifically bound as *the* crossing sensor for that boundary, per §5.4) is
strong, direct, sufficient evidence of a departure on its own. Arrivals get the opposite treatment
deliberately, not just for symmetry with regular transits: a door/window opening alone doesn't prove
someone *entered* (wind, a pet, retrieving a parcel from the porch) the way it proves someone with a
known location *left* through it, so arrivals still wait for the egress Area's own activity signal
to corroborate someone is actually now inside.
**Alternatives considered:** Confirming both directions immediately on crossing-sensor activity —
rejected because it would count e.g. a gust of wind opening a door as a confirmed arrival with no
supporting evidence at all, which is worse than the missed-transit failure mode the confirmation
window exists to avoid elsewhere. Requiring corroboration for departures too (e.g. waiting to see
the egress Area itself go quiet) — rejected as unimplementable in this model since "quiet" isn't a
trackable event distinct from the count changing, which is the very thing being decided.

## 2026-08-08 — Occupancy engine: `confirmed_freshness_window` added as a new tunable
**Decision:** `EngineConfig` gained a `confirmed_freshness_window` field (default 10 minutes)
governing how long after direct evidence an Area's quality tier stays `CONFIRMED` before degrading
to `LATCHED`. This is in addition to `transit_confirmation_window`, which `SPEC.md` §6.3 already
implies is needed.
**Why:** `SPEC.md` §6.2 states the occupant *count* never decays from absence of signal — but §6.8's
three-tier confirmed/latched/ambiguous model only means something if `CONFIRMED` and `LATCHED` are
actually distinguishable at some point after the evidence arrives; without a freshness window,
every non-ambiguous Area would read as `CONFIRMED` forever after a single signal, collapsing the
tier to two states in practice and making "latched" a dead label. This is exactly the kind of tunable
`docs/ARCHITECTURE.md` §2 anticipates ("decay/confirmation windows, confidence thresholds... flow
through one typed config object") — added to `EngineConfig` now rather than deferred, since without
it the quality tier as specified couldn't be implemented at all, not because it's speculative
future-proofing.
**Alternatives considered:** n/a — this was required to implement §6.8 as specified, not a
discretionary addition.

## 2026-08-08 — Occupancy engine: stale pending transits are dropped, not allowed to double-drain
**Decision:** `_confirm_transit` checks whether the source Area's occupant count is still `> 0`
before decrementing it; if a different, already-confirmed transit already drained that Area to zero
while this pending transit was still waiting, the stale pending transit is silently dropped (no
count change to either side) instead of confirming.
**Why:** The engine tracks occupancy as a per-Area integer count, not per-token identity, so two
Connectors that both inferred the same occupied Area as their source (e.g. two doorways off one
room, both firing before either confirms) can end up with two pending transits both claiming the
same single occupant. Confirming the second one without this guard would drive the source Area's
count to -1, violating `SPEC.md` §6.2's explicit non-negative-count requirement. Dropping the stale
transit rather than fabricating an ungrounded arrival on the destination side is the same
conservative bias applied elsewhere in this engine (§6.3's "no corroboration → assume no transit"
already establishes that an unresolved pending transit defaults to "nothing happened," not the
reverse). Covered by
`tests/test_occupancy_engine.py::test_two_pending_transits_from_the_same_source_do_not_double_drain_it`.
**Alternatives considered:** Tracking occupancy as identified tokens rather than a bare count, so
each pending transit could claim a specific token and this race couldn't occur structurally —
rejected for Phase 3 as substantially more complexity than the product currently needs (SPEC.md
never requires per-occupant identity, only counts) for an edge case fully handled by a much smaller
guard.

---

## 2026-08-08 — Topology store: full-rebuild reconciliation, drop-not-flag on broken references
**Decision:** `TopologyStore.reconcile()` (`custom_components/occupancy_tracker/topology_store.py`)
recomputes the cleaned topology from scratch against the current `HouseShape` every time it runs
(same full-rebuild philosophy as the registry sync layer's `HouseShape`, not incremental patching),
and its resolution for every kind of broken reference is **drop the reference**, not flag-and-keep:
a Connector loses an endpoint Area → the whole Connector is dropped; an egress point's Area is
removed → the whole egress point is dropped; an egress point's bound entities are all removed → the
egress point is dropped (a partial removal instead keeps the point with only the surviving
entities); a per-area entity selection's Area is removed, or one of its entities moves to a
*different* Area, → that entry is dropped for the affected entity/area. Every drop is collected into
a human-readable `removed: list[str]` returned alongside the cleaned topology, and
`async_reconcile_and_save()` only writes to disk when that list is non-empty.
**Why:** `SPEC.md` §5.3 requires broken topology references be surfaced, "rather than fail silently
or crash" — it does not require them be preserved. Dropping is simpler and safer than trying to keep
a half-valid reference around (e.g. a Connector with one real Area and one dangling id) that
downstream code (the future occupancy engine) would then have to specially guard against everywhere
it reads topology. A per-area entity selection entry for an entity that moved to a different Area is
treated as a drop, not a move, because "this entity is relevant evidence for occupancy in *this*
Area" was a judgment the user made about the entity's old Area — silently re-homing that judgment to
wherever the entity now lives could be wrong (e.g. an entity moved into a hallway it's not
diagnostic for) and the user should reselect deliberately instead. Save-only-when-changed avoids an
unnecessary write (and unnecessary reconciliation-log noise) on the common case where a registry
event fires but doesn't actually touch anything the topology references.
**Alternatives considered:** Flagging broken references as "disabled" but keeping them in storage,
so a since-recreated Area/entity could reattach automatically — rejected as speculative complexity
with no demonstrated need yet (HA doesn't guarantee a recreated Area gets the same `area_id`, so the
reattachment case this would serve may not even reliably occur); revisit only if real usage shows
users frequently rename via delete+recreate rather than HA's actual rename operation (which keeps
the same `area_id` and therefore needs no special handling at all — verified in Phase 1's research:
`area_registry.py`'s `async_update` changes `name` in place, it does not change `id`). Re-homing a
moved entity's selection to its new Area automatically — rejected per the reasoning above.

## 2026-08-08 — `entry.async_create_task`, not `hass.async_create_task`, for reconciliation
**Decision:** The registry-sync-changed listener in `__init__.py` schedules the async
reconcile-and-save via `entry.async_create_task(hass, ...)` (`config_entries.py:1379`), not
`hass.async_create_task(...)`.
**Why:** Verified directly from source: `HomeAssistant.async_create_task`'s own docstring
(`core.py:788`) says "If you are using this in your integration, use the create task methods on the
config entry instead." `ConfigEntry.async_create_task` ties the task's lifetime to the entry
(tracked in `entry._tasks`, awaited appropriately during setup/unload), which is the correct
behavior for a task a config entry's own listener spawns.
**Alternatives considered:** n/a — this was a verification question per `CLAUDE.md` rule 1, not a
design trade-off.

---

## 2026-08-08 — Registry sync layer: full-rebuild-on-any-event, direct bus listeners, `runtime_data`
**Decision:** `RegistrySync` (`custom_components/occupancy_tracker/registry_sync.py`) rebuilds its
entire `HouseShape` snapshot from scratch on any of the four registry-updated events, rather than
patching incrementally, and fires one generic "changed" notification to listeners (no per-change
diff payload). It subscribes directly via `hass.bus.async_listen(EVENT_..._REGISTRY_UPDATED, ...)`
for all four registries, not the dedicated `async_track_entity_registry_updated_event` /
`async_track_device_registry_updated_event` helpers in `homeassistant/helpers/event.py`. The
instance is stored on the config entry via the typed `ConfigEntry[RegistrySync]` `runtime_data`
attribute (confirmed present in the installed `homeassistant` 2026.8.1 core at
`config_entries.py:398`, generic `class ConfigEntry[_DataT = Any]` at `config_entries.py:391`), not
`hass.data[DOMAIN][entry.entry_id]`.
**Why:** Full-rebuild-on-any-event was chosen because the registries are already fully in memory
(no I/O), the whole-house model is small, and a diffing approach adds real complexity (four
different TypedDict event shapes — verified: area/device/entity carry an `action` field describing
create/remove/update/reorder with different payload fields per action, area_registry.py:66,
device_registry.py:178-205, entity_registry.py:156-175 — floor's `reorder` case doesn't even carry
a `floor_id`) for no demonstrated benefit yet; `docs/ARCHITECTURE.md` §1.1 only requires "a
simplified change event," not a diff. The dedicated per-id tracker helpers were rejected because
they're scoped to specific, already-known entity/device ids (their signature takes an
`entity_ids`/`device_ids` argument) — unsuitable for a layer that must also notice *brand-new*
areas/devices/entities appearing, which is exactly the registry-sync layer's job per `SPEC.md` §5.3.
No dedicated tracker helper exists for area or floor registries at all (confirmed absent from
`homeassistant/helpers/event.py`) — the raw bus event is the only mechanism for those two
regardless. `entry.runtime_data` was used over a `hass.data` dict because it's the current
HA-recommended typed pattern (avoids a stringly-keyed dict and a manual per-entry cleanup dance) and
was already confirmed available in the installed core version before use.
**Alternatives considered:** Incremental/diffed updates using the event `action`/`changes` payloads
— rejected for now as premature complexity; may be revisited if a full rebuild becomes a measurable
cost on a large installation. `hass.data[DOMAIN][entry.entry_id]` for the shared instance — rejected
as the older pattern now superseded by `runtime_data` in current HA core.

## 2026-08-08 — Entity area resolution matches HA's own precedence, verified from source
**Decision:** `EntitySnapshot.area_id` resolves an entity's own `area_id` first, falling back to its
linked device's `area_id` only when the entity's is `None` — never the reverse, and never merged.
**Why:** `SPEC.md` §5.2 states an entity "can inherit its device's area, or override it directly"
but doesn't spell out which wins if both are set. Rather than guess, this was checked directly
against the installed `homeassistant` 2026.8.1 source: `entity_registry.py`'s
`_async_get_full_entity_name` (around line 524) contains `if area_id is None: area_id =
device.area_id` — i.e. the entity's own value wins outright, device is purely a fallback. This is
the same precedence Home Assistant's own core UI uses, so matching it avoids the registry sync
layer disagreeing with what a user sees in HA's own area pages.
**Alternatives considered:** None — this was a verification question, not a design trade-off (per
`CLAUDE.md` rule 1, every HA API/behavior assumption must be checked against source, not recalled).

---

## 2026-08-08 — Local HA-integration testing runs via a WSL2 venv, not native Windows Python
**Decision:** `pytest-homeassistant-custom-component` runs from a Python virtual environment inside
WSL2 (Ubuntu), at `.venv-wsl/` in the repo root (gitignored, machine-local, not committed). Native
Windows Python remains fine for Phase 0-style pure-Python tests (`tests/test_manifest.py`) but is
not used for anything importing `homeassistant.runner` or the `hass` fixture.
**Why:** `homeassistant.runner` unconditionally imports the Unix-only `fcntl` module, and since
`pytest-homeassistant-custom-component` autoloads as a pytest plugin, this broke the entire pytest
run under native Windows, not just HA-touching tests (see `docs/TESTING.md` §1a). The project owner
installed WSL2 + Ubuntu and, after installing `python3.14-venv` via `sudo apt install`, a venv was
created and `requirements-test.txt` installed successfully. Verified working end-to-end: `import
homeassistant.runner` succeeds, the `homeassistant-custom-component` plugin loads during collection,
the existing manifest test suite passes, and a throwaway test using the real `hass` fixture passed
before being deleted. This unblocks Phase 1 onward (`docs/STATUS.md`), which needs HA-integration
tests (TESTING.md layer 2).
**Alternatives considered:** Docker/devcontainer-based workflow — not needed once WSL2 was
confirmed working, and WSL2 has less per-session overhead (no container start/stop, direct
filesystem access to the repo via `/mnt/c/...`). Relying on GitHub Actions CI as the only place
layer-2+ tests run — rejected as the primary path since it would slow local iteration; CI remains
the authoritative gate regardless.

## 2026-08-08 — Phase 0 scaffolding: manifest fields and config flow shape, verified against HA core source
**Decision:** `manifest.json` uses `integration_type: "helper"` and `iot_class: "calculated"` (not
`"entity"`/other options) — verified by reading the real `manifest.json` of HA core's `group` and
`derivative` integrations directly from `home-assistant/core`, both of which are the closest
existing analogues to Occupancy Tracker (an entity computed from other existing entities, not a
new physical device or cloud service). `config_flow.py` uses the plain
`class X(ConfigFlow, domain=DOMAIN)` pattern (not the newer `SchemaConfigFlowHandler`, which
`derivative` uses but which is unnecessary complexity for a confirmation-only flow), verified
against `home-assistant/core`'s `local_ip` integration source, itself. `manifest.json` sets
`single_config_entry: true` (HA 2024.3+) so HA core blocks a second config entry automatically —
no manual `_async_current_entries()` check needed in `config_flow.py`.
**Why:** Every one of these was checked against real HA core source or developer docs before being
written, per `CLAUDE.md` rule 1 — this is exactly the class of "plausible-sounding API" mistake
that broke the v0 prototype.
**Alternatives considered:** n/a — this entry exists to record what was verified and against what,
not a design trade-off.

## 2026-08-08 — Custom integrations must use translations/en.json, not strings.json
**Decision:** The config flow's UI text lives in `custom_components/occupancy_tracker/translations/en.json`
with full, flat English text. No `strings.json` file exists in the integration.
**Why:** Verified against HA developer docs (`docs/internationalization/custom_integration/`):
`strings.json` and the `[%key:...%]` core-translation-key placeholder syntax are **build-time-only
features of Home Assistant core's own Lokalise pipeline** — a custom (HACS-distributed) integration
never runs through that pipeline, so a `strings.json`-only integration silently shows raw
translation keys instead of text in the UI. This is a well-documented, easy-to-hit trap for anyone
copying patterns from HA core source (which does use `strings.json`) into a custom component — the
exact "plausible but wrong for this context" failure mode `CLAUDE.md` rule 1 exists to catch.
**Alternatives considered:** n/a — this is a hard platform requirement, not a design choice.

## 2026-08-08 — HACS validation ignores the brand-assets check for now
**Decision:** The `hacs/action` CI job passes `ignore: brands`, skipping the check that requires a
submitted `home-assistant/brands` entry (icon/logo assets).
**Why:** Brand-asset submission is part of the "full HACS default-repository bar" question in
`SPEC.md` §13, explicitly deferred to before Phase 8. Custom-repository installs (the near-term
distribution path) don't require it; failing CI on a Phase-8 concern from Phase 0 onward would be
noise, not signal.
**Alternatives considered:** Submitting brand assets now — rejected as premature; revisit when
`SPEC.md` §13's HACS-submission-bar question is answered.

## 2026-08-08 — Near-house zones are user-picked
**Decision:** Which HA zones count as "near the house" for pre-arming (`SPEC.md` §6.7) is an
explicit user selection in the options flow, not auto-detected by proximity to `zone.home`'s
radius.
**Why:** Auto-detection would require a geodetic-distance heuristic that can guess wrong for
unusual zone shapes/sizes (e.g. an elongated driveway zone, a multi-property zone), and would be
hard for the user to predict or debug. Explicit selection is predictable and trivially correct
across arbitrary user setups.
**Alternatives considered:** Auto-detect zones within a radius of `zone.home` — rejected as an
unnecessary heuristic for a one-time, low-frequency setup choice.

## 2026-08-08 — Household-size hint is whole-house scope
**Decision:** The "typical household size" confidence hint (`SPEC.md` §6.4, §7.2) is a single
whole-house value, not settable per area.
**Why:** Keeps the config surface and its persistence/test coverage small. Per-area hints would
add configuration, storage schema, and test surface for a secondary tuning knob without a clearly
demonstrated need yet — can be revisited if real-world confidence tuning shows whole-house scope
is too coarse.
**Alternatives considered:** Per-area hints (e.g. "primary bedroom rarely has more than 2") —
rejected for now as added complexity without demonstrated need.

## 2026-08-08 — Floors are display-only
**Decision:** Floors (`SPEC.md` §6.1) are a display/grouping attribute in the topology editor only.
The transit-inference algorithm treats every Connector identically regardless of which floor(s) it
spans — a staircase connector is not weighted or timed differently from a same-floor doorway
connector.
**Why:** Keeps the engine's first version simpler and avoids adding a floor-aware dimension to
transit confirmation windows/confidence before there's evidence it's needed. Can be revisited if
real-world testing shows same-floor and cross-floor transits need materially different treatment
(e.g. stairs are slower and should have longer confirmation windows).
**Alternatives considered:** Cross-floor connectors get different confirmation windows/confidence
than same-floor ones — rejected for v1 as premature complexity.

## 2026-08-08 — Documentation suite split out from SPEC.md
**Decision:** Process, architecture, UX, and testing guidance moved out of `SPEC.md` into
dedicated docs (`CLAUDE.md`, `ARCHITECTURE.md`, `AGENT_WORKFLOW.md`, `UX_GUIDELINES.md`,
`TESTING.md`, `STATUS.md`), leaving `SPEC.md` focused on product/functional requirements.
**Why:** This project is intended to be built and maintained primarily by an AI coding agent
across many sessions. A single monolithic spec doc doesn't scale as a working reference — an agent
needs to load only what's relevant to the current task (architecture for structure questions, UX
guidelines only when touching UI, etc.) without pulling the entire product spec into context every
time. Splitting also makes `STATUS.md` a lightweight, frequently-updated file separate from the
comparatively stable `SPEC.md`.
**Alternatives considered:** Keeping everything in one file — rejected as it would grow unbounded
and defeat the point of scoped context loading per session.

## 2026-08-08 — Registry-driven configuration, no hand-authored house config
**Decision:** Rooms, floors, devices, and entities are read live from Home Assistant's Area/Floor/
Device/Entity registries, with continuous sync on registry-update events. Only topology
(room-to-room connections) and egress-point bindings are user-authored, via a visual editor.
**Why:** The project's scope changed from a single personal house to a general-purpose,
HACS-distributed integration for arbitrary users' homes. Hand-authored YAML config doesn't scale
to that and doesn't stay in sync with the user's actual, changing HA setup.
**Alternatives considered:** Continuing with a YAML-based house config (v1 spec) — rejected once
HACS distribution became the goal.

## 2026-08-08 — Occupancy model: latch + topology-based transit inference, not decay
**Decision:** Replaced the original per-room decaying confidence score with a latching model:
occupant counts only change on inferred transit events across a user-defined room-adjacency graph,
never from a timer alone.
**Why:** A decay-based score can't represent a stationary-but-present occupant (e.g. asleep on a
couch) and has no way to reason about whether a person could plausibly have moved between rooms.
**Alternatives considered:** Tuning the decay formula further (e.g. slower decay, per-room decay
rates) — rejected as a workaround that doesn't address the underlying lack of topology awareness.

## 2026-08-08 — Provenance via Context chain, treated as a confidence signal, not a boolean
**Decision:** Automation-vs-manual detection for device-state signals uses Home Assistant's
`Context.parent_id`/`user_id` chain (the same mechanism HA's own Logbook uses), with three
confidence tiers (automation-suppressed / user-confirmed / ambiguous-physical) rather than a hard
accept/reject gate.
**Why:** Researched against HA developer docs and community reports; `parent_id`/`user_id`
propagation is not perfectly reliable in all cases, so treating it as absolute would produce
confidently wrong exclusions/inclusions. Treating it as a confidence signal fits the rest of the
occupancy model's confidence-tiered design.
**Alternatives considered:** Hard boolean gate on `parent_id` presence alone — rejected as too
brittle given documented propagation caveats.

## 2026-08-08 — v0 prototype rejected wholesale
**Decision:** The original prototype implementation (pre-spec) was not carried forward; this
project restarted from a clean specification.
**Why:** A full code review found the prototype non-functional: it could not be installed (no
config flow), one platform module failed to import (incorrect HA enum casing), core methods threw
on every call (missing instance attributes), several Home Assistant APIs used didn't exist
(`hass.event_listener`, `State.async_set_state`, `ConfigEntry.async_update_hass_options`), the
"live sensor data" path was never actually wired to Home Assistant state, and the test suite
didn't run (crashing `setUp`, tests exercising reimplemented logic instead of the real code, and
one literal tautological assertion). These are the specific failure modes several rules in
`CLAUDE.md` and `docs/ARCHITECTURE.md` §3 exist to prevent going forward.
**Alternatives considered:** Patching the prototype incrementally — rejected; the number and depth
of defects made a clean rebuild against a proper spec the more reliable path.
