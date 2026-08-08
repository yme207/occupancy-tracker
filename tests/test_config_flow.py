"""Tests for the options flow (SPEC.md §6.7, §7.2)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.const import CONF_NEAR_HOUSE_ZONES, CONF_TRACKED_PERSONS


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
