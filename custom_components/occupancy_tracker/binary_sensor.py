"""Binary sensor platform: per-Area occupied state (SPEC.md §8)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import OccupancyTrackerConfigEntry
from .occupancy_engine import OccupancyEngine


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OccupancyTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up per-Area occupied binary sensors for this config entry."""
    runtime_data = entry.runtime_data
    house_shape = runtime_data.registry_sync.house_shape

    async_add_entities(
        AreaOccupiedBinarySensor(entry.entry_id, runtime_data.engine, area.area_id, area.name)
        for area in house_shape.areas.values()
    )


class AreaOccupiedBinarySensor(BinarySensorEntity):
    """Whether an Area currently has any occupant (SPEC.md §8)."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(
        self, entry_id: str, engine: OccupancyEngine, area_id: str, area_name: str
    ) -> None:
        self._engine = engine
        self._area_id = area_id
        self._attr_unique_id = f"{entry_id}_{area_id}_occupied"
        self._attr_name = f"{area_name} Occupied"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._engine.add_listener(self._handle_engine_update))

    @callback
    def _handle_engine_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._engine.area_state(self._area_id, dt_util.utcnow()).occupant_count > 0

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        state = self._engine.area_state(self._area_id, dt_util.utcnow())
        attributes = {"quality": state.quality.name.lower()}
        if state.last_provenance is not None:
            attributes["provenance"] = state.last_provenance.name.lower()
        return attributes
