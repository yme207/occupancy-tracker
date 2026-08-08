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

from .const import DOMAIN
from .registry_sync import HouseShape
from .topology_store import TopologyData, topology_from_dict, topology_to_dict

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


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the topology editor's websocket commands."""
    websocket_api.async_register_command(hass, websocket_get_topology)
    websocket_api.async_register_command(hass, websocket_save_topology)


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
                "device_id": entity.device_id,
                "area_id": entity.area_id,
                "platform": entity.platform,
                "disabled": entity.disabled,
                "hidden": entity.hidden,
            }
            for entity in house_shape.entities.values()
        ],
    }


def _topology_validation_errors(topology: TopologyData, house_shape: HouseShape) -> list[str]:
    """Reject references to Areas/entities the live registries don't have.

    Unlike `TopologyStore.reconcile()` (which silently drops stale
    references after a *registry* change, §5.3), a save request the editor
    itself submits should never contain one in the first place — surfacing
    it as a rejected save is more useful to the person editing than quietly
    discarding part of what they just drew.
    """
    errors: list[str] = []
    connector_ids: set[str] = set()
    for connector in topology.connectors:
        if connector.connector_id in connector_ids:
            errors.append(f"Duplicate connector_id: {connector.connector_id}")
        connector_ids.add(connector.connector_id)
        if connector.area_id_a == connector.area_id_b:
            errors.append(
                f"Connector {connector.connector_id} connects area {connector.area_id_a} to itself"
            )
        for area_id in (connector.area_id_a, connector.area_id_b):
            if area_id not in house_shape.areas:
                errors.append(
                    f"Connector {connector.connector_id} references unknown area {area_id}"
                )

    for egress in topology.egress_points:
        if egress.area_id not in house_shape.areas:
            errors.append(f"Egress point references unknown area {egress.area_id}")
        if not egress.entity_ids:
            errors.append(f"Egress point for area {egress.area_id} has no crossing entities")
        for entity_id in egress.entity_ids:
            if entity_id not in house_shape.entities:
                errors.append(
                    f"Egress point for area {egress.area_id} references unknown entity {entity_id}"
                )

    for area_id, entity_ids in topology.area_entity_selections.items():
        if area_id not in house_shape.areas:
            errors.append(f"Entity selection references unknown area {area_id}")
        for entity_id in entity_ids:
            entity = house_shape.entities.get(entity_id)
            if entity is None:
                errors.append(
                    f"Entity selection for area {area_id} references unknown entity {entity_id}"
                )
            elif entity.area_id != area_id:
                errors.append(
                    f"Entity selection for area {area_id} references entity {entity_id}, "
                    f"which is actually in area {entity.area_id}"
                )

    for area_id in topology.area_positions:
        if area_id not in house_shape.areas:
            errors.append(f"Node position references unknown area {area_id}")

    return errors


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


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/topology/save",
        vol.Required("entry_id"): str,
        vol.Required("connectors"): [_CONNECTOR_SCHEMA],
        vol.Required("egress_points"): [_EGRESS_POINT_SCHEMA],
        vol.Required("area_entity_selections"): {str: [str]},
        vol.Required("area_positions"): {str: _AREA_POSITION_SCHEMA},
    }
)
@require_admin
@async_response
async def websocket_save_topology(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]
) -> None:
    """Validate and persist a full topology replacement.

    Reloads the entry afterward (rather than trying to live-patch the
    running engine/signal ingestion) — the same "next reload" mechanism the
    options flow already uses (docs/DECISIONS.md, Phase 6) — *but only if*
    something the engine actually cares about changed. `area_positions` is a
    pure display concern the engine/signal-ingestion/entity platforms never
    read, and the topology editor panel saves it on every node drag (for a
    responsive, no-explicit-"save"-button feel) — reloading the whole entry,
    with the brief entity-unavailable window that causes, on every drag
    would be pure churn for a change nothing downstream observes.
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
        }
    )
    house_shape = entry.runtime_data.registry_sync.house_shape
    errors = _topology_validation_errors(topology, house_shape)
    if errors:
        connection.send_error(msg["id"], ERR_INVALID_FORMAT, "; ".join(errors))
        return

    previous = entry.runtime_data.topology_store.topology
    engine_relevant_change = (
        topology.connectors != previous.connectors
        or topology.egress_points != previous.egress_points
        or dict(topology.area_entity_selections) != dict(previous.area_entity_selections)
    )

    await entry.runtime_data.topology_store.async_save(topology)
    connection.send_result(msg["id"], {"topology": topology_to_dict(topology)})

    if engine_relevant_change:
        await hass.config_entries.async_reload(entry.entry_id)
