# Project Status

**Read this first, every session.** This file is more current than anyone's memory of past
sessions — keep it that way by updating it at the end of every session that changes anything.

## Current phase

**Phase 7 complete end-to-end and browser-verified. Phase 8 (polish & packaging) is now underway**:
a full requirements-conformance pass against `SPEC.md` plus a UX audit against `docs/UX_GUIDELINES.md`
(from an average-HA-user lens, at the project owner's explicit request) surfaced several real gaps —
missing services, tunables that existed in code but were never reachable from the UI at all (one of
them, a `config_panel_domain` side effect, made the *existing* options flow completely unreachable
too, not just the new tunables — see `docs/DECISIONS.md`'s 2026-08-09 entry), and some rough UX edges
— all now fixed; see the new Phase 8 entry below for the full list. `docs/SPEC.md` (functional) and
`docs/ARCHITECTURE.md` (technical) are the agreed target design. The old prototype under
`custom_components/occupancy_tracker/` predates this spec, does not conform to it, and should be
treated as reference-only for "what not to do" (see `docs/DECISIONS.md`'s 2026-08-08 entry) — not as
a starting point to patch. **The current `custom_components/occupancy_tracker/` is the new,
spec-conformant scaffold**, and is now genuinely usable end-to-end against a real house: registry
sync (Phase 1) → topology store (Phase 2) → occupancy engine (Phase 3) → signal ingestion + entity
platforms (Phase 4) → provenance resolution (Phase 5) → zone-presence fusion (Phase 6) → topology
editor websocket API (Phase 7a) → a real, in-browser topology panel with pan/zoom,
draggable+persisted+grid-snapped Area/Outside layout, floor-aware auto-arrange, a permanent sidebar
entry point, editable Connectors, editable access points, a live-refreshing explainability inspector,
an editable per-area entity-selection checklist (Phase 7b-i through 7b-v), and now (Phase 8) manual
occupant-count override/topology backup services, user-tunable confidence windows, and entity
friendly names throughout the panel all wire together, verified both by real integration tests **and
by the project owner actually using it in a browser this session** (see "Local dev Home Assistant
instance" below). Everything §5.2/§7.3 describes — picking which entities count as occupancy
evidence, drawing/flagging topology graphically, and inspecting live engine state per Area — works
and is confirmed working, not just unit-tested. A real house with real Areas and real sensors can now
be fully configured and tuned through the panel and its options flow alone, with no house-specific
data hardcoded and no manual `.storage` editing required, matching `SPEC.md` §7.4.

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
- [x] **Phase 7 — WebSocket API + visual topology editor frontend.** `SPEC.md` §7.3. The largest
      single chunk of remaining engineering effort — being taken as multiple bounded sub-slices.
  - [x] **Phase 7a — WebSocket API.** New `websocket_api.py`: two commands,
        `occupancy_tracker/topology/get` (read-only; returns the live house shape — areas, floors,
        entities — plus the current saved topology) and `occupancy_tracker/topology/save`
        (`require_admin`; validates a full topology replacement against the live house shape,
        rejecting — not silently dropping — any reference to an Area/entity that doesn't exist,
        a connector to itself, or a duplicate `connector_id`, then persists it and triggers
        `hass.config_entries.async_reload()`). Both commands take `entry_id` and resolve it via
        `hass.config_entries.async_get_known_entry()`, checked against this integration's `DOMAIN`
        and `ConfigEntryState.LOADED` before touching `runtime_data` — verified deliberately
        generic (not hardcoded to "the one entry") even though `single_config_entry: true` makes
        that the only case today. Registering commands requires `hass.data[websocket_api.DOMAIN]`
        to exist first, so `manifest.json` now lists `"websocket_api"` as a dependency — verified
        from `setup.py`'s `_async_process_dependencies`, which HA's own `config_entries.py` calls
        for a domain's first setup even for a config-entry-only (no top-level YAML) integration
        like this one. The save command reloads the entry so a saved topology takes effect right
        away rather than requiring a manual reload — the same "next reload" delivery mechanism the
        Phase 6 options flow already established, applied to a second trigger. `topology_store.py`'s
        private `_to_stored`/`_from_stored` helpers were promoted to public `topology_to_dict`/
        `topology_from_dict` so the websocket API reuses the exact same JSON shape as `Store`
        persistence rather than re-deriving its own (both a `TopologyDict` and a websocket payload
        are the same plain, JSON-safe structure). Under `mypy --strict`, importing `ActiveConnection`/
        `ERR_NOT_FOUND`/`ERR_INVALID_FORMAT`/`websocket_command`/`require_admin`/`async_response` via
        the aggregating `homeassistant.components.websocket_api` package triggers "does not explicitly
        export attribute" (`homeassistant/components/websocket_api/__init__.py` re-exports them via
        `# noqa: F401` imports without an `__all__`) — fixed by importing each directly from the
        submodule that actually defines it (`.connection`, `.const`, `.decorators`), verified against
        source, not guessed. 5 new tests (108 total) via `hass_ws_client`: get returns house shape +
        topology; unknown `entry_id` → `not_found`; save persists, reloads, and is reflected in the
        reloaded `runtime_data`; save rejects an unknown-area reference (topology left untouched);
        save is rejected for a non-admin user. `ruff`/`mypy` clean on the CI-equivalent commands.
  - [x] **Phase 7b — Frontend panel.** Taken as its own bounded sub-slices, all now complete.
    - [x] **Phase 7b-i — Panel registration + interactive area layout.** New `panel.py`: serves
          `custom_components/occupancy_tracker/www/` as a static path
          (`hass.http.async_register_static_paths`) and registers a `panel_custom` panel
          (`frontend_url_path="occupancy_tracker"`, `require_admin=True`,
          `config_panel_domain="occupancy_tracker"` — the latter is what makes Settings → Devices &
          Services → Occupancy Tracker → Configure open this panel instead of the options-flow
          form). `panel_custom.async_register_panel()` raises if the same `frontend_url_path` is
          registered twice (verified from `frontend/__init__.py`'s `async_register_built_in_panel`,
          which only tolerates a repeat call with `update=True` — a parameter the `panel_custom`
          wrapper never exposes) and `async_setup_entry` re-runs on every reload — including the one
          Phase 7a's topology save triggers — so `panel.py` guards itself with a `hass.data`
          sentinel, registering at most once per HA runtime. `manifest.json` gained `"http"` and
          `"panel_custom"` as direct dependencies (the latter transitively pulls in `"frontend"`,
          verified from `panel_custom`'s own manifest). The frontend itself
          (`www/topology-panel.js`) is a `LitElement` custom element — verified against HA's
          developer docs (`developers.home-assistant.io/docs/frontend/custom-ui/creating-custom-panels/`),
          fetched live this session since there's no Python source to check the `hass`/`narrow`/
          `panel` property contract against — that resolves this integration's one config entry via
          the core `config_entries/get` websocket command (rather than baking `entry_id` into the
          panel's static registration config, which would go stale if the entry were ever removed
          and re-added within the same HA runtime, since the panel-registration guard above means it
          wouldn't be re-registered with a fresh `entry_id`), then calls Phase 7a's
          `occupancy_tracker/topology/get` and renders Areas as an SVG node graph with Connector
          edges, a dashed edge to a synthesized "Outside" node for each egress point, and a
          click-to-inspect detail panel (bound entities, egress status, selected entities) — loading/
          error/empty states written deliberately per `docs/UX_GUIDELINES.md` §5, not raw exception
          text. Ships a vendored, self-contained `lit@3.3.3` build (`www/vendor/lit-core.min.js`,
          built via `esbuild --bundle --format=esm`, verified to contain zero remaining external
          imports) rather than loading Lit from a CDN at runtime — a deliberate choice (see
          `docs/DECISIONS.md`) so the panel works on LAN-only/offline HA installs, at the cost of a
          vendored file to keep in sync on future Lit updates. Adding `panel_custom`→`frontend` to
          the dependency chain surfaced a real test-environment gap: `frontend`'s `async_setup`
          imports the separate `home-assistant-frontend` PyPI package (declared in its own
          `manifest.json` `requirements`, not auto-installed by `pytest-homeassistant-custom-component`),
          which broke every test touching config-entry setup until installed — now pinned in
          `requirements-test.txt` with a note on why.

          **Layout is fully interactive, not just read-only render** (added after the project owner
          reviewed the first pass live and asked for it): `TopologyData` gained `area_positions`
          (topology store schema 1.1 → 1.2, with a migration), a pure display field the engine never
          reads. The panel supports pan (drag background) and zoom (scroll, cursor-anchored), dragging
          any Area node with optional snap-to-grid (subtle dot grid, on by default, toggleable, scales
          naturally with zoom since it's drawn in the same SVG coordinate space as everything else),
          and a one-click "Auto-arrange" that groups Areas by floor — ordered by the floor's own
          `level` (new field on `FloorSnapshot`/`_house_shape_json`, verified against
          `helpers/floor_registry.py`'s real `FloorEntry.level`) — into a deterministic, overlap-free
          grid (verified with a standalone Node script checking pairwise node distances, not just
          eyeballed). Every drag-end and auto-arrange saves via `occupancy_tracker/topology/save`;
          `websocket_save_topology` now skips its `async_reload()` when only `area_positions` changed
          (see `docs/DECISIONS.md`), so repositioning a room doesn't tear down/rebuild the whole
          integration for a field nothing downstream reads. Connector-drawing and egress-flagging are
          still not built — the graph only shows *existing* Connectors/egress points (there are none
          yet in a fresh install), so it's still not usable end-to-end, but the layout/navigation
          surface itself now is.

          **Verified live in a real (non-test-harness) HA instance this session** — see "Local dev
          Home Assistant instance" below for how that's set up, since it wasn't trivial (several
          environment issues fixed along the way, documented there so they don't need re-discovering).
          Real bugs the project owner found by actually using it, now fixed: (1) dragging didn't
          persist across a page refresh — root cause was the running dev `hass` process still
          executing the *pre-`area_positions`* Python backend, since Python modules load once at
          process start unlike the JS which is served fresh from disk every request; fixed by
          restarting the dev instance, and now called out explicitly below so this isn't
          re-discovered the hard way next session. (2) Zoom felt like it was also panning — root cause
          was the graph viewport's CSS having a different aspect ratio than the computed SVG
          `viewBox`, which made the SVG letterbox (`preserveAspectRatio="xMidYMid meet"`'s default
          behavior on a ratio mismatch) and silently broke the screen-to-canvas coordinate math every
          pan/zoom/drag handler depends on — fixed by locking both to the same fixed ratio (see
          `docs/DECISIONS.md`'s 2026-08-09 entry for the full explanation). 6 new/updated backend tests
          (115 total): `area_positions` round-trips through save/load and the websocket API,
          reconcile drops positions for removed areas, the pre-1.2→1.2 migration default, a
          position-only save leaves `runtime_data` untouched (no reload) while a structural save
          replaces it (does reload), and a save rejects a position referencing an unknown area.
          `ruff`/`mypy` clean on the CI-equivalent commands. Connector-drawing/egress-flagging UI
          (`SPEC.md` §7.3's actual "draw a line between two rooms" interaction) is still not built —
          that's Phase 7b-ii next.
    - [x] **Phase 7b-ii — Editable connectors.** A new "Draw connector" toolbar mode: click one Area
          node then another to create a Connector (live dashed preview line follows the pointer
          in between; click the same node again or press Esc to cancel), calling the existing
          `occupancy_tracker/topology/save` command — no backend changes needed, Phase 7a's schema/
          validation already fully supported connectors. Removing one is hover-or-tap-to-reveal a
          small delete control at the edge's midpoint, plus a keyboard path (Tab to the edge,
          Enter/Delete/Backspace) per `docs/UX_GUIDELINES.md` §6. Click-sequence rather than
          drag-node-to-node was a deliberate choice (see `docs/DECISIONS.md`) — node-drag already
          means "reposition" in this panel, so a second drag-based meaning would be ambiguous, and a
          click sequence works identically on touch and is straightforwardly keyboard-accessible.
          Duplicate connectors between the same pair of areas are silently skipped rather than
          rejected with an error (there's no meaningful reason to have two identical edges). All
          saves reuse the existing optimistic-update pattern node-dragging already established.
          **Verified live in the project owner's browser this session**, which also surfaced three
          real issues beyond the connector feature itself, now fixed (see `docs/DECISIONS.md`'s
          2026-08-09 entries): (1) the panel had no discoverable entry point — it now has a
          permanent sidebar item, not just a Devices & Services → Configure path; (2) connector/
          egress lines used a generic grey (`--divider-color`) instead of relating visually to the
          Area nodes' own color — now a translucent tint of `--primary-color`; (3) grid-snapped
          connectors visually didn't line up with the background dot grid — root cause was two
          separate bugs (the grid dot was drawn 1 unit off its tile's corner, and `AUTO_LAYOUT_CELL`
          not being a multiple of `GRID_SIZE` meant auto-arranged nodes were never actually
          grid-aligned even before this slice). No new Python beyond `panel.py`'s two new
          `async_register_panel()` kwargs (`sidebar_title`/`sidebar_icon`) — `ruff`/`mypy` clean, all
          115 existing tests still pass (`test_panel.py` failed once in isolation due to an
          unrelated test-ordering artifact — passes cleanly as part of the full suite, which is the
          authoritative signal). No Python-testable surface for the JS itself, per
          `docs/TESTING.md` — verification was manual in-browser, per `CLAUDE.md`'s UI rule.
    - [x] **Phase 7b-iii — Access-point flagging UI.** (Internally still `EgressPoint`/
          `egress_points` — the rename to "access point" is UI-copy-only this session, see
          `docs/DECISIONS.md`.) No graph-wide toolbar mode needed, unlike connectors — it's a
          per-area concern, so it lives entirely in the existing click-to-inspect detail panel: a
          checklist of that Area's entities, where checking one immediately makes the Area an access
          point (an Area *is* one exactly when it has a non-empty crossing-entity list, matching the
          backend's own validation — no separate on/off flag to keep in sync). Unchecking the last
          one removes it. No backend changes needed for this part — Phase 7a's schema/validation
          already fully supported it. Fixed two more real bugs surfaced by live testing (see
          `docs/DECISIONS.md`): the synthesized "Outside" node couldn't be repositioned and was
          clipped by "Fit view", both because its position was purely computed at render time and
          never actually stored anywhere — it's now a real, draggable, persisted node like any Area,
          which needed a small backend addition (`outside_position`, storage schema 1.2→1.3 with a
          migration, a new websocket field, `ruff`/`mypy` clean, 2 new tests, all 117 tests passing).
          **Verified live in the project owner's browser this session.**
    - [x] **Phase 7b-iv — Explainability inspector.** Two new read-oriented websocket commands in
          `websocket_api.py`: `occupancy_tracker/engine/get_state` (one-shot snapshot) and
          `occupancy_tracker/engine/subscribe_updates` (push updates), both serializing
          `OccupancyEngine` state via a new `_engine_state_json()` helper — per-Area occupant count,
          `StateQuality`/`ProvenanceTier` (by `.name`, matching the existing entity-attribute
          convention), `last_confirmed` (ISO, via `dt_util`), and any pending transit (source area,
          other side, deadline). `OccupancyEngine` gained a public `graph` property so the websocket
          layer can resolve a pending transit's other-side Area without duplicating graph-construction
          logic. The detail panel renders this as a quality chip, occupant count, "last confirmed"
          relative time (`Intl.RelativeTimeFormat`), and pending-transit reasoning in plain language.
          **Live-refresh, not poll-on-select:** the panel subscribes once per panel lifetime (not
          per-Area-selection) via `hass.connection.subscribeMessage()` — verified against real
          `home-assistant-js-websocket`/HA-core source, since this integration had no existing
          subscription-pattern example to copy. `subscribe_updates` registers cleanup in
          `connection.subscriptions` *and* via `entry.async_on_unload()` (idempotent, guarded by a
          `nonlocal removed` flag) so it tears down correctly whichever happens first — a browser
          disconnect or a config-entry reload (the websocket connection outlives any single reload,
          so only the latter actually exercises `async_on_unload`). The panel also
          resubscribes unconditionally after every topology save, since a save can trigger a reload
          that replaces the engine instance out from under an existing subscription.
          **Project-owner live-testing feedback this session**: the detail panel's chips didn't
          refresh when a device was toggled while a room was already selected — it only updated on
          unselect/reselect, because the original implementation fetched state once per selection
          rather than subscribing. Fixed by the live-subscription mechanism above; **confirmed working
          in the browser** (toggling a room's `input_boolean` while its detail panel is open now
          updates the quality chip/occupant count/pending-transit text immediately, no reselect
          needed). To support this and future live testing, also added real per-room test fixtures to
          the dev instance (`docs/STATUS.md`'s dev-instance section below): a motion `input_boolean`
          and a light `input_boolean` for each of the 9 internal rooms, each assigned to its Area and
          wired into that Area's entity selection, plus a single YAML dashboard grouping them by room
          alongside the house-level total/pre-armed sensors — replacing the single fake
          `input_boolean.egress_test` (which the project owner caught was registry-only and
          untoggleable) with genuine, real, toggleable entities. 5 new tests (122 total):
          `get_state`'s area/pending-transit/unknown-entry-id cases, and the subscribe command's
          push-on-signal and stops-after-reload lifecycle cases. `ruff`/`mypy` clean on the
          CI-equivalent commands.
    - [x] **Phase 7b-v — Per-area entity-selection UI (`SPEC.md` §5.2).** The gap flagged as an
          "Open follow-up" after Phase 7b-iii/iv: `area_entity_selections` was fully wired end-to-end
          on the backend since Phase 4 (`signal_ingestion.py` already subscribed to whatever was in
          it), but nothing in the panel let a user populate it. Fixed with no backend changes at all
          — purely a frontend addition reusing the exact same pattern Phase 7b-iii's access-point
          checklist established: the detail panel's previously read-only "Selected entities" list is
          now an editable "Activity evidence" checklist over that Area's `entity_ids`
          (`_setAreaEntitySelections`/`_toggleAreaEntitySelection`, mirroring
          `_setEgressEntities`/`_toggleEgressEntity`), saved via the existing
          `occupancy_tracker/topology/save` command. Deliberately independent of the access-point
          checklist rather than mutually exclusive with it — the backend places no exclusivity
          constraint between the two lists (an entity, e.g. a door sensor, can legitimately be both
          an access-point crossing sensor and general activity evidence for its room), so the UI
          doesn't invent one either. No new tests (no Python surface changed); `ruff`/`mypy` clean,
          all 122 existing tests still pass. **Verified live in the project owner's browser this
          session**: checked entities persist across refresh, and toggling a now-selected
          `input_boolean` actually produces occupant-count/quality-chip changes for that room where
          it previously wouldn't have. This was the last missing piece for a real house to be
          configurable through the panel alone — Phase 7 is now complete end-to-end.
- [~] **Phase 8 — Polish & packaging.** Kicked off with a full requirements-conformance pass against
      `SPEC.md` and a UX audit against `docs/UX_GUIDELINES.md` (project owner's explicit request:
      "scrutinised for usability, relevance and simplicity" from an average-HA-user's point of view,
      and the whole product checked against the original requirements). Found and fixed real gaps:
      - **Missing services (SPEC.md §8), now built**: `occupancy_tracker.set_occupant_count` (an
        entity service on the per-Area occupant-count sensor, via
        `entity_platform.async_register_entity_service` — verified pattern, mirrors core
        `utility_meter`'s own `calibrate` service) for manually correcting a wrong inference, and
        `occupancy_tracker.export_topology`/`import_topology` (response-data and
        schema-validated-input services respectively) for backup/restore or copying a topology
        between installs. Import reuses the exact same validate/save/reload logic the websocket
        save command uses — both now call a single `topology_store.async_replace_topology()`
        rather than duplicating that logic (the old `websocket_api._topology_validation_errors`
        was promoted to `topology_store.validate_topology()` in the same move).
      - **Missing "typical household size" confidence hint (SPEC.md §6.4/§7.2), now built**:
        `EngineConfig.household_size_hint`, deliberately *not* wired into any count-inference
        branch (SPEC.md is explicit this must never cap a count) — surfaced purely as a new
        `exceeds_household_size_hint` attribute on `sensor.total_occupant_count`.
      - **Tunables that existed in code but had no UI path at all, now exposed via the options
        flow**: `EngineConfig.transit_confirmation_window`/`confirmed_freshness_window` and
        `ZoneFusionConfig.pre_arm_window`, as `selector.DurationSelector` fields (verified: it
        validates but returns the submitted dict as-is, not zero-filled — `__init__.py`'s
        `_duration_option()` converts via `timedelta(**value)`), plus the household-size hint as a
        `selector.NumberSelector` with no forced default (unset is a real, distinct state).
      - **A real, previously-undiscovered bug**: `panel.py`'s `config_panel_domain` (added Phase
        7b-i) made the options flow completely unreachable from the UI — not just for these new
        tunables, but for the zone-fusion settings that had been there since Phase 6. Removed; see
        `docs/DECISIONS.md`'s 2026-08-09 entry for the full verification trail (this needed tracing
        into the actual frontend source, not assumed).
      - **A second, more severe discoverability bug found via live testing**: even after the fix
        above, the project owner still couldn't find "Occupancy Tracker" under Settings → Devices &
        Services at all — not a gear-icon problem, the integration itself wasn't listed. Root cause:
        `manifest.json`'s `integration_type: "helper"` (set at Phase 0, never revisited) routes a
        config entry to a **separate "Helpers" tab**, not the main "Integrations" tab everything in
        this project's own docs says to look at — verified from the frontend's actual websocket
        subscription filters (`ha-config-integrations.ts` vs. `ha-config-helpers.ts`), which are
        mutually exclusive by `integration_type`. Changed to `"hub"` (HA's own default for an
        unspecified `integration_type`, and a better fit than "helper" for something this
        multi-entity/panel-driven in the first place) — see `docs/DECISIONS.md`. **This means every
        session's own instructions to find it under Devices & Services → Integrations were subtly
        wrong the entire time**, and it's worth double-checking there isn't a third variant of this
        same class of bug still lurking (anything else keyed off `integration_type`/manifest
        metadata that was set once at Phase 0 and never re-validated against how the product
        actually turned out).
      - **Entity friendly names**: `registry_sync.py`'s `EntitySnapshot` gained a `name` field via
        `entity_registry.async_get_full_entity_name` (verified: the same display-name resolution
        HA's own UI uses), so the topology panel's entity checklists show "Kitchen Motion" instead
        of `binary_sensor.kitchen_motion` — SPEC.md §5.2's own examples ("this motion sensor")
        implied this, and showing raw entity ids was the single biggest "looks like a dev tool, not
        a Home Assistant feature" finding from the UX audit.
      - **Content/metadata**: `README.md`/`CHANGELOG.md` rewritten (both were still literal Phase-0
        placeholder text describing a nonexistent product); `manifest.json`'s `codeowners`/
        `documentation`/`issue_tracker` replaced with an obvious `TODO-set-your-github-username`
        placeholder (previously a stale, unrelated real-looking GitHub handle) — **project owner
        confirmed they'll set up a real GitHub repo before HACS submission, not now; this is a
        tracked action item, see "Open follow-up" below**; a stale docstring in
        `websocket_api.py`'s `_engine_state_json` (claimed "not a push subscription," no longer true
        since this session's earlier live-refresh fix) corrected.
      - **Investigated, deliberately not changed**: swapping the topology panel's raw
        `<input type="checkbox">` checklists for HA's native `ha-checkbox` — traced to the exact
        installed frontend version (`20260729.6`) and found it now wraps a "Web Awesome" web
        component whose event/property contract couldn't be verified from available source (a
        separate package, not in the `frontend` repo). Left as-is per this project's hard rule
        against proceeding on an unverified API guess — see `docs/DECISIONS.md`.
      - **Noted, not yet built** (real UX ideas from the audit, not requirements gaps): no
        setup-friction relief for entity selection (smart pre-checked defaults for likely-relevant
        entities), no transition when the detail panel opens/closes, color-contrast of the quality
        chips not spot-checked in both themes. Not blocking — logged for a future polish pass.
      36 new/updated tests (142 total: engine override/hint tests, options-flow tunable tests,
      `__init__.py` wiring tests, entity-attribute test, a new `tests/test_services.py`).
      `ruff`/`mypy` clean. This batch was browser-verified by the project owner, who then raised four
      more real findings from actually using it — all now fixed, in this same Phase 8 pass:
      - **Options-flow/service language was too technical**, per direct project-owner feedback ("the
        language needs to assume the user doesn't have an understanding of the code or technical
        methods... dumb it down to simple concepts"). `translations/en.json` fully rewritten in plain,
        cause-and-effect language throughout — see `docs/DECISIONS.md`'s entry for an example.
      - **No navigation path from Settings → Devices & Services back to the topology panel**, once
        `config_panel_domain` was removed (the fix above): the gear icon now only opens the options
        form, with nothing linking back to the panel except the sidebar entry. Fixed by registering a
        virtual `DeviceEntryType.SERVICE` device per entry with a `homeassistant://occupancy_tracker`
        `configuration_url` (its "Visit" link opens the panel without a real network request — a real,
        verified HA capability, see `docs/DECISIONS.md`), with the two house-level entities now
        grouped under it.
      - **Per-Area entity clutter**: since every HA Area becomes a topology node with no "opt out,"
        a house with many rooms produced a same-sized sensor/binary_sensor pair for every single one,
        regardless of whether the user had configured anything for most of them — flagged directly by
        the project owner as unnecessary and worth cleaning up. Fixed with a new
        `topology_store.active_area_ids()` (an Area counts as active once it has at least one
        activity-evidence entity selected, or is an access point) that `sensor.py`/`binary_sensor.py`
        now filter per-Area entity creation against, plus a new `__init__.py`
        `_prune_inactive_area_entities()` that actively removes a previously-registered per-Area
        entity once its Area drops out of the active set (not just leaves it unavailable). The engine's
        own internal graph is deliberately untouched by this — it still models every Area, since a
        sensor-less Area can be a legitimate transit pass-through node (SPEC.md §5.1); only which HA
        *entities* get created is affected. The topology panel gained a matching `_isAreaActive()`
        check: an inactive Area's node is now visibly dimmed in the graph, and its detail panel shows
        an explanatory notice instead of an empty checklist. See `docs/DECISIONS.md` for the full
        design writeup.
      - **A full UI/UX pass on the topology panel's main card**, triggered by an annotated screenshot
        from the project owner: the graph legend was mixed into the top explainer paragraph instead of
        living with the caption at the bottom (now moved into a new `.graph-footer` block); the
        caption was center-aligned against an otherwise left-aligned card (fixed); and all card copy —
        quality/provenance chip labels, section headings, empty/loading/error states, checklist
        descriptions — rewritten in the same plain language as the options-flow work above.
      12 new/updated tests since the 142 above (154 total): a device-registration test, three new
      `test_init.py` tests replacing the old zero-topology assumption (house-level entities always
      exist; per-Area entities appear once something's selected; a full deselect-then-reload actually
      removes them from the entity registry, not just from state), two existing `test_entities.py`
      tests updated to select-then-reload before asserting on a per-Area entity, two
      `test_services.py` tests updated to use a tracked Area, and four new `test_topology_store.py`
      tests for `active_area_ids()` itself. `ruff`/`mypy`/full `pytest` all clean; dev instance
      restarted and confirmed a clean boot (`Home Assistant initialized in 6.99s`, no
      `occupancy_tracker` errors). **Browser-verified by the project owner ("all ok")**: device →
      panel navigation, per-Area entity pruning (both directions), the panel's legend/alignment/
      language pass, and the options-flow plain-language rewrite all confirmed working live.
      - **A brand icon**: `custom_components/occupancy_tracker/brand/icon.png` (+ `icon@2x.png`) — a
        simple generated placeholder (house glyph + a small "presence" dot), swappable later. Uses
        HA 2026.3+'s local-brand-image mechanism (a `brand/` folder inside the integration, no
        `manifest.json` change), verified from the HA developer blog rather than assumed, since
        custom integrations can't get into the official `home-assistant/brands` repo the way core
        integrations can.
      - **Connector/egress lines redrawn from node edge to node edge**, not center to center (a new
        `pointTowardsEdge()` helper), and each active Area's node now shows its **live occupant count**
        as a label inside the circle, sourced from the same engine-state subscription the detail panel
        already uses. Both from direct project-owner feedback while testing (a connector line was
        visibly cutting through a room's circle; a count-at-a-glance was requested as lower-friction
        than opening a room's detail panel). See `docs/DECISIONS.md` for the full design writeup,
        including why trimming the line was the right fix rather than just patching the one visibly
        broken case (an inactive node's dimming).
      - **A real regression, found and fixed the same session**: the occupant-count/line-trim change
        above shipped with a literal backtick inside a comment inside `static styles = css\`...\``
        (one giant JS template literal — a backtick anywhere inside it, even in what reads as a "CSS
        comment," terminates it early). This blanked the entire panel — confirmed from HA's own
        server-side capture of the browser's console error, not guessed. Fixed, and — significant for
        future sessions — a **real Node.js install was found reachable from WSL** at
        `/mnt/c/Program Files/nodejs/node.exe` (previously believed unavailable; see
        `docs/DECISIONS.md`), so `node --check <file>` can now actually verify frontend JS syntax
        before it ships, instead of relying on manual re-reading alone.
      - **Two extra real per-room test fixtures** added to the dev instance for exercising access-point
        and near-house-zone flows: `input_boolean.kitchen_door` and
        `input_boolean.entrance_hallway_door`, assigned to their Areas the same way the existing
        motion/light fixtures are.
      - **The synthetic "Outside" graph node and its converging dashed edges were removed entirely**,
        per project-owner feedback (with screenshots) that a house with more than one or two access
        points produced messy crossing lines to one arbitrarily-positioned shared node, and it didn't
        add anything the existing per-node dashed ring didn't already convey. Access points are now
        communicated purely by that ring; the engine's own internal `OUTSIDE` transit-inference
        concept is unaffected (it was always derived independently from `egress_points`, never from
        this UI node). See `docs/DECISIONS.md` for the full reasoning and what was and wasn't touched
        on the backend (nothing — the save schema's `outside_position` field already accepted `null`).

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

## Local dev Home Assistant instance (Phase 7b browser verification)

Unlike the pytest harness above, actually *looking at* the frontend panel needs a real, running HA
server, not a test fixture. One exists, set up this session, running in the same WSL2 Ubuntu as the
test venv:

- **Config dir:** `~/occupancy-tracker-dev-config` (WSL-side, i.e. `/home/<user>/...` — deliberately
  *not* under the Windows-mounted repo path, since that path containing a space broke HA's on-demand
  package installer, see below). `custom_components/occupancy_tracker` inside it is a **symlink**
  into this repo's checkout, so code edits here are picked up without copying anything — except
  Python changes need a process restart (see the caveat below); JS under `www/` is served fresh from
  disk on every request, no restart needed for that.
- **Start/restart:** kill any existing `.venv-wsl/bin/hass` process (`ps aux | grep '[b]in/hass'`,
  then `kill <pid>` — don't use `pkill -f` with a pattern that would match its own invoking command
  line, e.g. a pattern containing `bin/hass`, or it kills itself), then from the repo root:
  `source .venv-wsl/bin/activate && hass --config ~/occupancy-tracker-dev-config`. Reachable at
  `http://<WSL IP>:8123` from Windows (`hostname -I` inside WSL for the IP; plain `localhost:8123`
  didn't forward correctly in this session's network config, though it may on other setups — try it
  first). Onboarding (create an owner account) only needs doing once; progress is saved in
  `.storage/onboarding` in the config dir.
- **⚠️ Restart after any Python change**, not just on first boot — this bit us once already (a
  drag-to-reposition silently failed to persist because the running process was still executing the
  pre-`area_positions` backend). There's no auto-reload for backend code the way there is for the
  frontend JS.
- **configuration.yaml is deliberately minimal** (`frontend:`, `config:`, `person:` — not
  `default_config:`). Reason: this WSL venv lives under a Windows path containing a space
  ("Occupancy Sensor"), which breaks HA's on-demand `uv`-based package installer (it splits the path
  on the space and gets a "file not found" for every package it tries to lazily install) — HA's own
  bootstrap unconditionally tries to load a large fixed set of integrations
  (`homeassistant.bootstrap.DEFAULT_INTEGRATIONS`/`STAGE_1_INTEGRATIONS`) regardless of
  `configuration.yaml` content, so this doesn't fully dodge the problem, but it avoids pulling in
  `default_config:`'s own large additional set on top of that. The packages that *do* need to load
  successfully (`_base_components()` in `helpers/service.py` imports `ai_task`, `camera`,
  `assist_satellite`, etc. unconditionally, and a failed import there breaks any voluptuous schema
  validation using a `supported_feature`/`supported_color_modes` key) were pre-installed by hand into
  `.venv-wsl` instead of relying on the broken on-demand installer:
  `pyotp==2.9.0 PyQRCode==1.2.1 ha-ffmpeg==3.2.2 hassil==3.11.0 home-assistant-intents==2026.7.30
  PyTurboJPEG==1.8.3 av==17.0.1 mutagen==1.48.1 pymicro-vad==1.0.1 pyspeex-noise==1.0.2`. The last two
  needed a real C++ toolchain + Python headers to build from source (`sudo apt install -y
  build-essential python3.14-dev` — already done on this machine). None of this is a project defect;
  it's entirely a byproduct of the dev machine's checkout path containing a space, and is irrelevant
  to a real end-user HACS install (which doesn't hit HA's on-demand installer for *this* integration's
  own dependencies, since `manifest.json` only requires already-published, already-compatible
  packages `home-assistant-frontend`/nothing exotic).
- A handful of *other* integrations (`radio_frequency`, `infrared`, `ffmpeg`'s actual ffmpeg binary,
  `libturbojpeg`) still fail to fully initialize in this dev instance and log errors on every boot —
  confirmed harmless and unrelated to Occupancy Tracker (traced each one), left as-is rather than
  chased further.
- **⚠️ Boot sometimes hangs silently, and default log verbosity gives zero visibility into it —
  always start with `-v` and confirm `Home Assistant initialized in Xs` before assuming anything is
  broken.** Discovered this session (cost significant time — recorded here so it isn't re-chased from
  scratch): at default log verbosity, `hass --config ...` produces almost no "Setting up X" lines at
  all (those are DEBUG-level, not INFO), so long stretches of log silence are *normal* and are not
  evidence of a hang either way. Separately, the process has genuinely hung silently and
  reproducibly partway through boot multiple times this session (always with plenty of free
  memory/disk, a working DNS/network, and a *correct* monotonic clock — all checked and ruled out),
  then booted cleanly in ~6s on the very next attempt with no code or environment change in between —
  so treat it as a known-intermittent WSL2/dev-instance flake, not a code regression, unless it
  reproduces after a real code change and disappears when that change is reverted. A `py-spy dump`
  of a genuinely hung process is **not useful** for distinguishing "hung" from "healthy and idle" —
  both show every thread parked in a normal idle wait (`select()` on the main thread, executor
  threads blocked on their work queue) with zero CPU, because a suspended asyncio Task waiting on a
  Future that will never resolve occupies no OS thread and so is invisible to a native stack dump
  either way. The one reliable signal is the log line `homeassistant.bootstrap: Home Assistant
  initialized in Xs` (only printed at INFO level, so `-v` isn't strictly required for *this* one
  line, but is needed for everything else that would help triage a real hang) — if that line never
  appears after a couple of minutes, kill (`pkill -f 'bin/hass'`) and just retry before assuming
  something is actually broken.

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

## Resolved follow-up — per-area entity-selection UI (was: discovered 2026-08-09, fixed 2026-08-09)

**Previously:** no UI existed to pick which entities count as activity evidence for a room
(`SPEC.md` §5.2). `area_entity_selections` was fully wired end-to-end on the backend since Phase 4,
but nothing in the panel let a user populate it, so a real house had zero real signal anywhere.
**Now fixed** by Phase 7b-v above — see that entry for what shipped. Leaving this note rather than
deleting it outright, since it explains why Phase 7 briefly showed as "done but not really usable"
in two consecutive sessions' status text.

## Open follow-up (action item, not blocking further phase work) — added 2026-08-09

**`manifest.json`'s `codeowners`/`documentation`/`issue_tracker` are an obvious
`TODO-set-your-github-username` placeholder**, replacing a previous *real-looking* but wrong GitHub
handle (`@yme207`) found during the Phase 8 requirements audit. Project owner explicitly chose to
set up a real GitHub repo later rather than provide details now — **must be fixed before any HACS
submission** (this will also need updating in `README.md`'s clone instructions once a real repo
exists). Revisit alongside `SPEC.md` §13 Q3 (HACS submission bar).

## Next action

Both Phase 8 batches (requirements/UX audit fixes, then the live-testing-driven navigation/clutter/
language fixes) are now built, tested, and browser-verified end to end. Remaining before Phase 8 can
be considered done:

1. The still-open `docs/UX_GUIDELINES.md` review items noted earlier (light/dark theme spot-checks on
   the quality chips, the noted-but-not-built UX ideas: setup-friction relief for entity selection,
   a detail-panel open/close transition).
2. `SPEC.md` §13 Q3's HACS submission bar decision (custom-repository-first vs. pursuing the HACS
   default-repository listing).
3. The `manifest.json` `codeowners`/`documentation`/`issue_tracker` placeholder (see "Open follow-up"
   above) — blocked on the project owner setting up a real GitHub repo, not forgotten.
4. A first real end-to-end smoke test against actual house sensors, not just `input_boolean`
   fixtures — never tried yet, worth doing before calling Phase 8 done.

The browser-verification loop this session (project owner testing → real bug found → fix → retest)
worked well through six slices in a row now and is worth continuing — see "Local dev Home Assistant
instance" above for the instance to reuse, **and read its boot-hang note before assuming a stuck
instance means broken code** (this cost real time earlier in the session before being understood as
an intermittent environment flake, not a regression). Check whether it's still running first:
`ps aux | grep '[b]in/hass'` in WSL; restart per the instructions there if not (note: `pkill -f
'bin/hass'` can match its own invocation and appear to fail with a nonzero exit even though it
successfully killed the target — always verify with a follow-up `ps` check, don't trust the exit
code alone), and **always restart after pulling in Python changes** (pure JS/frontend changes under
`www/` need no restart, per the same section).

No HA-python test harness applies to frontend JS; verification is manual in-browser per
`CLAUDE.md`'s UI rule.
