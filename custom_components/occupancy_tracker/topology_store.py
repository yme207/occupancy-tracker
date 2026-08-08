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
# migration hook is where old data gets upgraded to match (see its
# docstring — there's no earlier schema yet since this is the topology
# store's first release).
STORAGE_VERSION_MAJOR = 1
STORAGE_VERSION_MINOR = 1
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


class _ConnectorData(TypedDict):
    connector_id: str
    area_id_a: str
    area_id_b: str


class _EgressPointData(TypedDict):
    area_id: str
    entity_ids: list[str]


class _TopologyStoreData(TypedDict):
    connectors: list[_ConnectorData]
    egress_points: list[_EgressPointData]
    area_entity_selections: dict[str, list[str]]


def _to_stored(topology: TopologyData) -> _TopologyStoreData:
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
    }


def _from_stored(data: _TopologyStoreData) -> TopologyData:
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
    )


class _TopologyStorageStore(Store[_TopologyStoreData]):
    """`Store` subclass carrying the schema-migration hook.

    Mirrors the pattern Home Assistant core itself uses for its own
    registries (e.g. `helpers/floor_registry.py`'s `FloorRegistryStore`):
    override `_async_migrate_func`, which `Store.async_load()` calls
    whenever the on-disk major/minor version doesn't match this class's
    declared version (verified: `storage.py`'s `Store.async_load` docstring).
    """

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> _TopologyStoreData:
        if old_major_version > STORAGE_VERSION_MAJOR:
            raise ValueError(
                f"Occupancy Tracker topology store data is version "
                f"{old_major_version}.{old_minor_version}, newer than this integration "
                f"supports ({STORAGE_VERSION_MAJOR}.{STORAGE_VERSION_MINOR})"
            )
        # No earlier schema exists yet (this is the topology store's first
        # release), so there's nothing to upgrade. Future minor-version field
        # additions get their defaulting logic added here, keyed on
        # old_minor_version.
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
            self._topology = _from_stored(stored)

    async def async_save(self, topology: TopologyData) -> None:
        """Persist a new topology and make it the current one."""
        self._topology = topology
        await self._store.async_save(_to_stored(topology))

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

        cleaned = TopologyData(
            connectors=tuple(connectors),
            egress_points=tuple(egress_points),
            area_entity_selections=MappingProxyType(area_entity_selections),
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
