# Occupancy Tracker

A [Home Assistant](https://www.home-assistant.io/) integration that infers whole-house and
per-room occupancy from sensors and devices you already have — no dedicated people-counting
hardware required.

Rooms, floors, devices, and entities are read live from Home Assistant's own Area, Floor, Device,
and Entity registries. The one thing only you can provide — how your rooms connect, and where the
egress points are — is set through a visual topology editor built into the integration's own
configuration UI.

**Status: pre-release, under active development.** No functional occupancy-tracking code exists
yet — see [`docs/STATUS.md`](docs/STATUS.md) for the current build phase.

## Installation

Not yet ready for installation. This section will be filled in once the integration reaches a
usable state (see the build-phase plan in [`docs/STATUS.md`](docs/STATUS.md)).

## Documentation

- [`docs/SPEC.md`](docs/SPEC.md) — product/functional specification.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — technical structure and extension points.
- [`docs/STATUS.md`](docs/STATUS.md) — current build phase and next steps.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — design decision log.

## Contributing

This project is developed session-by-session against the documentation suite above — see
[`CLAUDE.md`](CLAUDE.md) and [`docs/AGENT_WORKFLOW.md`](docs/AGENT_WORKFLOW.md) for how work is
scoped and verified.

## License

Not yet chosen.
