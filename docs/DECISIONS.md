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
