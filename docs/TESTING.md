# Testing Strategy & Gates

Nothing described in `docs/AGENT_WORKFLOW.md`'s "definition of done" is satisfied by tests that
exist but weren't run, or tests built against a fake data shape that doesn't match production —
both were real failures in this project's first prototype. This document exists to make sure that
doesn't recur.

## 1. Test layers

1. **Engine unit tests (fast, no HA dependency).** The occupancy engine (`docs/ARCHITECTURE.md`
   §1.4) is designed to be pure logic — test it directly with constructed topologies and `Signal`
   sequences. This layer should be the bulk of the test suite and run in well under a second.
2. **HA-integration tests.** Anything touching real Home Assistant objects (registries, `Context`,
   config entries, entity platforms, `Store`, `websocket_api`) must be tested using
   `pytest-homeassistant-custom-component` (the standard HA custom-component test harness), which
   provides a real (test-mode) HA core instance and real `State`/`Context`/registry objects — not
   a hand-rolled mock with a different shape than production. This directly fixes the specific
   defect in the first prototype, where tests passed against a mock that didn't resemble what HA
   actually provides.
3. **Scenario tests** for the occupancy model, encoding the concrete cases from `SPEC.md` §11,
   e.g.: latching through a quiet period with no exit evidence; a confirmed multi-room transit;
   two simultaneous disconnected signals inferring a second occupant; an automation-sourced light
   change correctly excluded as evidence; a near-house zone pre-arming without incrementing the
   occupant count.
4. **Registry-sync tests**: area renamed/removed, entity moved between areas, new area appears —
   verify the topology store reconciles correctly rather than silently corrupting saved state.
5. **WebSocket API contract tests** for the topology editor's read/save commands.

## 1a. Windows note: `pytest-homeassistant-custom-component` does not run natively

Confirmed 2026-08-08 (Phase 0): `homeassistant.runner` unconditionally imports the Unix-only
`fcntl` module. The moment `pytest-homeassistant-custom-component` is installed, pytest fails to
even start on native Windows Python — not just for HA-integration tests, but for the whole run,
since it's an autoloaded pytest plugin. This blocks test layers 2–5 (anything importing
`pytest-homeassistant-custom-component`) locally on this development machine; layer 1 (pure-Python
engine unit tests, no HA import) is unaffected and runs fine natively.
CI (`ubuntu-latest`) is unaffected and remains the authoritative gate. See `docs/DECISIONS.md` for
how local development handles this.

## 2. Required tooling

- `pytest` + `pytest-homeassistant-custom-component` for layers 2–5.
- `ruff` for lint + format.
- `mypy` for type checking.
- Home Assistant's `hassfest` validation (manifest correctness) and the HACS validation action,
  run in CI — these catch a specific, real class of packaging mistakes (bad manifest fields,
  missing `config_flow` declaration, etc.) before they reach a user's install.

## 3. CI gate

GitHub Actions workflow runs on every push/PR:

- `ruff check` + `ruff format --check`
- `mypy`
- `pytest` (full suite)
- `hassfest`
- HACS validation action

All must pass before a change is considered mergeable. No `--no-verify`, no skipped steps, no
"fix it later" merges — if CI is red, the slice isn't done (see `docs/AGENT_WORKFLOW.md` §4).

## 4. What "tests pass" means in practice for an agent session

- Actually run the test command and read its output — don't infer pass/fail from reading the test
  code.
- A newly written test that wasn't run at least once (and observed to fail before the fix, pass
  after) hasn't actually verified anything — prefer writing the test first, watching it fail for
  the right reason, then implementing.
- If a test is flaky or environment-dependent, fix or remove it — a flaky test in this suite is
  worse than no test, because it teaches future sessions to distrust the suite.

## 5. Coverage expectation

No arbitrary percentage target. Instead: every branch in the occupancy engine's transit-inference
and provenance-resolution logic (`SPEC.md` §6.2–§6.8) should have a corresponding scenario test,
since that logic is the actual product — untested branches there are untested product behavior,
not incidental code.
