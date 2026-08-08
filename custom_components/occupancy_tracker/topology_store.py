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
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .registry_sync import HouseShape

_LOGGER = logging.getLogger(__name__)

# Bumped whenever the persisted shape changes; _TopologyStorageStore's
# migration hook is where old data gets upgraded to match. 1.1 -> 1.2 added
# area_positions (manual node layout for the topology editor panel).
STORAGE_VERSION_MAJOR = 1
STORAGE_VERSION_MINOR = 2
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
