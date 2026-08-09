"""Topology store.

Persists the one thing that's genuinely user-authored (docs/SPEC.md §5.4,
docs/ARCHITECTURE.md §1.2): Connectors between Areas, egress-point bindings,
and per-area entity selections. Everything else about "the house" comes from
the registry sync layer (registry_sync.py) and is never duplicated here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .registry_sync import HouseShape

if TYPE_CHECKING:
    from . import OccupancyTrackerConfigEntry

_LOGGER = logging.getLogger(__name__)

# Bumped whenever the persisted shape changes; _TopologyStorageStore's
# migration hook is where old data gets upgraded to match. 1.1 -> 1.2 added
# area_positions (manual node layout for the topology editor panel). 1.2 ->
# 1.3 added outside_position (manual position for the synthesized "Outside"
# node the panel draws once at least one egress point exists).
STORAGE_VERSION_MAJOR = 1
STORAGE_VERSION_MINOR = 3
_STORAGE_KEY_PREFIX = "occupancy_tracker_topology"


@dataclass(frozen=True, slots=True)
class Connector:
    """A user-drawn passage between two Areas."""

    connector_id: str
    area_id_a: str
    area_id_b: str


@dataclass(frozen=True, slots=True)
class EgressPoint:
    """An Area flagged as a house boundary, with its crossing entities."""

    area_id: str
    entity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TopologyData:
    """The full persisted, user-authored topology."""

    connectors: tuple[Connector, ...] = ()
    egress_points: tuple[EgressPoint, ...] = ()
    area_entity_selections: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: Manually-placed node positions for the topology editor panel, keyed by
    #: area_id. An area with no entry here is auto-arranged by the panel
    #: instead — this is purely a display concern, never read by the engine.
    area_positions: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: Manually-placed position for the panel's synthesized "Outside" node
    #: (there is no real Area for it to key against). `None` means the panel
    #: should fall back to its own computed default. Purely a display
    #: concern, like `area_positions` — never read by the engine.
    outside_position: tuple[float, float] | None = None


class ConnectorDict(TypedDict):
    connector_id: str
    area_id_a: str
    area_id_b: str


class EgressPointDict(TypedDict):
    area_id: str
    entity_ids: list[str]


class PositionDict(TypedDict):
    x: float
    y: float


class TopologyDict(TypedDict):
    connectors: list[ConnectorDict]
    egress_points: list[EgressPointDict]
    area_entity_selections: dict[str, list[str]]
    area_positions: dict[str, PositionDict]
    outside_position: PositionDict | None


def topology_to_dict(topology: TopologyData) -> TopologyDict:
    """Convert to a plain JSON-serializable dict.

    Shared by `Store` persistence and the websocket API (`websocket_api.py`)
    — both want the same plain-dict shape, so this is the one place that
    defines it rather than each caller re-deriving its own.
    """
    return {
        "connectors": [
            {
                "connector_id": connector.connector_id,
                "area_id_a": connector.area_id_a,
                "area_id_b": connector.area_id_b,
            }
            for connector in topology.connectors
        ],
        "egress_points": [
            {"area_id": egress.area_id, "entity_ids": list(egress.entity_ids)}
            for egress in topology.egress_points
        ],
        "area_entity_selections": {
            area_id: list(entity_ids)
            for area_id, entity_ids in topology.area_entity_selections.items()
        },
        "area_positions": {
            area_id: {"x": x, "y": y} for area_id, (x, y) in topology.area_positions.items()
        },
        "outside_position": (
            {"x": topology.outside_position[0], "y": topology.outside_position[1]}
            if topology.outside_position is not None
            else None
        ),
    }


def topology_from_dict(data: TopologyDict) -> TopologyData:
    """Inverse of `topology_to_dict` — also used to parse a validated websocket save payload."""
    return TopologyData(
        connectors=tuple(
            Connector(
                connector_id=connector["connector_id"],
                area_id_a=connector["area_id_a"],
                area_id_b=connector["area_id_b"],
            )
            for connector in data["connectors"]
        ),
        egress_points=tuple(
            EgressPoint(area_id=egress["area_id"], entity_ids=tuple(egress["entity_ids"]))
            for egress in data["egress_points"]
        ),
        area_entity_selections=MappingProxyType(
            {
                area_id: tuple(entity_ids)
                for area_id, entity_ids in data["area_entity_selections"].items()
            }
        ),
        area_positions=MappingProxyType(
            {
                area_id: (position["x"], position["y"])
                for area_id, position in data["area_positions"].items()
            }
        ),
        outside_position=(
            (data["outside_position"]["x"], data["outside_position"]["y"])
            if data["outside_position"] is not None
            else None
        ),
    )


class _TopologyStorageStore(Store[TopologyDict]):
    """`Store` subclass carrying the schema-migration hook.

    Mirrors the pattern Home Assistant core itself uses for its own
    registries (e.g. `helpers/floor_registry.py`'s `FloorRegistryStore`):
    override `_async_migrate_func`, which `Store.async_load()` calls
    whenever the on-disk major/minor version doesn't match this class's
    declared version (verified: `storage.py`'s `Store.async_load` docstring).
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> TopologyDict:
        if old_major_version > STORAGE_VERSION_MAJOR:
            raise ValueError(
                f"Occupancy Tracker topology store data is version "
                f"{old_major_version}.{old_minor_version}, newer than this integration "
                f"supports ({STORAGE_VERSION_MAJOR}.{STORAGE_VERSION_MINOR})"
            )
        if old_major_version == 1 and old_minor_version < 2:
            # 1.2 added area_positions; older data simply never had any
            # manually-placed nodes, so an empty map is the correct default,
            # not a guess.
            old_data.setdefault("area_positions", {})
        if old_major_version == 1 and old_minor_version < 3:
            # 1.3 added outside_position; older data never had a manually-
            # placed "Outside" node, so None (fall back to the panel's
            # computed default) is the correct default, not a guess.
            old_data.setdefault("outside_position", None)
        return old_data  # type: ignore[return-value]


class TopologyStore:
    """Loads, saves, and reconciles the user-authored topology."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = _TopologyStorageStore(
            hass,
            STORAGE_VERSION_MAJOR,
            f"{_STORAGE_KEY_PREFIX}_{entry_id}",
            minor_version=STORAGE_VERSION_MINOR,
        )
        self._topology = TopologyData()

    @property
    def topology(self) -> TopologyData:
        """Return the current in-memory topology."""
        return self._topology

    async def async_load(self) -> None:
        """Load the persisted topology, if any."""
        stored = await self._store.async_load()
        if stored is not None:
            self._topology = topology_from_dict(stored)

    async def async_save(self, topology: TopologyData) -> None:
        """Persist a new topology and make it the current one."""
        self._topology = topology
        await self._store.async_save(topology_to_dict(topology))

    def reconcile(self, house_shape: HouseShape) -> tuple[TopologyData, list[str]]:
        """Strip references to Areas/entities that no longer exist.

        Returns the cleaned topology and a human-readable list of what was
        removed and why — SPEC.md §5.3 requires broken topology references
        be surfaced clearly, never silently dropped or left to corrupt saved
        state.
        """
        removed: list[str] = []
        current = self._topology

        connectors: list[Connector] = []
        for connector in current.connectors:
            if (
                connector.area_id_a not in house_shape.areas
                or connector.area_id_b not in house_shape.areas
            ):
                removed.append(
                    f"Connector {connector.connector_id} removed: area "
                    f"{connector.area_id_a} or {connector.area_id_b} no longer exists"
                )
                continue
            connectors.append(connector)

        egress_points: list[EgressPoint] = []
        for egress in current.egress_points:
            if egress.area_id not in house_shape.areas:
                removed.append(
                    f"Egress point for area {egress.area_id} removed: area no longer exists"
                )
                continue
            remaining_entities = tuple(
                entity_id for entity_id in egress.entity_ids if entity_id in house_shape.entities
            )
            if not remaining_entities:
                removed.append(
                    f"Egress point for area {egress.area_id} removed: all its crossing "
                    "entities were removed"
                )
                continue
            if remaining_entities != egress.entity_ids:
                dropped = sorted(set(egress.entity_ids) - set(remaining_entities))
                removed.append(
                    f"Egress point for area {egress.area_id}: removed missing entities {dropped}"
                )
            egress_points.append(EgressPoint(area_id=egress.area_id, entity_ids=remaining_entities))

        area_entity_selections: dict[str, tuple[str, ...]] = {}
        for area_id, entity_ids in current.area_entity_selections.items():
            if area_id not in house_shape.areas:
                removed.append(
                    f"Entity selection for area {area_id} removed: area no longer exists"
                )
                continue
            remaining = tuple(
                entity_id
                for entity_id in entity_ids
                if entity_id in house_shape.entities
                and house_shape.entities[entity_id].area_id == area_id
            )
            if remaining != entity_ids:
                dropped = sorted(set(entity_ids) - set(remaining))
                removed.append(
                    f"Entity selection for area {area_id}: removed entities no longer "
                    f"in that area {dropped}"
                )
            if remaining:
                area_entity_selections[area_id] = remaining

        area_positions: dict[str, tuple[float, float]] = {}
        for area_id, position in current.area_positions.items():
            if area_id not in house_shape.areas:
                removed.append(f"Node position for area {area_id} removed: area no longer exists")
                continue
            area_positions[area_id] = position

        cleaned = TopologyData(
            connectors=tuple(connectors),
            egress_points=tuple(egress_points),
            area_entity_selections=MappingProxyType(area_entity_selections),
            area_positions=MappingProxyType(area_positions),
            outside_position=current.outside_position,
        )
        return cleaned, removed

    async def async_reconcile_and_save(self, house_shape: HouseShape) -> list[str]:
        """Reconcile against the current house shape, persisting only if it changed."""
        cleaned, removed = self.reconcile(house_shape)
        if removed:
            for message in removed:
                _LOGGER.warning("Occupancy Tracker topology reconciliation: %s", message)
            await self.async_save(cleaned)
        return removed


def active_area_ids(topology: TopologyData) -> frozenset[str]:
    """Areas the user has actually opted into tracking: at least one selected
    activity-evidence entity, or flagged as an access point (which itself
    requires at least one crossing entity — SPEC.md §7.3's own validation).

    Entity platforms (`sensor.py`/`binary_sensor.py`) use this to only create
    per-Area entities for Areas with real signal — project-owner feedback:
    an Area with nothing selected is one the user has implicitly said they
    don't want tracked, and a permanently-zero, never-updating sensor for it
    is clutter, not a useful default. The occupancy *engine* still tracks
    every Area regardless (`engine_adapter.build_house_graph` — a
    sensor-less Area can still be a valid pass-through node for transit
    inference, SPEC.md §5.1), so this only ever affects what's exposed as HA
    entities, never what the engine reasons over.
    """
    with_evidence = {
        area_id for area_id, entity_ids in topology.area_entity_selections.items() if entity_ids
    }
    access_points = {egress.area_id for egress in topology.egress_points}
    return frozenset(with_evidence | access_points)


def validate_topology(topology: TopologyData, house_shape: HouseShape) -> list[str]:
    """Reject references to Areas/entities the live registries don't have.

    Shared by the websocket save command (`websocket_api.py`) and the
    topology-import service (`services.py`) — docs/ARCHITECTURE.md's
    anti-duplication rule. Unlike `TopologyStore.reconcile()` (which silently
    drops stale references after a *registry* change, SPEC.md §5.3), a
    topology submitted through either of those entry points should never
    contain one in the first place; surfacing it as a rejected write is more
    useful to whoever/whatever submitted it than quietly discarding part of
    it.
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


async def async_replace_topology(
    hass: HomeAssistant, entry: OccupancyTrackerConfigEntry, topology: TopologyData
) -> list[str]:
    """Validate, persist, and (if engine-relevant) reload for a full topology replacement.

    Shared by `websocket_api.py`'s save command and `services.py`'s
    topology-import service, so "validate against the live house shape, then
    persist, then reload only if something the engine actually cares about
    changed" lives in exactly one place rather than being re-derived per
    caller (docs/ARCHITECTURE.md's anti-duplication rule). Returns a list of
    validation errors — empty on success, with nothing persisted or reloaded
    when non-empty.
    """
    house_shape = entry.runtime_data.registry_sync.house_shape
    errors = validate_topology(topology, house_shape)
    if errors:
        return errors

    previous = entry.runtime_data.topology_store.topology
    engine_relevant_change = (
        topology.connectors != previous.connectors
        or topology.egress_points != previous.egress_points
        or dict(topology.area_entity_selections) != dict(previous.area_entity_selections)
    )

    await entry.runtime_data.topology_store.async_save(topology)
    if engine_relevant_change:
        await hass.config_entries.async_reload(entry.entry_id)
    return []
