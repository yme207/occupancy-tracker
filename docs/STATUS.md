# Project Status

**Read this first, every session.** This file is more current than anyone's memory of past
sessions — keep it that way by updating it at the end of every session that changes anything.

## Current phase

**Specification complete. No implementation code exists yet.** `docs/SPEC.md` (functional) and
`docs/ARCHITECTURE.md` (technical) are the agreed target design. The old prototype under
`custom_components/occupancy_tracker/` predates this spec, does not conform to it, and should be
treated as reference-only for "what not to do" (see `docs/DECISIONS.md`'s 2026-08-08 entry) —
not as a starting point to patch.

## Build phases (planned order)

Work bottom-up: engine logic before HA glue, HA glue before the frontend that depends on it.

- [ ] **Phase 0 — Repo scaffolding.** `hacs.json`, `manifest.json` (with `config_flow: true`),
      empty `config_flow.py`, CI workflow (`docs/TESTING.md` §3), `README.md`, `CHANGELOG.md`,
      pre-commit/lint config. No functional code yet.
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

## Next action

Start Phase 0 (repo scaffolding) once `git init` / GitHub repo creation is confirmed with the
project owner — that's an explicit, separate step from this documentation work (see `SPEC.md`
§12).
