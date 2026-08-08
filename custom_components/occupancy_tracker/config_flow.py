"""Config flow for Occupancy Tracker.

Setup is confirmation-only (SPEC.md §7.1) — rooms, devices, and entities are
discovered from HA's own registries, not entered here. Topology and egress
points are configured later via the options flow / topology editor (§7.3),
not part of this initial flow.
"""

from typing import Any, override

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class OccupancyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Occupancy Tracker."""

    VERSION = 1

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial confirmation step."""
        if user_input is None:
            return self.async_show_form(step_id="user")

        return self.async_create_entry(title="Occupancy Tracker", data={})
