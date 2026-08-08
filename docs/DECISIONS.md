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
