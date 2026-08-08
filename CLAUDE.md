# Occupancy Tracker — Agent Operating Rules

This file is read automatically at the start of every Claude Code session in this repository.
It is the entry point — read it first, every session, before touching code.

## What this project is

A general-purpose, HACS-distributed Home Assistant integration that infers whole-house and
per-room occupancy from existing sensors/devices, using room topology, transit inference, and
Home Assistant's native registries. Full functional spec: [`docs/SPEC.md`](docs/SPEC.md).

## Read in this order, every session

1. [`docs/STATUS.md`](docs/STATUS.md) — what phase we're in, what's done, what's next. This is
   more current than your memory of past sessions. **Always read this first.**
2. [`docs/SPEC.md`](docs/SPEC.md) — the product/functional requirements (the "what").
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — technical structure and extension points
   (the "how"), only as deep as the current task needs.
4. [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md) — how to scope a session, verify work, and
   hand off. Read in full before your first coding session in this repo.
5. [`docs/UX_GUIDELINES.md`](docs/UX_GUIDELINES.md) — required reading before touching anything
   under `custom_components/occupancy_tracker/www/`.
6. [`docs/TESTING.md`](docs/TESTING.md) — required test layers and the CI gate.
7. [`docs/DECISIONS.md`](docs/DECISIONS.md) — why past choices were made; check before reversing
   one.

## Hard rules (non-negotiable)

1. **Never assume a Home Assistant API.** Every HA symbol (registry helper, entity base class,
   `Context` field, `websocket_api` decorator, `Store` usage, frontend panel registration) must be
   verified against the installed `homeassistant` package source or current HA developer docs
   before it's used — not recalled from memory. This project's first prototype failed almost
   entirely because of invented APIs; that failure mode is the single most important thing to not
   repeat. If you can't verify something, say so and go verify it — don't proceed on a plausible
   guess.
2. **Nothing is "done" until its tests pass.** Not "written" — run and green. See
   `docs/TESTING.md`.
3. **No house-specific or personal data in source code, ever.** Rooms, entities, thresholds all
   come from HA's registries or the user's saved topology (`Store`), never hardcoded, never
   duplicated as boilerplate across files (this was a specific, repeated defect in the first
   prototype — one config object, read from one place).
4. **No polling loops standing in for real subscriptions.** Signal ingestion is event-driven
   (`async_track_state_change_event` and friends). See `docs/ARCHITECTURE.md` for the full banned-
   patterns list.
5. **One shared engine instance per config entry.** Entities read from it; they never construct
   their own throwaway copy of the occupancy engine (another specific first-prototype defect that
   silently broke the whole state machine).
6. **UI changes must meet `docs/UX_GUIDELINES.md`.** No generic/default-looking forms, no
   inconsistent theming, no placeholder content shipped as final.
7. **Extend, don't hardcode.** Before adding a new signal type, threshold, or config field, check
   `docs/ARCHITECTURE.md`'s extension points — if it needs a rewrite of existing code to bolt on,
   that's a signal the architecture needs revisiting, not that hardcoding is fine "for now."

## Session shape

Work in small, independently-testable vertical slices (see `docs/AGENT_WORKFLOW.md` for the full
process and suggested sub-agent breakdown). At the end of any session that changed code:

- Tests pass.
- `docs/STATUS.md` reflects reality (what's done, what's next).
- `docs/DECISIONS.md` has a new entry if a design decision was made or changed.
- No dead code, no unused imports, no leftover TODOs without a tracked follow-up in STATUS.md.

## When in doubt

Favor maintainability and correctness over speed. Ask (via a STATUS.md open question, or directly)
rather than guess on anything touching: Home Assistant API behavior, a product decision not
covered in `docs/SPEC.md`, or a UX judgment call not covered in `docs/UX_GUIDELINES.md`.
