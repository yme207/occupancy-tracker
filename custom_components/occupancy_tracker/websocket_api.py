"""WebSocket API for the visual topology editor (docs/SPEC.md §7.3).

Talks directly to the registry sync layer (read) and topology store
(read/write), per docs/ARCHITECTURE.md §1.6 — never through the occupancy
engine. Registering these commands requires `hass.data[websocket_api.DOMAIN]`
(the handler table `async_register_command` writes into) to already exist,
which is why `websocket_api` is listed in `manifest.json`'s `dependencies`:
verified from `setup.py`'s `_async_process_dependencies`, which sets up every
declared dependency before a depending integration's own `async_setup`/
`async_setup_entry` runs — including for a config-entry-only integration like
this one, since `config_entries.py`'s `ConfigEntries.async_setup` calls
`async_setup_component(hass, domain, config)` the first time that domain is
set up, and that function is what processes `dependencies`.

Command/response shapes deliberately reuse `topology_store.topology_to_dict`/
`topology_from_dict` rather than re-deriving their own JSON shape, since a
`TopologyDict` is already a plain, JSON-safe structure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.const import ERR_INVALID_FORMAT, ERR_NOT_FOUND
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.config_entries import ConfigEntryState, UnknownEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .occupancy_engine import OccupancyEngine
from .registry_sync import HouseShape
from .topology_store import async_replace_topology, topology_from_dict, topology_to_dict

if TYPE_CHECKING:
    from . import OccupancyTrackerConfigEntry

_CONNECTOR_SCHEMA = vol.Schema(
    {
        vol.Required("connector_id"): str,
        vol.Required("area_id_a"): str,
        vol.Required("area_id_b"): str,
    }
)

_EGRESS_POINT_SCHEMA = vol.Schema(
    {
        vol.Required("area_id"): str,
        vol.Required("entity_ids"): [str],
    }
)

_AREA_POSITION_SCHEMA = vol.Schema(
    {
        vol.Required("x"): vol.Coerce(float),
        vol.Required("y"): vol.Coerce(float),
    }
)

_OUTSIDE_POSITION_SCHEMA = vol.Any(None, _AREA_POSITION_SCHEMA)


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the topology editor's websocket commands."""
    websocket_api.async_register_command(hass, websocket_get_topology)
    websocket_api.async_register_command(hass, websocket_save_topology)
    websocket_api.async_register_command(hass, websocket_get_engine_state)
    websocket_api.async_register_command(hass, websocket_subscribe_engine_state)


def _resolve_entry(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> OccupancyTrackerConfigEntry | None:
    """Look up the requested config entry, sending a WS error if it's unusable.

    Returns None (having already sent the error) if the entry doesn't exist,
    belongs to a different domain, or isn't currently loaded (`runtime_data`
    is only populated while loaded).
    """
    try:
        entry = hass.config_entries.async_get_known_entry(msg["entry_id"])
    except UnknownEntry:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown config entry")
        return None
    if entry.domain != DOMAIN:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Unknown config entry")
        return None
    if entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "Config entry is not loaded")
        return None
    return entry


