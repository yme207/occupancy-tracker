# Project Status

**Read this first, every session.** This file is more current than anyone's memory of past
sessions — keep it that way by updating it at the end of every session that changes anything.

## Current phase

**Phase 0 complete.** `docs/SPEC.md` (functional) and `docs/ARCHITECTURE.md` (technical) are the
agreed target design. The old prototype under `custom_components/occupancy_tracker/` predates this
spec, does not conform to it, and should be treated as reference-only for "what not to do" (see
`docs/DECISIONS.md`'s 2026-08-08 entry) — not as a starting point to patch. **The current
`custom_components/occupancy_tracker/` is the new, spec-conformant scaffold** (manifest + a
confirmation-only config flow, no engine logic yet), not the old prototype.

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
- [ ] **Phase 1 — Registry sync layer.** Read-only Area/Floor/Device/Entity model + live
      registry-update handling (`docs/ARCHITECTURE.md` §1.1). Unit + HA-integration tests.
- [ ] **Phase 2 — Topology store.** Schema (versioned), persistence via `Store`, migration
      scaffold, reconciliation against registry sync on area/entity changes. Tests.
- [ ] **Phase 3 — Occupancy engine.** Latch/transit state machine (`SPEC.md` §6.2–§6.5),
      independent of Home Assistant. This is the core product logic — thorough scenario tests
      per `docs/TESTING.md` §1.3 before moving on.
- [ ] **Phase 4 — Entity platforms.** Wired to one shared engine instance per config entry
      (`docs/ARCHITECTURE.md` §1.4–1.5 — do not recreate the engine per property read).
- [ ] **Phase 5 — Provenance resolver.** `Context` chain walking, confidence tiers
      (`SPEC.md` §6.6). Unit-testable independent of a running HA instance.
- [ ] **Phase 6 — Zone-presence fusion.** `SPEC.md` §6.7 (corroboration + pre-arming, not direct
      count changes).
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

## Known environment constraint (blocks local testing from Phase 1 onward)

`pytest-homeassistant-custom-component` cannot run on native Windows Python — `homeassistant.runner`
unconditionally imports the Unix-only `fcntl` module, and since it's an autoloaded pytest plugin
this breaks the whole pytest run, not just HA-touching tests (confirmed 2026-08-08, see
`docs/TESTING.md` §1a). This machine has no WSL installed. Phase 0's pure-Python manifest test
ran fine locally; **Phase 1 onward needs HA-integration tests (TESTING.md layer 2), which cannot be
verified locally until this is resolved.** Options (needs project-owner decision, not yet made):
WSL2 (needs admin rights + restart to install), a Docker/devcontainer-based workflow, or relying on
GitHub Actions CI as the only place layer-2+ tests actually run (slower local iteration).

## Next action

Phase 1 — Registry sync layer. Before writing HA-integration tests for it, resolve the local
testing constraint above with the project owner.
