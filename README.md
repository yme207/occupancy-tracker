# Occupancy Tracker

A [Home Assistant](https://www.home-assistant.io/) custom integration that infers whole-house and
per-room occupancy from sensors and devices you already have — no dedicated people-counting
hardware required.

Rooms, floors, devices, and entities are read live from Home Assistant's own Area, Floor, Device,
and Entity registries — nothing is re-typed or hand-maintained. The two things only you can
provide — how your rooms connect, and where the access points to outside are — are set through a
visual topology editor built into the integration's own configuration panel.

**Status: functional, pre-HACS-submission.** Setup, the topology editor (rooms, connectors, access
points, per-room entity selection), the occupancy engine, and explainability are all built and
working end-to-end against a real Home Assistant instance. Packaging for HACS distribution
(repository metadata, documentation, HACS validation) is still in progress — see
[`docs/STATUS.md`](docs/STATUS.md) for the current build phase and what's left.

## What it does

- **Per-room occupant count and occupied state**, plus a whole-house total, exposed as regular HA
  entities (`sensor`/`binary_sensor`) — usable in dashboards and automations like any other sensor.
- **A visual topology editor** (Settings → Devices & Services → Occupancy Tracker → Configure, or
  the permanent sidebar entry) for drawing which rooms connect to which, flagging access points to
  outside, and picking which entities in each room count as occupancy evidence.
- **Explainability, one click away**: select any room in the topology panel to see its current
  confidence tier, last confirmed activity, and any in-progress transit reasoning — not just a
  number with no way to see why.
- **Companion-app / zone-presence fusion**: optionally fuse `person`/`device_tracker` zone state as
  corroborating evidence and a pre-arm signal for automations, without ever letting zone presence
  alone place someone in a specific room.
- **Tunable confidence windows and an optional household-size hint**, set through the integration's
  options — never a YAML file to hand-edit.
- **Services** for a manual occupant-count correction (`occupancy_tracker.set_occupant_count`) and
  topology backup/restore (`occupancy_tracker.export_topology` / `.import_topology`).

## Installation

Not yet distributed via HACS (see "Status" above). Until then: clone this repository and copy
`custom_components/occupancy_tracker/` into your Home Assistant `config/custom_components/`
directory, restart Home Assistant, then add the integration from Settings → Devices & Services.

## Getting started

1. Add the integration (Settings → Devices & Services → Add Integration → Occupancy Tracker). No
   setup questions — it discovers your Areas, devices, and entities directly.
2. Open the topology editor (Configure, or the sidebar entry) and, for each room: pick which
   entities count as occupancy evidence, draw connectors to physically adjacent rooms, and flag any
   room that's a boundary to outside (an access point) with its door/window sensor(s).
3. Optionally, use Configure → Options to fuse companion-app zone presence and tune the confidence
   windows to your household.

## Documentation

- [`docs/SPEC.md`](docs/SPEC.md) — product/functional specification.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — technical structure and extension points.
- [`docs/UX_GUIDELINES.md`](docs/UX_GUIDELINES.md) — the visual/interaction bar for the UI.
- [`docs/STATUS.md`](docs/STATUS.md) — current build phase and next steps.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — design decision log.

## Contributing

This project is developed session-by-session against the documentation suite above — see
[`CLAUDE.md`](CLAUDE.md) and [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md) for how work is
scoped and verified.

## License

Not yet chosen.