def _house_shape_json(house_shape: HouseShape) -> dict[str, Any]:
    return {
        "areas": [
            {
                "area_id": area.area_id,
                "name": area.name,
                "floor_id": area.floor_id,
                "entity_ids": list(area.entity_ids),
            }
            for area in house_shape.areas.values()
        ],
        "floors": [
            {"floor_id": floor.floor_id, "name": floor.name, "level": floor.level}
            for floor in house_shape.floors.values()
        ],
        "entities": [
            {
                "entity_id": entity.entity_id,
                "name": entity.name,
                "device_id": entity.device_id,
                "area_id": entity.area_id,
                "platform": entity.platform,
                "disabled": entity.disabled,
                "hidden": entity.hidden,
            }
            for entity in house_shape.entities.values()
        ],
    }


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/topology/get",
        vol.Required("entry_id"): str,
    }
)
@callback
def websocket_get_topology(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the live house shape plus the current saved topology."""
    entry = _resolve_entry(hass, connection, msg)
    if entry is None:
        return
    runtime_data = entry.runtime_data
    connection.send_result(
        msg["id"],
        {
            "house_shape": _house_shape_json(runtime_data.registry_sync.house_shape),
            "topology": topology_to_dict(runtime_data.topology_store.topology),
        },
    )


def _engine_state_json(engine: OccupancyEngine) -> dict[str, Any]:
    """Live per-Area occupancy belief, for the topology panel's explainability view (SPEC.md §7.3).

    Shared by both `websocket_get_engine_state` (one-shot snapshot, used for the panel's first
    paint) and `websocket_subscribe_engine_state` (push updates thereafter, see that command's own
    docstring) so the two commands serialize identically rather than drifting apart.
    """
    now = dt_util.utcnow()
    pending_ids = engine.pending_transit_connector_ids(now)
    pending_connectors = [c for c in engine.graph.connectors if c.connector_id in pending_ids]
    return {
        "areas": {
            area_id: {
                "occupant_count": state.occupant_count,
                "quality": state.quality.name,
                "last_confirmed": (
                    state.last_confirmed.isoformat() if state.last_confirmed is not None else None
                ),
                "last_provenance": (
                    state.last_provenance.name if state.last_provenance is not None else None
                ),
            }
            for area_id, state in engine.all_area_states(now).items()
        },
        "total_occupant_count": engine.total_occupant_count(now),
        "pending_transits": [
            {
                "connector_id": connector.connector_id,
                "area_id_a": connector.area_id_a,
                "area_id_b": connector.area_id_b,
            }
            for connector in pending_connectors
        ],
    }


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/engine/get_state",
        vol.Required("entry_id"): str,
    }
)
@callback
def websocket_get_engine_state(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return live per-Area occupancy belief (SPEC.md §7.3's explainability inspector)."""
    entry = _resolve_entry(hass, connection, msg)
    if entry is None:
        return
    connection.send_result(msg["id"], _engine_state_json(entry.runtime_data.engine))


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/engine/subscribe_updates",
        vol.Required("entry_id"): str,
    }
)
@callback
def websocket_subscribe_engine_state(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Push a fresh engine-state snapshot to the client on every belief-state change.

    Mirrors core `websocket_api.commands.handle_subscribe_events`'s own pattern (verified from
    source): an initial `send_result()` with no data acks the subscription itself — the panel
    already gets its first snapshot from a plain `engine/get_state` call, so no initial event is
    needed here, only the ongoing push. Registering the unsubscribe callable into
    `connection.subscriptions` (the same dict core's own subscribe commands use) is what makes the
    frontend's generic `unsubscribe_events` — sent automatically by `hass.connection.
    subscribeMessage()`'s returned unsubscribe function, verified against the home-assistant-
    js-websocket source — tear this down correctly, with no bespoke unsubscribe command needed.
    """
    entry = _resolve_entry(hass, connection, msg)
    if entry is None:
        return

    engine = entry.runtime_data.engine

    @callback
    def _forward_update() -> None:
        connection.send_event(msg["id"], _engine_state_json(engine))

    remove_listener = engine.add_listener(_forward_update)
    removed = False

    @callback
    def _cleanup() -> None:
        nonlocal removed
        if not removed:
            removed = True
            remove_listener()

    connection.subscriptions[msg["id"]] = _cleanup
    # A reload replaces runtime_data.engine with a brand-new instance, so this
    # subscription (tied to *this* one) must stop forwarding when the entry
    # unloads — not just when the browser disconnects, since the websocket
    # connection itself outlives any single config-entry reload.
    entry.async_on_unload(_cleanup)

    connection.send_result(msg["id"])


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/topology/save",
        vol.Required("entry_id"): str,
        vol.Required("connectors"): [_CONNECTOR_SCHEMA],
        vol.Required("egress_points"): [_EGRESS_POINT_SCHEMA],
        vol.Required("area_entity_selections"): {str: [str]},
        vol.Required("area_positions"): {str: _AREA_POSITION_SCHEMA},
        vol.Required("outside_position"): _OUTSIDE_POSITION_SCHEMA,
    }
)
@require_admin
@async_response
async def websocket_save_topology(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Validate, persist, and (if engine-relevant) reload a full topology replacement.

    Delegates to `topology_store.async_replace_topology`, shared with the
    topology-import service (`services.py`) — reloads the entry (rather than
    trying to live-patch the running engine/signal ingestion) the same
    "next reload" mechanism the options flow already uses (docs/DECISIONS.md,
    Phase 6) — *but only if* something the engine actually cares about
    changed. `area_positions` is a pure display concern the
    engine/signal-ingestion/entity platforms never read, and the topology
    editor panel saves it on every node drag (for a responsive, no-explicit-
    "save"-button feel) — reloading the whole entry, with the brief
    entity-unavailable window that causes, on every drag would be pure churn
    for a change nothing downstream observes.
    """
    entry = _resolve_entry(hass, connection, msg)
    if entry is None:
        return

    topology = topology_from_dict(
        {
            "connectors": msg["connectors"],
            "egress_points": msg["egress_points"],
            "area_entity_selections": msg["area_entity_selections"],
            "area_positions": msg["area_positions"],
            "outside_position": msg["outside_position"],
        }
    )
    errors = await async_replace_topology(hass, entry, topology)
    if errors:
        connection.send_error(msg["id"], ERR_INVALID_FORMAT, "; ".join(errors))
        return
    connection.send_result(msg["id"], {"topology": topology_to_dict(topology)})
