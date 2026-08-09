# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). Nothing has been tagged/released yet — everything
below is still `[Unreleased]`; see [`docs/STATUS.md`](docs/STATUS.md) for the current build phase.

## [Unreleased]

### Added

- Registry sync layer reading Areas/Floors/Devices/Entities live from Home Assistant's own
  registries, reacting to registry changes without a restart.
- Topology store (`Store`-backed, schema-versioned) for the one genuinely user-authored data:
  connectors between rooms, access-point bindings, per-room entity selections, and panel layout.
- The occupancy engine: a latch/transit-inference state machine producing per-room occupant counts
  and confidence tiers (confirmed/latched/ambiguous) from a stream of normalized signals.
- Signal ingestion, automation-vs-manual provenance resolution (via Home Assistant's `Context`
  chain), and companion-app/device-tracker zone-presence fusion (corroboration + pre-arm).
- `sensor`/`binary_sensor` entities per room plus whole-house total occupant count and pre-armed
  entities, all push-updated (no polling).
- A visual, in-app topology editor (custom frontend panel + WebSocket API): draggable room layout,
  editable connectors, access-point flagging, per-room entity selection, and a live-refreshing
  explainability inspector showing each room's current confidence and reasoning.
- An options flow for zone-presence fusion settings, an optional "typical household size" hint, and
  the engine's transit/confirmation timing windows — all tunable without editing files.
- Services: `set_occupant_count` (manual per-room correction) and `export_topology` /
  `import_topology` (backup/restore, or copying a topology between installs).

### Known gaps before HACS submission

- Repository metadata in `manifest.json` (`codeowners`, `documentation`, `issue_tracker`) is still a
  placeholder — see `docs/STATUS.md`.
- HACS submission bar (default-repository vs. custom-repository-first) not yet decided —
  `docs/SPEC.md` §13.
