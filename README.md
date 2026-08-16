# Occupancy Tracker

A [Home Assistant](https://www.home-assistant.io/) custom integration that infers whole-house and
per-room occupancy from sensors and devices you already have — no dedicated people-counting
hardware required.

Rooms, floors, devices, and sensors are read live from Home Assistant's own settings — nothing is
re-typed or hand-maintained. The two things only you can provide — how your rooms connect to each
other, and which doors or windows lead outside — are set through a visual room layout editor built
into the integration's own configuration panel.

**Status: functional, pre-HACS-submission.** Setup, the room layout editor (rooms, connections
between them, access points, per-room sensor selection), the occupancy engine, and the "see why"
detail view are all built and working end-to-end against a real Home Assistant instance. Packaging
for HACS distribution (repository metadata, documentation, HACS validation) is still in progress —
see [`docs/STATUS.md`](docs/STATUS.md) for the current build phase and what's left.

## What it does

- **Per-room occupant count and occupied state**, plus a whole-house total, exposed as regular HA
  entities (`sensor`/`binary_sensor`) — usable in dashboards and automations like any other sensor.
- **A visual room layout editor** (Settings → Devices & Services → Occupancy Tracker → Configure,
  or the permanent sidebar entry) for drawing which rooms connect to which, marking doors/windows
  that lead outside, and picking which sensors in each room count as activity.
- **See why, one click away**: select any room in the layout editor to see how sure it is right
  now, when it last saw activity, and — if it thinks someone just walked in from another room —
  what it's currently checking. Not just a number with no way to see why.
- **Companion-app / phone-location fusion**: optionally use where your phone (or Home Assistant's
  own person tracking) says you are as extra supporting evidence and to get the house "ready" a
  little early, without ever letting your phone's location alone decide which room you're in.
- **Tunable timing settings and an optional household-size hint**, set through the integration's
  options — never a YAML file to hand-edit.
- **Services** for manually correcting a room's occupant count
  (`occupancy_tracker.set_occupant_count`) and for backing up/restoring your room layout
  (`occupancy_tracker.export_topology` / `.import_topology`).

## Installation

Not yet distributed via HACS (see "Status" above). Until then:

```
git clone https://github.com/yme207/occupancy-tracker.git
```

then copy `custom_components/occupancy_tracker/` into your Home Assistant `config/custom_components/`
directory, restart Home Assistant, and add the integration from Settings → Devices & Services.

## Getting started

1. Add the integration (Settings → Devices & Services → Add Integration → Occupancy Tracker). No
   setup questions — it discovers your rooms, devices, and sensors directly.
2. Open the room layout editor (Configure, or the sidebar entry) and, for each room: pick which
   sensors count as activity, draw connections to rooms that are physically next to each other, and
   mark any room that has a door or window leading outside (an access point) with its sensor(s).
3. Optionally, use Configure → Options to bring in your phone's location and adjust the timing
   settings to fit your household.

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
