"""Tests for the options flow (SPEC.md §6.7, §7.2)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.const import (
    CONF_CONFIRMED_FRESHNESS_WINDOW,
    CONF_HOUSEHOLD_SIZE_HINT,
    CONF_NEAR_HOUSE_ZONES,
    CONF_PRE_ARM_WINDOW,
    CONF_TRACKED_PERSONS,
    CONF_TRANSIT_CONFIRMATION_WINDOW,
)


async def test_options_flow_shows_form(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"


async def test_options_flow_saves_selections(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_TRACKED_PERSONS: ["person.alice"],
            CONF_NEAR_HOUSE_ZONES: ["zone.front_yard"],
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TRACKED_PERSONS] == ["person.alice"]
    assert entry.options[CONF_NEAR_HOUSE_ZONES] == ["zone.front_yard"]


async def test_options_flow_defaults_to_current_options(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(
        domain="occupancy_tracker", options={CONF_TRACKED_PERSONS: ["person.alice"]}
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    schema = result["data_schema"].schema
    tracked_persons_key = next(key for key in schema if key == CONF_TRACKED_PERSONS)
    assert tracked_persons_key.default() == ["person.alice"]


async def test_options_flow_saves_tunables(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """SPEC.md §7.2's "typical household size" hint and transit/confirmation
    windows must be settable through the options flow, not just the
    zone-fusion entity/zone pickers.
    """
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HOUSEHOLD_SIZE_HINT: 4,
            CONF_TRANSIT_CONFIRMATION_WINDOW: {"minutes": 2},
            CONF_CONFIRMED_FRESHNESS_WINDOW: {"minutes": 15},
            CONF_PRE_ARM_WINDOW: {"minutes": 7},
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HOUSEHOLD_SIZE_HINT] == 4
    # DurationSelector validates the submitted dict but returns it unchanged
    # (verified: selector.py's DurationSelector.__call__ discards
    # cv.positive_time_period_dict's normalized/expanded result and returns
    # the original `data` as-is) — so only whichever keys were actually
    # submitted round-trip, not a zero-filled hours/minutes/seconds dict.
    assert entry.options[CONF_TRANSIT_CONFIRMATION_WINDOW] == {"minutes": 2}
    assert entry.options[CONF_CONFIRMED_FRESHNESS_WINDOW] == {"minutes": 15}
    assert entry.options[CONF_PRE_ARM_WINDOW] == {"minutes": 7}


async def test_options_flow_household_size_hint_is_optional(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Leaving the hint blank must not force a value like 0 — "unset" (never
    tuning confidence at all) is a real, distinct state (SPEC.md §6.4).
    """
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_HOUSEHOLD_SIZE_HINT not in entry.options
