"""Tests for the topology editor's frontend panel registration (docs/SPEC.md §7.3).

The frontend JS itself (www/topology-panel.js) has no Python-testable
surface — these tests cover the backend wiring only: the panel and its
static path get registered, and re-registration on every config-entry
reload (which a websocket topology save triggers, see websocket_api.py)
doesn't raise. That last case is the one panel_custom.async_register_panel
itself would reject outright if panel.py's idempotency guard were missing.
"""

from __future__ import annotations

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_panel_is_registered_on_setup(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert frontend.async_panel_exists(hass, "occupancy_tracker")


async def test_static_path_serves_the_panel_module(
    hass: HomeAssistant, hass_client, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_client()
    response = await client.get("/occupancy_tracker_static/topology-panel.js")

    assert response.status == 200
    body = await response.text()
    assert "customElements.define" in body


async def test_reload_does_not_raise_on_duplicate_panel_registration(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert frontend.async_panel_exists(hass, "occupancy_tracker")
