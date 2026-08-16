# Occupancy Tracker — Specification & Requirements

Status: **Draft v2.1** — this is the *product/functional* spec. It's one part of a documentation
suite written so this project can be built and maintained primarily by an AI coding agent. Read
this alongside:

- **[`/CLAUDE.md`](../CLAUDE.md)** — the agent's operating rules (start here in any coding session).
- **[`docs/ARCHITECTURE.md`](ARCHITECTURE.md)** — technical structure, module boundaries, and the
  extension points that keep future features from requiring rewrites.
- **[`docs/AGENT_WORKFLOW.md`](AGENT_WORKFLOW.md)** — how development sessions are scoped,
  verified, and handed off, including the anti-hallucination and context-window rules.
- **[`docs/UX_GUIDELINES.md`](UX_GUIDELINES.md)** — the visual/interaction bar for the UI (no "AI
  slop," modern and native-feeling, the "witchcraft" product feel).
- **[`docs/TESTING.md`](TESTING.md)** — required test layers, tooling, and CI gates.
- **[`docs/STATUS.md`](STATUS.md)** — current build phase and next steps; read this first each
  session, it's more current than this file's prose.
- **[`docs/DECISIONS.md`](DECISIONS.md)** — why past choices were made (ADR log).

This supersedes v1's assumption of a single, hand-configured house. Occupancy Tracker is specified
as a **general-purpose, HACS-distributed Home Assistant integration**, configured entirely through
the HA GUI, that any user can install and set up without editing a single file.

---

## 1. Overview & Product Positioning

Occupancy Tracker determines how many people are in a house and which room each is likely in,
using sensors and devices the user already has in Home Assistant — no dedicated people-counting
hardware required.

**This is now a product, not a personal script.** It must be installable via HACS by any Home
Assistant user, work against *their* house (unknown to us in advance, arbitrary shape, arbitrary
sensor coverage), and be fully configurable and re-configurable from the HA UI at any time —
including a **visual editor for drawing how rooms connect**, which is the one piece of setup that
genuinely can't be auto-derived from Home Assistant.

Everything else — the list of rooms, and which devices/entities belong to each — must be **pulled
live from Home Assistant's own registries**, not re-entered by the user and not hand-maintained
as static config.

## 2. Goals (v2)

1. **Zero hand-written configuration.** Rooms, floors, devices, and entities are read from Home
   Assistant's native registries. The user never types a room name or an entity ID into a YAML
   file.
2. **Registries stay authoritative — always.** If a room is renamed in HA, an entity is moved to
   a different area, or a new area is added, Occupancy Tracker must pick that up automatically
   (event-driven, not just "on restart"), and the topology/config must not silently go stale.
3. **A visual, in-app topology editor.** The one thing only the user can provide — which rooms are
   physically connected, and where the egress points are — must be set through a graphical
   interface that lives inside the integration's own configuration UI, reachable at any time
   (not just during first-time setup) and always reflects the current set of HA areas.
4. **Egress points are first-class.** Any area can be flagged as a house egress point and bound to
   specific door/window open-close entities in that area, without needing a whole separate config
   mechanism.
5. **Unbounded, dynamic occupant counting.** The system must scale from a studio flat to a large
   multi-occupant household without a hard ceiling. A "typical household size" may be supplied as
   an optional hint to the algorithm's confidence tuning, but it must never cap the actual count.
6. **Robust, evidence-based automation-vs-manual detection.** Distinguish a device changing state
   because of a person vs. because of an automation/script, using Home Assistant's own causality
   model (event `Context`), the same mechanism HA's own Logbook relies on — understood, applied
   correctly, and treated as *evidence with a confidence level*, not an infallible switch (see
   §6.6 for why).
7. **Companion-app zone presence as a first-class signal, from day one.** A person's phone
   reporting them in a *zone* (e.g. "home", or a named zone like "front yard") is corroborating
   evidence, not proof of being inside the house — egress-point activity (door + motion/manual
   action) is what confirms an actual arrival or departure. Zone entry can also pre-arm
   arrival/departure automations before an egress event is confirmed.
8. **Distributable via HACS.** Packaged, versioned, documented, and structured the way HACS and
   the Home Assistant integration quality scale expect, so it's realistically installable by
   someone who is not you.
9. **Efficient and performant by construction.** Event-driven throughout (no polling loops standing
   in for real update mechanisms — see `docs/ARCHITECTURE.md`), incremental recomputation, and no
   wasted work — full detail in §9 and `docs/ARCHITECTURE.md`.
10. **A product that feels like it "just knows."** The setup experience and the day-to-day feel of
    the integration should read as effortless and impressively capable, not like a raw
    configuration tool — full detail in `docs/UX_GUIDELINES.md`.

## 3. Non-Goals (v2)

- No camera/vision-based person detection or re-identification.
- No per-person identity tracking beyond what a companion-app device_tracker already provides
  (i.e. we consume `person`/`device_tracker` entities; we don't build a new identity system).
- No requirement to auto-infer room topology (which rooms connect to which) — this is
  fundamentally something only the user knows, and is explicitly a manual, visual step (§7.3).
- No custom mobile app — configuration happens inside the Home Assistant frontend.

## 4. Core Concepts & Terminology

| Term | Meaning |
|---|---|
| **Area** | Home Assistant's native concept of a room/space (Area Registry). This is what we mean by "room" — we do not invent a parallel concept. |
| **Floor** | Home Assistant's native grouping of Areas (Floor Registry), used optionally for display/grouping, not for topology logic. |
| **Connector** | A user-drawn link between two Areas (or an Area and an egress point) in the topology editor, representing a passage a person must cross to move between them. |
| **Egress point** | An Area flagged by the user as a boundary to outside the house, with one or more door/window entities bound to it as the actual sensors of crossing. |
| **House graph** | All Areas + user-drawn Connectors + egress points — the topology the transit-inference algorithm reasons over. |
| **Occupant token** | An abstract unit representing one believed person, held by exactly one Area (or "outside") at a time. |
| **Transit event** | An inferred movement of an occupant token across a Connector. |
| **Signal** | A single piece of evidence: a sensor firing, a device changing state, a zone change — timestamped, sourced, and (where relevant) carrying a provenance confidence. |
| **Provenance** | The inferred cause of a signal: direct physical/human action, automation/script, or unknown — derived from HA's `Context` chain (§6.6). |
| **Zone presence** | The zone (`home`, `not_home`, or a named zone like `zone.front_yard`) reported by a `person`/`device_tracker` entity. |

## 5. Data Source of Truth: Native Home Assistant Registries

### 5.1 Rooms = Home Assistant Areas

The integration must **not** maintain its own room list. On setup (and continuously thereafter),
it reads the **Area Registry** (`homeassistant.helpers.area_registry`) to enumerate rooms, and the
**Floor Registry** (`homeassistant.helpers.floor_registry`) for optional floor grouping in the UI.
An Area with zero devices/entities is still a valid graph node (the user may still want it in the
topology, e.g. a hallway with no sensors at all, connecting two sensored rooms).

### 5.2 Devices/Entities per Room = Home Assistant Device/Entity Registry

Which devices and entities live in which Area is likewise **not** something the user re-declares
here — it's read from the **Device Registry** and **Entity Registry**
(`homeassistant.helpers.device_registry`, `homeassistant.helpers.entity_registry`), both of which
carry an `area_id` (an entity can inherit its device's area, or override it directly). The
integration presents these, per area, in its UI so the user can pick which ones matter for
occupancy (e.g. "this motion sensor," "this TV," "this door contact") rather than treating every
entity in a room as equally relevant.

### 5.3 Dynamic Sync (must not go stale)

Home Assistant's registries fire update events — `area_registry_updated`, `device_registry_updated`,
`entity_registry_updated` — whenever something changes. The integration must **subscribe to these**
and react: if an area is renamed, merged, or removed; if an entity moves to a different area or is
deleted; if a new area/entity appears — the topology and per-area entity picks must be revalidated
and the UI/state kept consistent, without requiring the user to notice and manually fix things.
Where a change breaks part of the topology (e.g. an entity bound to an egress point is deleted),
the integration must surface that clearly rather than fail silently or crash.

### 5.4 What Cannot Be Auto-Derived (must be user-declared)

Two things are inherently unknowable from the registries alone, and must be provided by the user
through the GUI (§7.3):

1. **Topology** — which Areas are physically adjacent/connected, and via which Connector.
2. **Egress points** — which Areas count as a boundary to outside, and which door/window
   entities within them represent that boundary.

Everything else (room list, entity list, floor grouping) is derived, not entered.

## 6. Occupancy Model

### 6.1 Topology Graph

Same conceptual model as v1: rooms (now: HA Areas) are graph nodes, Connectors are edges, and an
"outside" boundary node exists for each egress point. The graph is built entirely from data the
user set in the visual editor (§7.3), layered on top of the live Area Registry. Floors are a
display/grouping attribute only (used to group Areas visually in the topology editor) — the
transit-inference algorithm treats every Connector identically regardless of which floor(s) it
spans; a staircase connector carries no different weighting than a same-floor doorway connector.

### 6.2 Occupant State Machine (latching, not decaying)

Unchanged from v1: each Area holds a non-negative integer occupant count that only changes on a
discrete transit event (or a confirmed egress event). Absence of signal never decays it — a room
only empties when there's positive evidence of an exit. This holds unconditionally for any Area's
*directly evidenced* count.

**One narrow, deliberate exception, added 2026-08-16** ("uncertain births" — see `docs/DECISIONS.md`
for the full reasoning): when a new occupant is inferred because two or more Connector-adjacent
Areas were *equally* plausible sources (§6.3's transit inference can't tell which, so it doesn't
guess — the new occupant is recorded as usual), that specific inference — not the Area's count in
general, only this one uncorroborated guess — is allowed to lose confidence purely from elapsed
time, and self-correct back to whichever original candidate is still untouched since the tie, if
nothing ever independently confirms it really was a separate person. This is what lets a missed or
slow transit heal itself instead of permanently double-counting; it never applies to a count with
real, direct evidence behind it, and it never happens sooner than a configurable delay
(`uncertain_birth_resolution_delay`, §7.2) intended to give real corroborating evidence a fair
chance to arrive first.

### 6.3 Transit Inference

Unchanged in principle from v1: a Connector's sensor firing shortly after an occupied Area goes
quiet is candidate evidence of an exit via that path; the destination Area is watched for
corroboration before the transit is confirmed. No corroborating Connector activity → occupant is
assumed to still be in the Area.

### 6.4 Multi-Occupant Disambiguation (unbounded)

Unchanged in principle from v1: simultaneous, non-explainable signals in disconnected Areas
increase belief in an additional occupant token, subject to a conservation check across the whole
graph. **There is no maximum.** An optional "typical household size" setting — a single whole-house value,
not per-area — may be used purely to tune *confidence* (e.g. "an unexplained 6th simultaneous
occupant is treated with lower confidence than a 2nd," since it's statistically less likely for
most households) — it must never reject or cap a count the evidence actually supports.

### 6.5 Egress Points

An egress point is simply an Area the user has flagged as a house boundary, with one or more
door/window entities selected as its "crossing" sensors (§5.4, §7.3). Egress-point activity is
handled exactly like any other Connector, except one side of it is "outside" rather than another
Area — its Connector logic drives arrival/departure changes to the whole-house occupant total
(§6.6 of v1; retained here unchanged), and it's the anchor for zone-presence fusion (§6.7 below).

### 6.6 Automation vs. Manual Signal Provenance

**Researched and grounded in Home Assistant's own causality model.** Every state change carries a
`Context` with an `id`, an optional `parent_id`, and an optional `user_id`:

- When an automation or script runs (because of a trigger), it is assigned its own `Context`. Any
  state change that automation/script causes is stamped with a **new** context whose `parent_id`
  points back to the automation/script's context. This is exactly how Home Assistant's own Logbook
  attributes entries to "triggered by automation X."
- A state change caused directly by a person (toggling in the HA UI, the companion app, voice
  assistant) generally carries a `user_id` identifying who did it, and typically **no**
  automation-context `parent_id` in the chain.
- A state change reported by a physical device acting on its own (e.g. a smart wall switch
  toggled at the wall, reporting its new state through its integration) typically has **neither**
  a `user_id` nor a `parent_id` — it's an origin-less "physical world" event.

**This is a heuristic, not a guarantee** — Home Assistant's own community/developer discussions
note `parent_id`/`user_id` propagation isn't always perfectly consistent (e.g. certain trigger
paths, some integrations not tagging context fully). The spec must treat provenance as **a
confidence signal**, not a hard boolean gate:

- `parent_id` chain resolves to an automation/script → **suppress** as occupancy evidence
  (high confidence it's machine-caused).
- `user_id` present, no automation ancestry → **accept** as occupancy evidence (high confidence
  it's human-caused).
- Neither present → treat as **physical/manual and weakly-positive** evidence by default (a
  device with no causal chain at all is most consistent with someone physically operating it),
  but at lower confidence than a direct `user_id` match, and this default must be documented as a
  judgment call to revisit once tested against real installations.

### 6.7 Companion App / Zone Presence Fusion

A `person`/`device_tracker` entity's zone state is a real, first-class signal, fused with egress
data rather than trusted alone:

- **Zone = "home"** corroborates that the person is *somewhere* in the house, but says nothing
  about which room — it should raise confidence in the current occupant total, and can help
  resolve ambiguous transits (§6.7 of v1, "confidence tiers"), but doesn't by itself place someone
  in a specific Area.
- **A named zone outside the house** (e.g. `zone.front_yard`, a driveway zone) is evidence someone
  is *near* the house, not evidence they're inside it. This should not increment the house
  occupant count, but is legitimate as a **pre-arm** signal — e.g. enabling a lighting/unlock
  automation to trigger faster once genuine egress-point activity (door + motion, or a manual
  action) follows shortly after. Which zones count as "near the house" is **explicitly user-picked**
  in the options flow (§7.2) — the integration does not auto-detect zones by proximity to
  `zone.home`'s radius.
- **Confirmed arrival/departure** still requires egress-point corroboration (door/motion activity
  at a flagged egress Area) — zone presence alone must never silently change the occupant count,
  it only informs confidence and timing (including pre-arming automations tied to "someone is
  approaching").
- **Zone = "not_home"** (with no recent egress-point activity) is evidence *against* that person
  currently being counted in the house total, and should be used to correct/decay the *confidence*
  behind a stale occupant token if other evidence for it has also gone quiet — but should not
  instantly zero out a room; it works through the same latch/transit machinery as any other
  departure evidence, anchored at an egress point.

### 6.8 Confidence & Ambiguity Handling

Unchanged in principle from v1 (confirmed / latched / ambiguous tiers), extended to also carry
provenance confidence (§6.6) and zone-corroboration state (§6.7) as explicit, inspectable
attributes on the relevant entities — this system will get things wrong sometimes, and the UI/
entity attributes must make it possible to see *why* it believes what it believes.

## 7. Configuration & GUI Requirements

### 7.1 Config Flow (initial setup)

Minimal by design: adding the integration via the HA UI should require little more than
confirmation — it discovers Areas/Devices/Entities itself. Any advanced setup (topology, egress
points, entity selection per area) happens in the Options Flow / topology editor (§7.3), which
must be reachable at any time after setup, not just once.

### 7.2 Options Flow (settings)

Standard HA options-flow forms for scalar settings: optional "typical household size" hint,
provenance-confidence thresholds, transit confirmation/grace windows, and which zone(s) count as
"near the house" for pre-arming (§6.7).

### 7.3 Visual Topology Editor (core new requirement)

This is the piece that cannot be a standard voluptuous options-flow form — the user needs to see
their rooms and draw connections between them, and flag egress points, graphically.

**Architecture (grounded in how real HACS integrations do this today):**
- A **custom frontend panel**, shipped as a bundled JS module inside the integration's own
  repository, registered so it appears inside the integration's configuration UI (accessible via
  Settings → Devices & Services → Occupancy Tracker → Configure, and independently re-openable at
  any time after setup).
- The panel renders the current Areas (from the live registries, §5.1) as nodes and lets the user
  draw/remove Connector edges between them, and mark any Area as an egress point (picking its
  door/window entity from that Area's entity list).
- Visual and interaction quality is a hard requirement, not a nice-to-have — the panel must meet
  `docs/UX_GUIDELINES.md` (native HA look-and-feel, light/dark theme support, smooth interactions).
  It should also double as the primary "explainability" surface: selecting a room shows the live
  signals, confidence tier, and transit reasoning behind its current state (the "how did it know
  that" moment `docs/UX_GUIDELINES.md` calls out as core to the product feel).
- All tunable parameters from §7.2 (and the confidence thresholds in §6.6/§6.4) must be exposed
  here or in the options flow as labeled, ranged controls with plain-language effect descriptions
  — never a raw JSON/YAML blob for the user to hand-edit.
- Backend support via a **WebSocket API** the integration registers (`websocket_api` commands) for
  the panel to read live Area/Entity data and to save topology changes — not a REST round-trip via
  the options-flow form mechanism, which isn't built for a graphical editor.
- This is a genuinely larger engineering surface than a typical integration (frontend JS + backend
  websocket commands, in addition to the occupancy engine itself) and should be scoped/sequenced
  as such rather than bolted on at the end.
- Changes made in the editor take effect immediately (or on next reload) — no restart required,
  and it must always be re-editable, not a one-time setup wizard.

### 7.4 Persistence

Topology, egress-point bindings, and per-area entity selections are **user data**, not YAML —
persisted via Home Assistant's `Store` helper (`homeassistant.helpers.storage.Store`), the standard
mechanism for a config-entry-scoped `.storage/` JSON file, edited exclusively through the GUI
described above. No file-editing should ever be required or expected of the end user.

## 8. Home Assistant Integration Requirements

- **Config flow required** (`config_flow: true` in `manifest.json`, plus `config_flow.py`) —
  installable and configurable entirely from the UI.
- **Entities per Area**: occupant count (sensor), occupied binary sensor, state-quality attribute
  (confirmed/latched/ambiguous) and provenance/zone-corroboration attributes (§6.8).
- **House-level entities**: total occupant count, diagnostic entity/attribute listing currently
  open ambiguous transits.
- **Services**: manual occupant-count override, topology export/import (for backup or
  copying a config between installs), registered via `hass.services.async_register` with a
  `services.yaml`.
- **All Home Assistant APIs used (registries, Context, websocket_api, storage, frontend panel
  registration) must be verified against current HA developer documentation/source before being
  relied on** — this project's v0 prototype broke on invented/incorrect API usage, and that failure
  mode must not recur, especially now that this targets other people's installations, not just
  one.
- **HACS-ready packaging**: `hacs.json`, a proper `README.md`, semantic versioning in
  `manifest.json`, appropriate `iot_class`/`codeowners`/`issue_tracker` fields, and a repository
  structure HACS validation expects (see §12).

## 9. Non-Functional Requirements

- **Scale to arbitrary, unknown house shapes** — no assumption baked in about room count, naming,
  or sensor coverage; the algorithm must degrade gracefully (lower confidence, not crashes) where
  a user's house has sparse sensor coverage (e.g. a connecting hallway with no sensor at all).
- **No hand-editing required** for any part of normal use — setup, topology changes, and tuning
  all happen in the HA GUI.
- **Correctness of HA API usage over cleverness**, verified against source/docs, not assumed
  (carried over from v1, doubly important now).
- **Bounded recomputation** — a signal updates only the affected Area(s)/Connector(s); no
  full-topology recalculation per entity property read.
- **Event-driven, never polled.** All signal ingestion uses HA's native event/state-change
  subscriptions (e.g. `async_track_state_change_event`); a background `while True: sleep(...)`
  loop standing in for a real update mechanism (as in the v0 prototype) is explicitly disallowed —
  see `docs/ARCHITECTURE.md` for the banned-patterns list.
- **Non-blocking.** No synchronous/blocking calls on the event loop; all I/O (storage reads/writes,
  registry lookups) uses HA's async APIs correctly.
- **Bounded memory.** Per-room signal history and pending-transit tracking must have an explicit
  retention window/cap — they must not grow unbounded over an installation's uptime.
- **Registry-change resilience** — renames/moves/deletions in HA's own registries must be handled
  without corrupting or silently invalidating the saved topology (§5.3).
- **Observability** — enough logging/diagnostic attributes to explain *why* the system believes
  what it believes (signals, transit chain, provenance confidence, zone corroboration), since this
  will run on installations we can't personally debug.
- **Multi-install safety** — since this is now distributed to other users, config/state must be
  fully self-contained per Home Assistant instance (via `Store`, scoped to the config entry), with
  no assumptions specific to any one household leaking into the code.

## 10. Architecture Overview

Rough component breakdown (to be refined during implementation planning, not frozen here):

1. **Registry sync layer** — reads Area/Floor/Device/Entity registries, subscribes to their
   update events, exposes a clean "current house shape" model to the rest of the integration.
2. **Topology store** — persisted (via `Store`) user-drawn Connectors + egress-point bindings,
   reconciled against the live registry sync layer (§5.3).
3. **Signal ingestion layer** — listens to relevant entity state changes, resolves `Context`
   provenance (§6.6), and reads zone-presence signals (§6.7).
4. **Occupancy engine** — the latch/transit-inference state machine (§6.2–6.5), operating over the
   topology store + registry sync layer, consuming signals from the ingestion layer.
5. **Entity platforms** — expose engine state as HA entities (§8), sharing one persistent engine
   instance per config entry (not recreated per property read — a v0 defect that must not recur).
6. **WebSocket API + frontend panel** — the visual topology editor (§7.3), talking to the topology
   store and registry sync layer directly.

## 11. Testing Strategy

Carried over from v1, extended for the new scope:

- Unit tests for the transit-inference state machine against realistic HA `State`/`Context`
  fixtures (not a bespoke mock shape divorced from production data, as in v0).
- Registry-sync tests: area renamed/removed, entity moved between areas, new area appears —
  topology store must reconcile correctly, not silently corrupt.
- Context-provenance tests built on realistic context chains (automation → service call →
  resulting state change; direct user action; contextless physical device report) to validate the
  confidence-tiering in §6.6, including the "neither present" ambiguous case.
- Zone-fusion scenario tests: zone entry to a "near house" zone followed by egress-point activity
  (pre-arm confirmed); zone = "home" alone (corroboration only, no room placement); zone =
  "not_home" with no egress activity (confidence decay, not instant removal).
- WebSocket API contract tests for the topology editor's read/save commands.
- All tests must actually be executed (`pytest`) as part of development, not merely written.

## 12. Development & Repository Workflow

- Work will be developed and maintained in VS Code, from this project folder, then pushed to
  GitHub for ongoing/version-controlled development and eventual HACS submission.
- Target repo structure:
  ```
  /
  ├── custom_components/occupancy_tracker/
  │   ├── __init__.py
  │   ├── config_flow.py
  │   ├── websocket_api.py
  │   ├── registry_sync.py
  │   ├── topology_store.py
  │   ├── occupancy_engine.py
  │   ├── binary_sensor.py / sensor.py / ...
  │   ├── www/                         # bundled frontend panel (topology editor JS)
  │   └── manifest.json
  ├── hacs.json
  ├── docs/
  │   ├── SPEC.md                      # this document (living spec)
  │   └── DECISIONS.md                 # ADR-style log of design decisions/changes
  ├── tests/
  ├── README.md
  └── CHANGELOG.md
  ```
- This spec remains a **living document** — design changes during implementation should update
  this file and be logged in `docs/DECISIONS.md`.
- Git init + GitHub push is a separate, explicit step you'll confirm when ready — not implied by
  writing this document.

## 13. Open Questions / Inputs Needed From You

Most house-specific questions from v1 are now moot (the integration derives that from HA
directly). Resolved:

- **Floors** are display/grouping only (§6.1, §7.3) — every connector is treated identically by
  the transit-inference algorithm regardless of which floors it spans. See `docs/DECISIONS.md`
  2026-08-08 "Floors are display-only."
- **"Typical household size"** is a single whole-house hint (§6.4, §7.2), not per-area. See
  `docs/DECISIONS.md` 2026-08-08 "Household-size hint is whole-house scope."
- **Near-house zones for pre-arming (§6.7)** are explicitly user-picked in the options flow, not
  auto-detected by proximity. See `docs/DECISIONS.md` 2026-08-08 "Near-house zones are
  user-picked."
- **Multi-user HA installs**: topology *editing* (the panel, the websocket save command, and the
  `import_topology` service) requires an admin user; topology *viewing* (the panel's read side, the
  websocket get command, `export_topology`) is open to any authenticated user. See
  `docs/DECISIONS.md` 2026-08-15 "`SPEC.md` §13 Q1 resolved."

Still open — product/design-level, none block Phase 0–3:

1. **Backup/restore** — is exporting/importing topology (mentioned in §8 services) a v1 requirement
   or a later addition? (before Phase 8)
2. **HACS submission bar** — are we building toward the full HACS default-repository review
   (code owners, brand assets, quality scale) from the start, or shipping as a custom-repository
   HACS install first and formalizing later? (before Phase 8) **Partially resolved 2026-08-15:**
   license chosen (MIT, root `LICENSE` added — required by `hacs`'s `check-license` regardless of
   which path this resolves to). The broader default-repository-vs-custom-repository question is
   still open; brand-assets submission remains explicitly skipped (`ignore: brands` in CI, see
   `docs/DECISIONS.md` 2026-08-08).

## 14. Glossary

See §4.

## 15. Revision History

| Date | Change |
|---|---|
| 2026-08-08 | Initial draft (v1) capturing the topology/latching/transit-inference design, scoped to a single hand-configured house. |
| 2026-08-08 | Root-and-branch revision (v2): repositioned as a general-purpose, HACS-distributed integration. Rooms/devices now sourced live from HA Area/Floor/Device/Entity registries instead of user-authored YAML. Added the visual in-app topology editor (custom panel + websocket API) as a core requirement. Removed occupant-count capping — target size is a confidence hint only. Grounded automation-vs-manual detection in HA's `Context.parent_id`/`user_id` model (researched against HA docs/community sources), explicitly documented as a confidence heuristic, not an absolute. Added companion-app zone presence as a first-class, always-on signal fused with egress-point confirmation, including pre-arming from near-house zones. |
| 2026-08-08 | v2.1: split process/technical/UX guidance out into a documentation suite (`CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/AGENT_WORKFLOW.md`, `docs/UX_GUIDELINES.md`, `docs/TESTING.md`, `docs/STATUS.md`) so the project can be built and maintained primarily by an AI coding agent. Added explicit performance/efficiency requirements (event-driven only, non-blocking, bounded memory) and elevated UI/UX craft and setup-time tunability to top-level goals. |
| 2026-08-08 | v2.2: resolved three of §13's open questions ahead of Phase 3 — floors are display-only (§6.1), household-size hint is whole-house scope (§6.4), near-house zones are user-picked not auto-detected (§6.7). See `docs/DECISIONS.md`. |
