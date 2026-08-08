# Project Status

**Read this first, every session.** This file is more current than anyone's memory of past
sessions — keep it that way by updating it at the end of every session that changes anything.

## Current phase

**Phase 6 complete.** `docs/SPEC.md` (functional) and `docs/ARCHITECTURE.md` (technical) are the
agreed target design. The old prototype under `custom_components/occupancy_tracker/` predates this
spec, does not conform to it, and should be treated as reference-only for "what not to do" (see
`docs/DECISIONS.md`'s 2026-08-08 entry) — not as a starting point to patch. **The current
`custom_components/occupancy_tracker/` is the new, spec-conformant scaffold**, and is now
end-to-end functional: registry sync (Phase 1) → topology store (Phase 2) → occupancy engine
(Phase 3) → signal ingestion + entity platforms (Phase 4) → provenance resolution (Phase 5) →
zone-presence fusion (Phase 6) all wire together, verified by real integration tests. The topology
editor's WebSocket API + frontend (Phase 7) — the actual way a user sets a real topology — is the
only functional gap left before this is usable against a real house; see Phase 8 for packaging.

## Build phases (planned order)

Work bottom-up: engine logic before HA glue, HA glue before the frontend that depends on it.

- [x] **Phase 0 — Repo scaffolding.** `hacs.json`, `manifest.json` (`config_flow: true`,
      `integration_type: helper`, `iot_class: calculated`, `single_config_entry: true` — all
      verified against real HA core source, see `docs/DECISIONS.md`), confirmation-only
      `config_flow.py` + `translations/en.json`, CI workflow (ruff, mypy, pytest, hassfest, HACS
      validation), `README.md`, `CHANGELOG.md`, `pyproject.toml` (ruff/mypy config),
      `.pre-commit-config.yaml`. `ruff check`, `ruff format --check`, and `mypy` all verified clean
      locally; a manifest-sanity test suite (`tests/test_manifest.py`) verified passing locally.
      hassfest and the HACS validation action have not been run locally (see note below) — they run
      in CI on push. No occupancy-tracking logic yet — that starts at Phase 1.
- [x] **Phase 1 — Registry sync layer.** `custom_components/occupancy_tracker/registry_sync.py`:
      `RegistrySync` builds a read-only `HouseShape` snapshot (areas/floors/entities) from HA's
      Area/Floor/Device/Entity registries, with entity area resolution matching HA's own precedence
      (entity's own `area_id` wins, falls back to its device's) — verified against
      `entity_registry.py`'s `_async_get_full_entity_name`, not assumed. Subscribes directly to all
      four `*_registry_updated` bus events (no per-id tracker helper exists for area/floor, and the
      device/entity tracker helpers are scoped to known ids, not suited to "detect anything new" —
      see `docs/DECISIONS.md`) and rebuilds+notifies on any change. Wired into
      `custom_components/occupancy_tracker/__init__.py` via the typed `ConfigEntry[RegistrySync]`
      `runtime_data` pattern, with `entry.async_on_unload` cleanup. 17 tests passing (registry
      snapshot correctness, entity area-resolution precedence, disabled/hidden flags, live
      registry-update reactions, area-deletion cascade, listener add/remove, unload), all run
      against real HA registry objects via `pytest-homeassistant-custom-component`, not mocks.
      `ruff`/`mypy` clean on the CI-equivalent commands (see `docs/DECISIONS.md` for a caveat on
      `mypy .` vs. `mypy custom_components`).
- [x] **Phase 2 — Topology store.** `custom_components/occupancy_tracker/topology_store.py`:
      `TopologyStore` persists `Connector`s, `EgressPoint`s, and per-area entity selections —
      the genuinely user-authored data per `SPEC.md` §5.4 — via `homeassistant.helpers.storage.Store`,
      versioned (`STORAGE_VERSION_MAJOR`/`MINOR`) with a `_TopologyStorageStore` subclass carrying
      the `_async_migrate_func` migration hook (mirrors HA core's own `FloorRegistryStore` pattern;
      `Store.async_load()` invokes it on any major/minor mismatch — verified from source, not
      assumed). `TopologyStore.reconcile()` is a pure function that strips Connectors/egress
      points/selections referencing Areas or entities no longer in the current `HouseShape`
      (dangling refs are reported, never silently dropped or left corrupting saved state, per
      `SPEC.md` §5.3), and `async_reconcile_and_save()` only writes to disk when something actually
      changed. Wired into `__init__.py`: reconciles once at startup (registries may have changed
      while HA was off) and again automatically on every live registry change, via
      `RegistrySync.async_add_listener` + `entry.async_create_task` (verified: `ConfigEntry`'s own
      task helper, ties task lifetime to the entry, unlike `hass.async_create_task` which its own
      docstring says integrations shouldn't use directly). `runtime_data` is now a small
      `OccupancyTrackerRuntimeData` dataclass holding both `registry_sync` and `topology_store`. 32
      tests passing total (15 new: save/load roundtrip, per-entry storage isolation, migration-hook
      behavior, every reconciliation branch, reconcile-is-a-noop-when-nothing-changed, and an
      end-to-end test proving a live area deletion cascades through to a persisted topology change).
      `ruff`/`mypy` clean on the CI-equivalent commands.
- [x] **Phase 3 — Occupancy engine.** `custom_components/occupancy_tracker/occupancy_engine.py`:
      `OccupancyEngine` is the latch/transit state machine (`SPEC.md` §6.2–§6.5), pure Python with
      **zero Home Assistant imports, even transitive** — it defines its own standalone `HouseGraph`/
      `GraphConnector` rather than importing `topology_store.Connector` or `registry_sync.HouseShape`
      (importing either would drag in `homeassistant.core`/registries transitively, defeating the
      point of a fast, no-HA-dependency test layer). Building a real `HouseGraph` from the live
      topology store + registry sync layer is Phase 4's job. Consumes a normalized `Signal`
      (`AreaActivitySignal | ConnectorActivitySignal`) stream and produces per-Area `AreaState`
      (occupant count + confirmed/latched/ambiguous quality tier, `SPEC.md` §6.8). SPEC.md
      deliberately leaves the transit/direction-inference algorithm unspecified ("rough component
      breakdown... to be refined during implementation") — several original design calls were made
      and are logged in `docs/DECISIONS.md`. Two distinct transit-inference mechanisms coexist: (1)
      occupancy-asymmetry + pending/corroboration for Connectors that *do* have a bound sensor
      (currently only egress points can have one, per `SPEC.md` §7.3), and (2) timing-gated direct
      inference for the common case of a sensor-less Connector — destination activity is the only
      available evidence there, so it's checked against `min_transit_time`/`transit_confirmation_
      window` bounds (a too-fast gap between two Areas' activity can't be one person, since they
      can't teleport, so it's read as a second occupant instead of a transfer; this specific
      correction came from project-owner review of an earlier no-timing version that could never
      count two people in adjacent connected rooms). Also: asymmetric egress confirmation (departure
      confirms immediately, arrival needs corroboration, since "outside" can never itself
      corroborate), a `confirmed_freshness_window` tunable added to give the latched/confirmed
      distinction real meaning, and a guard against two pending transits draining the same source
      area into a negative count. 24 scenario tests passing, covering every branch TESTING.md's
      example list calls out for this layer plus the edge cases above; these tests have **zero HA
      dependency** and are TESTING.md layer 1 — confirmed they collect and run via plain `pytest`
      with no `pytest-homeassistant-custom-component` involvement (though a *global*, non-project
      install of that plugin on native Windows still breaks pytest's plugin autoload for the whole
      invocation regardless of which test file runs — a preexisting, environment-specific quirk, not
      a project issue; WSL remains the standard way to run the full suite locally). `ruff`/`mypy`
      clean on the CI-equivalent commands.
- [x] **Phase 4 — Entity platforms.** Four new modules:
      - `engine_adapter.py`: the *only* module importing both the HA-independent `occupancy_engine`
        and the HA-dependent `registry_sync`/`topology_store` — `build_house_graph()` converts a
        `HouseShape` + `TopologyData` into an engine `HouseGraph`, synthesizing an `OUTSIDE`-facing
        `GraphConnector` for each egress point (regular `Connector`s map straight across). Defensively
        drops anything referencing an Area not in the current `HouseShape` even though
        `TopologyStore.reconcile()` should already guarantee that.
      - `signal_ingestion.py`: first pass at `docs/ARCHITECTURE.md` §1.3 — subscribes to state
        changes (`async_track_state_change_event`, verified signature/event-data shape from source)
        for topology-selected entities and turns a transition to `"on"` into an `AreaActivitySignal`
        or (for egress-bound entities) a `ConnectorActivitySignal`. Deliberately no provenance
        resolution or zone fusion yet — those are Phase 5/6; every Signal is full-confidence.
      - `sensor.py` / `binary_sensor.py`: per-Area occupant-count sensor + occupied binary sensor
        (with `quality` as an inspectable attribute, SPEC.md §6.8) and a house-level total sensor
        (SPEC.md §8). Push-updated (`should_poll = False` + a listener on the engine, never a
        `DataUpdateCoordinator` poll loop) via a new `OccupancyEngine.add_listener()` hook added to
        the Phase 3 engine specifically for this (fires after any `process_signal()` call).
      - `__init__.py` builds one `HouseGraph`/`OccupancyEngine`/`SignalIngestion` per config entry at
        setup time, holds them in `runtime_data` alongside `registry_sync`/`topology_store`
        (never recreated per property read — `docs/ARCHITECTURE.md` §1.4–1.5), and forwards setup to
        both platforms via `hass.config_entries.async_forward_entry_setups`.
      **Known, accepted scope limits** (not bugs — documented so they're not mistaken for regressions
      later): the engine's graph and signal ingestion's subscriptions are a snapshot built once at
      setup, not live-updated when the topology changes afterward — `SPEC.md` §7.3 explicitly permits
      "immediately (**or on next reload**)," so this is deferred rather than built speculatively before
      Phase 7's topology editor exists to exercise it. Similarly, an Area's quality tier (confirmed →
      latched, or a pending transit timing out) only updates in the UI when the *next* Signal touches
      that Area, not from a precisely-timed scheduled callback — the occupant count and occupied
      state, the primary facts, are always accurate and always push-update immediately; only the
      freshness *label* can lag. 20 new tests (69 total): `engine_adapter` graph-building +
      defensive-drop cases, `signal_ingestion` wiring (on/off/unselected/egress/unsubscribe), and one
      true end-to-end test (`test_entities.py`) proving a real `hass.states.async_set()` call reaches
      `sensor.kitchen_occupant_count`/`binary_sensor.kitchen_occupied`/`sensor.total_occupant_count`
      through every layer. `test_init.py` now uses the real `hass.config_entries.async_setup()`/
      `async_unload()` flow (needs `enable_custom_integrations`) instead of calling
      `async_setup_entry`/`async_unload_entry` directly, since platform forwarding requires the
      integration to be loader-discoverable. `ruff`/`mypy` clean on the CI-equivalent commands.
- [x] **Phase 5 — Provenance resolver.** Two new pieces in `provenance.py`, deliberately separated:
      `resolve_provenance(context, known_automation_context_ids)` is a pure function (unit-testable
      with constructed `Context` objects, no running `hass` needed — `homeassistant.core.Context` is
      a plain, standalone-constructible object, unlike `HomeAssistant`/registries/`Store`) that
      classifies a `Context` as `AUTOMATION_SUPPRESSED` / `USER_CONFIRMED` / `AMBIGUOUS_PHYSICAL`
      (`SPEC.md` §6.6). `AutomationContextTracker` is the stateful, HA-dependent half — builds the
      live set of automation/script-trigger context ids by listening for
      `EVENT_AUTOMATION_TRIGGERED`/`EVENT_SCRIPT_STARTED` (bounded LRU, 256 entries), the same
      events Home Assistant's own Logbook integrates against (verified by tracing through
      `components/automation/__init__.py`, `helpers/script.py`, and `components/*/logbook.py` — see
      docs/DECISIONS.md for what that trace found, including a real discovery: the common case
      matches by `Context` **id equality**, not `parent_id` walking, because HA passes the *same*
      `Context` object through unchanged from an automation trigger to the state changes it causes).
      `ProvenanceTier` itself is defined in `occupancy_engine.py`, not `provenance.py`, keeping the
      Phase 3 engine's zero-HA-import property intact — `provenance.py` depends on
      `occupancy_engine.py`, never the reverse. `signal_ingestion.py` now resolves provenance for
      every state change before constructing a `Signal`: `AUTOMATION_SUPPRESSED` is dropped
      entirely (never reaches the engine); the tier is otherwise attached to the `Signal` and — new
      this phase — the engine records each Area's `last_provenance` (`AreaState.last_provenance`),
      surfaced as a `provenance` attribute on both entity platforms (`SPEC.md` §6.8's explainability
      requirement). The old, dead `Signal.confidence: float` field (added in Phase 3, never actually
      read anywhere) was replaced by `provenance: ProvenanceTier` rather than kept alongside it. 15
      new tests (84 total): pure-function provenance-tier tests (id match, parent_id fallback,
      user_id, neither, automation-takes-priority-over-user_id), tracker lifecycle (remember/forget/
      bounded eviction), and end-to-end signal-ingestion tests proving an automation-fired context
      really does get suppressed and a user/contextless one really does get tagged correctly, plus
      the existing end-to-end entity test extended to check the new `provenance` attribute.
      `ruff`/`mypy` clean on the CI-equivalent commands.
- [x] **Phase 6 — Zone-presence fusion.** `SPEC.md` §6.7, implemented deliberately *without*
      touching the occupancy engine's per-Area counts — zone presence alone must never change them.
      New `config_flow.py` options flow (a plain `OptionsFlow` subclass, not the
      `SchemaConfigFlowHandler` framework — keeps the Phase 0 confirmation-only `ConfigFlow`
      untouched; see docs/DECISIONS.md) lets the user pick which `person`/`device_tracker` entities
      to fuse and which zones count as "near the house" (`SPEC.md` explicitly requires this be
      user-picked, never auto-detected by proximity). New `zone_fusion.py`, mirroring
      `provenance.py`'s split: `classify_zone_membership()` is a pure function classifying a
      `State` as `HOME`/`NEAR_HOUSE`/`AWAY`, verified against real `person`/`device_tracker`
      source (`components/person/__init__.py`, `components/device_tracker/legacy.py`) — notably,
      the legacy free-text zone-*name* state value turned out too unreliable to match against
      (could be any display string, not a stable slug), so near-house matching uses the modern
      `in_zones` attribute (a list of real zone entity ids) instead, a decision logged in
      DECISIONS.md along with its known limitation for legacy (non-`in_zones`) trackers.
      `ZoneFusion` (stateful) derives two behaviors, both surfaced as inspectable attributes/entities
      per `SPEC.md` §6.8, not by changing counting logic: `house_zone_corroboration()`
      (CORROBORATED/CONTRADICTED/UNKNOWN — attribute on `sensor.total_occupant_count`) and
      `is_pre_armed()` (a new house-level `binary_sensor.pre_armed`, push-updated, for automations
      that want to trigger faster once genuine egress activity follows near-house zone entry).
      "Help resolve ambiguous transits" and "decay a *specific* stale occupant's confidence" from
      `SPEC.md` §6.7's prose were deliberately **not** implemented at this per-token granularity —
      the engine tracks counts, not identified occupant tokens, so neither has a well-defined
      implementation without a deeper redesign; logged as a known, documented scope limit rather
      than guessed at. Options changes now trigger an automatic entry reload
      (`entry.add_update_listener`) so this is the one piece of runtime config that *does*
      live-update, unlike the topology snapshot (Phase 4's "or on next reload" precedent). 19 new
      tests (103 total): pure zone-membership classification, corroboration/pre-arm state machine
      behavior including window expiry, listener/unload lifecycle, and options-flow tests (form
      shown, selections saved, defaults reflect current options, end-to-end reload-picks-up-new-
      config). `ruff`/`mypy` clean on the CI-equivalent commands.
- [ ] **Phase 7 — WebSocket API + visual topology editor frontend.** `SPEC.md` §7.3. The largest
      single chunk of remaining engineering effort — expect this phase to take multiple sessions,
      each a bounded sub-slice (e.g. "read-only graph render," then "editable connectors," then
      "egress-point flagging UI," then "explainability inspector").
- [ ] **Phase 8 — Polish & packaging.** Full `docs/UX_GUIDELINES.md` pass, HACS submission
      readiness (`SPEC.md` §13 Q6 needs answering before this phase), documentation finalization.

## Open questions blocking specific phases

Resolved 2026-08-08 (see `docs/DECISIONS.md`): floors are display-only, household-size hint is
whole-house scope, near-house zones are user-picked. None of these block Phase 0–3 anymore.

Still open, from `SPEC.md` §13 — none block Phase 0–6:

- Multi-user topology-editing permissions — before Phase 7.
- Topology export/import as v1 or later — before Phase 8 service definitions.
- Full HACS default-repository bar vs. custom-repository-first — before Phase 8.

## Local testing environment (resolved 2026-08-08)

HA-integration tests (TESTING.md layer 2) run from a WSL2 (Ubuntu) venv at `.venv-wsl/` in the repo
root — gitignored, machine-local. Native Windows Python can't run
`pytest-homeassistant-custom-component` (`homeassistant.runner` unconditionally imports the
Unix-only `fcntl` module, and since the plugin autoloads this broke the whole pytest run, not just
HA-touching tests). WSL2 + Ubuntu is now installed; verified end-to-end with a real `hass`-fixture
test. See `docs/DECISIONS.md`'s 2026-08-08 entry for full detail.

To run tests locally: `wsl -d Ubuntu -- bash -c 'cd "<repo path>" && source .venv-wsl/bin/activate
&& python -m pytest tests/ -v'`. If `.venv-wsl/` doesn't exist yet on a given machine, create it
with `python3 -m venv .venv-wsl` (requires `python3.14-venv` installed via apt) and `pip install -r
requirements-test.txt`.

## Known tooling caveat (not a CI issue)

`mypy .` (whole repo at once) reports a "Source file found twice under different module names"
error for `custom_components/occupancy_tracker/registry_sync.py`, because `custom_components/` has
no `__init__.py` (deliberately — that's the HA convention) and mypy's module-root inference gets
ambiguous once both `custom_components/` and `tests/` are scanned together. CI is unaffected: it
runs `mypy custom_components` only (`.github/workflows/ci.yml`), which is unambiguous and passes
clean. If this needs fixing for local whole-repo runs later, the fix is `--explicit-package-bases`
(confirmed to resolve it) — not attempted as a pyproject.toml change yet since it's out of scope for
Phase 1 and CI isn't affected. See `docs/DECISIONS.md`.

## Open follow-up (not blocking)

Reconciliation currently surfaces dropped topology references only via `_LOGGER.warning` (see
`topology_store.py`'s `async_reconcile_and_save`). `SPEC.md` §5.3 just requires this not be silent
or crash-prone, which a log line satisfies for now — but a more visible mechanism (e.g. an HA
Repair/Issue via `issue_registry`, or a diagnostic entity attribute) would fit the product's
"explainability" goal (§7.3) better once there's a UI to show it in. Revisit at Phase 7 rather than
building it speculatively now.

## Open follow-up (not blocking) — Phase 6

`house_zone_corroboration()`/`is_pre_armed()` don't yet feed back into anything automated — they're
read-only, inspectable signals (an attribute and a binary sensor) that a *user's own* automations
can consume, matching what `SPEC.md` §6.7 actually asks for ("enabling a lighting/unlock automation
to trigger faster" describes a user-authored automation reacting to this integration's signal, not
this integration driving HA services itself). `SPEC.md` §6.7's "help resolve ambiguous transits" and
"decay a *specific* stale occupant's confidence" were explicitly not implemented — both need
per-token occupant identity the engine's count-based model doesn't have (see docs/DECISIONS.md).
Revisit only if a concrete per-token redesign is undertaken for other reasons; not worth building
just for this.

Near-house zone matching only works for trackers that populate the modern `in_zones` attribute
(companion app and other GPS-based `TrackerEntity`-based integrations do; some legacy/router-based
trackers don't). Documented in DECISIONS.md rather than worked around, since the primary spec'd use
case (companion app) is unaffected.

## Next action

Phase 7 — WebSocket API + visual topology editor frontend (`SPEC.md` §7.3). The largest remaining
chunk of engineering effort in this project — this is genuinely the piece nothing else can
substitute for, since it's the only way a user actually *sets* a topology (Connectors, egress-point
entity bindings, per-area entity selections) rather than an agent/test constructing one directly via
`TopologyStore.async_save()`. Per `docs/STATUS.md`'s existing phase-8 note and
`docs/AGENT_WORKFLOW.md` §2, treat this as multiple sessions, each a bounded sub-slice — a reasonable
first slice is the WebSocket API commands alone (read current Area/topology state, save topology
changes) with contract tests per `docs/TESTING.md` §1, *before* any frontend JS exists to call them.
