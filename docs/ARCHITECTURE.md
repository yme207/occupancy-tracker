# Architecture & Extensibility Contracts

Companion to [`SPEC.md`](SPEC.md) (the *what*) — this is the *how*, written so future features are
additive, not rewrites. If you're about to hardcode something house-specific, sensor-specific, or
UI-specific, check here first for the extension point that should hold it instead.

## 1. Layered structure

```
registry sync  →  topology store  →  occupancy engine  →  entity platforms
                                   ↑
                        signal ingestion (+ provenance resolver, zone fusion)
                                   ↓
                    websocket API  →  frontend panel (topology editor)
```

Each layer only talks to the layer(s) adjacent to it. The occupancy engine in particular must have
**no Home Assistant import dependency it doesn't strictly need** — it should be testable as plain
Python given a topology + a stream of signals, independent of HA being running at all (this is
what makes §TESTING.md's fast unit-test layer possible).

### 1.1 Registry sync layer
Wraps `homeassistant.helpers.area_registry`, `floor_registry`, `device_registry`,
`entity_registry`. Exposes a clean, HA-independent "current house shape" model (areas, floors,
entities-per-area) to everything downstream, and re-emits a simplified change event when any of
the underlying registries fire their update events. Downstream code never touches the HA registry
helpers directly.

### 1.2 Topology store
Persists (via `homeassistant.helpers.storage.Store`) the one thing that's genuinely user-authored:
Connectors, egress-point bindings, and per-area entity selections. **Must carry an explicit schema
version** and a `Store` migration function — assume this schema will grow (new signal types, new
per-connector settings) and design the persisted shape so old data upgrades cleanly rather than
breaking or being silently dropped.

### 1.3 Signal ingestion layer
Subscribes to state changes for entities selected in the topology store. For each change:
resolves provenance (§4 below) and, for `person`/`device_tracker` entities, resolves zone state.
Emits a normalized `Signal` object (source, value, confidence, provenance, timestamp) — this is
the *only* shape the occupancy engine consumes; it never sees a raw HA `State` object.

### 1.4 Occupancy engine
The latch/transit-inference state machine (`SPEC.md` §6). Pure logic: given a topology and a
stream of normalized `Signal`s, produces per-room occupant state + confidence tier. No I/O, no HA
imports beyond typing if unavoidable. One instance per config entry, created once, held for the
lifetime of the entry, and shared by every entity that reads from it (never recreated per property
access — this exact mistake in the v0 prototype silently broke temporal/transit state across every
entity read).

### 1.5 Entity platforms
Thin views over the shared engine instance. A property getter reads current state; it does not
recompute anything itself.

### 1.6 WebSocket API + frontend panel
The visual topology editor (`SPEC.md` §7.3). Talks to the registry sync layer (read) and topology
store (read/write) directly; does not go through the occupancy engine.

## 2. Extension points (design for these explicitly)

- **`SignalSource` interface.** Every kind of evidence — motion sensor, analog sensor, device-state
  inference, egress contact, zone presence — implements a common protocol/interface that the
  signal ingestion layer registers against. Adding a new evidence type later (e.g. a presence-radar
  sensor, an energy-based "kettle just ran" inference) means writing a new implementation of this
  interface, not modifying the ingestion layer's core dispatch logic.
- **Provenance resolver as a pluggable strategy.** The automation-vs-manual heuristic (`SPEC.md`
  §6.6) will need tuning as it's tested against real installations. It must sit behind a single,
  swappable interface so refining it doesn't ripple through every call site that currently checks
  provenance.
- **Typed, centralized config.** Every tunable parameter (decay/confirmation windows, confidence
  thresholds, household-size hint, near-house zone list) flows through one typed config object,
  sourced from the options flow / topology store. Exposing a new tunable in the UI should mean
  adding one field, not threading a new parameter through several modules.
- **Topology schema versioning.** See §1.2 — this is the extension point that prevents "add one
  field to the topology" from becoming a breaking migration for existing users.
- **Entity platform additions.** New entity types (e.g. a future "ambiguous transits" diagnostic
  sensor) should only need to read from the existing engine's public state — if adding one requires
  changing the engine's internals, that's a sign the engine's public interface is too narrow and
  needs revisiting directly, rather than working around it.

## 3. Banned patterns (specific, not generic advice)

These are things the first prototype actually did; they're called out explicitly because "don't
write bad code" is too vague to enforce and these are the concrete failure modes to watch for:

- A `while True: await asyncio.sleep(...)` loop used as the primary update mechanism, instead of
  real event subscriptions.
- Recreating a stateful engine/calculator instance inside an entity's property getter.
- Duplicating a default config block (or any config) across multiple files instead of one shared
  source.
- Calling an HA method/attribute that "sounds right" without checking it exists (`entity.confidence`,
  `hass.event_listener`, `config_entry.async_update_hass_options`, wrong-case device-class enum
  members were all real examples — see the project's code-review history in `docs/DECISIONS.md`).
- Tests built against a hand-rolled mock whose data shape doesn't match what production code
  actually receives from Home Assistant.

## 4. Provenance resolution (implementation note)

See `SPEC.md` §6.6 for the product-level model. Implementation must:
- Walk the `Context.parent_id` chain (bounded depth — don't walk indefinitely) to check whether it
  resolves to a known automation/script context.
- Check `Context.user_id` directly for a human-attributed action.
- Treat "neither present" as its own case (weak-positive physical/manual), not as an error.
- Be unit-testable with constructed `Context` chains, independent of a running HA instance.

## 5. Tooling & style baseline

- Full type hints; `mypy` clean.
- `ruff` clean (formatting + linting).
- No unused imports (a recurring smell in the first prototype — every entity-platform file
  imported `CoordinatorEntity` and never used it).
- Docstrings only where they explain a non-obvious *why* (constraint, workaround, invariant) — not
  restating the function name in prose. Consistent with the project's general code style.
