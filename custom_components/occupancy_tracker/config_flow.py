"""Config flow for Occupancy Tracker.

Setup is confirmation-only (SPEC.md §7.1) — rooms, devices, and entities are
discovered from HA's own registries, not entered here. Topology and egress
points are configured later via the options flow / topology editor (§7.3),
not part of this initial flow.

The options flow (added for Phase 6) holds the two zone-presence-fusion
settings SPEC.md §7.2 calls out as user-picked: which person/device_tracker
entities to fuse, and which zones count as "near the house" for pre-arming.
A plain `OptionsFlow` subclass is used rather than the declarative
`SchemaConfigFlowHandler` framework (which `derivative`/`threshold` use) —
consistent with the Phase 0 decision to keep this integration's flows in the
simpler, explicit `ConfigFlow`/`OptionsFlow` style (see docs/DECISIONS.md)
and because it doesn't require restructuring the existing confirmation-only
`ConfigFlow` into that framework's combined config+options shape.
"""

from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_NEAR_HOUSE_ZONES, CONF_TRACKED_PERSONS, DOMAIN


class OccupancyTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Occupancy Tracker."""

    VERSION = 1

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the initial confirmation step."""
        if user_input is None:
            return self.async_show_form(step_id="user")

        return self.async_create_entry(title="Occupancy Tracker", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OccupancyTrackerOptionsFlow()


class OccupancyTrackerOptionsFlow(OptionsFlow):
    """Zone-presence-fusion settings (SPEC.md §6.7, §7.2)."""

    # No @override: async_step_init is dispatched dynamically by step_id
    # convention (options flows start at "init"), not inherited from a base
    # class method — confirmed via mypy itself, which correctly rejects
    # @override here since no such base method exists.
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle the (only) options step."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TRACKED_PERSONS, default=options.get(CONF_TRACKED_PERSONS, [])
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["person", "device_tracker"], multiple=True
                    )
                ),
                vol.Optional(
                    CONF_NEAR_HOUSE_ZONES, default=options.get(CONF_NEAR_HOUSE_ZONES, [])
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="zone", multiple=True)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
