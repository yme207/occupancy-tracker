"""Registry sync layer.

Wraps Home Assistant's Area/Floor/Device/Entity registries behind a single,
HA-independent "house shape" snapshot (docs/ARCHITECTURE.md §1.1). Downstream
code (topology store, occupancy engine, websocket API) reads this model and
must never touch the HA registry helpers directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr

# Fired by HA core whenever the corresponding registry changes; the sync
# layer rebuilds its whole snapshot on any of them rather than trying to
# patch it incrementally (verified event shapes/names: area_registry.py:36,
# floor_registry.py:27, device_registry.py:62, entity_registry.py:70 in the
# installed homeassistant 2026.8.1 package).
_TRACKED_EVENTS = (
    ar.EVENT_AREA_REGISTRY_UPDATED,
    fr.EVENT_FLOOR_REGISTRY_UPDATED,
    dr.EVENT_DEVICE_REGISTRY_UPDATED,
    er.EVENT_ENTITY_REGISTRY_UPDATED,
)


@dataclass(frozen=True, slots=True)
class FloorSnapshot:
    """A Floor Registry entry, reduced to what this integration needs.

    ``level`` (verified: `helpers/floor_registry.py`'s `FloorEntry.level`) is
    the user's own floor ordering (e.g. -1 basement, 0 ground, 1 first) —
    display-only, same as the rest of a Floor (docs/DECISIONS.md "Floors are
    display-only"), but it's what lets the topology editor panel stack
    floors top-to-bottom in the right order instead of an arbitrary one.
    """

    floor_id: str
    name: str
    level: int | None


@dataclass(frozen=True, slots=True)
class AreaSnapshot:
    """An Area Registry entry, reduced to what this integration needs."""

    area_id: str
    name: str
    floor_id: str | None
    entity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """An Entity Registry entry, reduced to what this integration needs.

    ``area_id`` is pre-resolved: the entity's own area if set, otherwise its
    device's area. This mirrors the precedence Home Assistant itself uses
    (entity_registry.py's ``_async_get_full_entity_name``: entity area_id
    wins, falling back to the linked device's area_id only when None).
    """

    entity_id: str
    device_id: str | None
    area_id: str | None
    platform: str
    disabled: bool
    hidden: bool


@dataclass(frozen=True, slots=True)
class HouseShape:
    """A point-in-time snapshot of what the house currently looks like."""

    areas: Mapping[str, AreaSnapshot]
    floors: Mapping[str, FloorSnapshot]
    entities: Mapping[str, EntitySnapshot]


class RegistrySync:
    """Live, read-only view of the Area/Floor/Device/Entity registries."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Build the initial house-shape snapshot.

        Registry data is already loaded into memory by HA core before any
        config entry is set up, so this is a synchronous read, not I/O.
        """
        self._hass = hass
        self._house_shape = self._build_house_shape()
        self._listeners: list[Callable[[], None]] = []
        self._unsub_bus: list[CALLBACK_TYPE] = []

    @property
    def house_shape(self) -> HouseShape:
        """Return the current house-shape snapshot."""
        return self._house_shape

    @callback
    def async_setup(self) -> None:
        """Start listening for registry-update events."""
        for event_type in _TRACKED_EVENTS:
            self._unsub_bus.append(
                self._hass.bus.async_listen(event_type, self._handle_registry_event)
            )

    @callback
    def async_unload(self) -> None:
        """Stop listening for registry-update events."""
        for unsub in self._unsub_bus:
            unsub()
        self._unsub_bus.clear()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Register a callback invoked whenever the house shape changes."""
        self._listeners.append(listener)

        @callback
        def remove_listener() -> None:
            self._listeners.remove(listener)

        return remove_listener

    @callback
    def _handle_registry_event(self, event: Event[Any]) -> None:
        self._house_shape = self._build_house_shape()
        for listener in list(self._listeners):
            listener()

    def _build_house_shape(self) -> HouseShape:
        area_registry = ar.async_get(self._hass)
        floor_registry = fr.async_get(self._hass)
        device_registry = dr.async_get(self._hass)
        entity_registry = er.async_get(self._hass)

        floors = {
            floor.floor_id: FloorSnapshot(
                floor_id=floor.floor_id, name=floor.name, level=floor.level
            )
            for floor in floor_registry.async_list_floors()
        }

        entities: dict[str, EntitySnapshot] = {}
        entity_ids_by_area: dict[str, list[str]] = {}
        for entry in entity_registry.entities.values():
            resolved_area_id = entry.area_id
            if resolved_area_id is None and entry.device_id is not None:
                device = device_registry.async_get(entry.device_id)
                if device is not None:
                    resolved_area_id = device.area_id

            entities[entry.entity_id] = EntitySnapshot(
                entity_id=entry.entity_id,
                device_id=entry.device_id,
                area_id=resolved_area_id,
                platform=entry.platform,
                disabled=entry.disabled_by is not None,
                hidden=entry.hidden_by is not None,
            )
            if resolved_area_id is not None:
                entity_ids_by_area.setdefault(resolved_area_id, []).append(entry.entity_id)

        areas = {
            area.id: AreaSnapshot(
                area_id=area.id,
                name=area.name,
                floor_id=area.floor_id,
                entity_ids=tuple(sorted(entity_ids_by_area.get(area.id, ()))),
            )
            for area in area_registry.async_list_areas()
        }

        return HouseShape(
            areas=MappingProxyType(areas),
            floors=MappingProxyType(floors),
            entities=MappingProxyType(entities),
        )
