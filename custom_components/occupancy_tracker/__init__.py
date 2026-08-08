"""The Occupancy Tracker integration.

Phase 0 scaffolding only — no entity platforms exist yet, so there is
nothing to forward setup to. Real setup (registry sync, topology store,
occupancy engine, entity platforms) lands in later phases; see
docs/STATUS.md for the build-phase plan.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Occupancy Tracker from a config entry."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return True
