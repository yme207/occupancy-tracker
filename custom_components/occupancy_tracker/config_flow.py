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

from .const import (
    CONF_CLEAR_HOUSE_WHEN_ALL_AWAY,
    CONF_CONFIRMED_FRESHNESS_WINDOW,
    CONF_DECAY_GRACE_PERIOD,
    CONF_HOUSEHOLD_SIZE_HINT,
    CONF_LONG_LATCHED_REVIEW_THRESHOLD,
    CONF_NEAR_HOUSE_ZONES,
    CONF_PRE_ARM_WINDOW,
    CONF_TRACKED_PERSONS,
    CONF_TRANSIT_AREA_HOP_EXTENSION,
    CONF_TRANSIT_CONFIRMATION_WINDOW,
    CONF_UNCERTAIN_BIRTH_RESOLUTION_DELAY,
    CONF_ZONE_AWAY_CLEAR_DELAY,
    DOMAIN,
)


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
    """Zone-presence-fusion settings (SPEC.md §6.7, §7.2) plus the engine's
    tunable confidence/timing windows (SPEC.md §6.4, §7.2) — all the scalar
    settings SPEC.md §7.2 calls for as "standard HA options-flow forms," as
    opposed to the topology itself, which needs the graphical panel (§7.3).
    """

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
                # Optional whole-house confidence hint (SPEC.md §6.4) — no
                # default value at all (as opposed to a numeric default like
                # 0) so "unset" is a real, distinct state, not
                # indistinguishable from "hint of 0 people."
                vol.Optional(
                    CONF_HOUSEHOLD_SIZE_HINT,
                    description={"suggested_value": options.get(CONF_HOUSEHOLD_SIZE_HINT)},
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_TRANSIT_CONFIRMATION_WINDOW,
                    default=options.get(CONF_TRANSIT_CONFIRMATION_WINDOW, {"seconds": 90}),
                ): selector.DurationSelector(),
                vol.Optional(
                    CONF_TRANSIT_AREA_HOP_EXTENSION,
                    default=options.get(CONF_TRANSIT_AREA_HOP_EXTENSION, {"seconds": 60}),
                ): selector.DurationSelector(),
                vol.Optional(
                    CONF_DECAY_GRACE_PERIOD,
                    default=options.get(CONF_DECAY_GRACE_PERIOD, {"minutes": 5}),
                ): selector.DurationSelector(),
                vol.Optional(
                    CONF_LONG_LATCHED_REVIEW_THRESHOLD,
                    default=options.get(CONF_LONG_LATCHED_REVIEW_THRESHOLD, {"hours": 12}),
                ): selector.DurationSelector(),
                vol.Optional(
                    CONF_UNCERTAIN_BIRTH_RESOLUTION_DELAY,
                    default=options.get(CONF_UNCERTAIN_BIRTH_RESOLUTION_DELAY, {"minutes": 30}),
                ): selector.DurationSelector(),
                vol.Optional(
                    CONF_CONFIRMED_FRESHNESS_WINDOW,
                    default=options.get(CONF_CONFIRMED_FRESHNESS_WINDOW, {"minutes": 10}),
                ): selector.DurationSelector(),
                vol.Optional(
                    CONF_PRE_ARM_WINDOW,
                    default=options.get(CONF_PRE_ARM_WINDOW, {"minutes": 5}),
                ): selector.DurationSelector(),
                # Opt-in, defaults off (docs/DECISIONS.md's "zone-fusion
                # away-clear" entry) — only trustworthy when every person who
                # might be home is one of the trackers picked above; the
                # translation copy for this field spells that caveat out.
                vol.Optional(
                    CONF_CLEAR_HOUSE_WHEN_ALL_AWAY,
                    default=options.get(CONF_CLEAR_HOUSE_WHEN_ALL_AWAY, False),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_ZONE_AWAY_CLEAR_DELAY,
                    default=options.get(CONF_ZONE_AWAY_CLEAR_DELAY, {"minutes": 15}),
                ): selector.DurationSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
