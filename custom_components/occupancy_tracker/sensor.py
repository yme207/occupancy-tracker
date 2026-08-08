"""Sensor platform: per-Area occupant count + house-level total (SPEC.md §8)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OccupancyTrackerConfigEntry
from .occupancy_engine import OccupancyEngine
from .zone_fusion import ZoneFusion


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OccupancyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up occupant-count sensors for this config entry."""
    runtime_data = entry.runtime_data
    house_shape = runtime_data.registry_sync.house_shape

    entities: list[SensorEntity] = [
        TotalOccupantCountSensor(entry.entry_id, runtime_data.engine, runtime_data.zone_fusion)
    ]
    entities.extend(
        AreaOccupantCountSensor(entry.entry_id, runtime_data.engine, area.area_id, area.name)
        for area in house_shape.areas.values()
    )
    async_add_entities(entities)


class _EngineBoundSensor(SensorEntity):
    """Common push-update wiring for engine-backed sensors.

    `should_poll = False` + a listener registered on the shared
    `OccupancyEngine` instance (never a private copy — docs/ARCHITECTURE.md
    §1.4-1.5) is how these stay current without a polling loop (SPEC.md §9).
    """

    _attr_should_poll = False

    def __init__(self, engine: OccupancyEngine) -> None:
        self._engine = engine

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._engine.add_listener(self._handle_engine_update))

    @callback
    def _handle_engine_update(self) -> None:
        self.async_write_ha_state()


class TotalOccupantCountSensor(_EngineBoundSensor):
    """Whole-house occupant total (SPEC.md §8), with zone-presence
    corroboration as an inspectable attribute (SPEC.md §6.7 — zone presence
    alone never changes this *value*, only informs confidence).
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-multiple"

    def __init__(self, entry_id: str, engine: OccupancyEngine, zone_fusion: ZoneFusion) -> None:
        super().__init__(engine)
        self._zone_fusion = zone_fusion
        self._attr_unique_id = f"{entry_id}_total_occupant_count"
        self._attr_name = "Total Occupant Count"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._zone_fusion.add_listener(self._handle_engine_update))

    @property
    def native_value(self) -> int:
        return self._engine.total_occupant_count(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"zone_corroboration": self._zone_fusion.house_zone_corroboration().name.lower()}


class AreaOccupantCountSensor(_EngineBoundSensor):
    """Per-Area occupant count, with state-quality as an inspectable attribute
    (SPEC.md §6.8 — "the UI/entity attributes must make it possible to see
    why it believes what it believes").
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account"

    def __init__(
        self, entry_id: str, engine: OccupancyEngine, area_id: str, area_name: str
    ) -> None:
        super().__init__(engine)
        self._area_id = area_id
        self._attr_unique_id = f"{entry_id}_{area_id}_occupant_count"
        self._attr_name = f"{area_name} Occupant Count"

    @property
    def native_value(self) -> int:
        return self._engine.area_state(self._area_id, dt_util.utcnow()).occupant_count

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        state = self._engine.area_state(self._area_id, dt_util.utcnow())
        attributes = {"quality": state.quality.name.lower()}
        if state.last_provenance is not None:
            attributes["provenance"] = state.last_provenance.name.lower()
        return attributes
