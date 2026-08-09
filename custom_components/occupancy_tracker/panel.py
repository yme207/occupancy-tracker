"""Registers the topology editor's frontend panel (docs/SPEC.md §7.3).

Backend-only wiring: serves the bundled `www/` directory as a static path and
registers a `panel_custom` panel that the frontend surfaces as its own
sidebar item (`sidebar_title`/`sidebar_icon`) so it's always one click away.
Deliberately *not* passed `config_panel_domain` (tried in an earlier
session): that parameter makes Settings → Devices & Services → Occupancy
Tracker → Configure open this panel *instead of* the options flow — with no
other way to reach the options flow at all (verified against the frontend's
own `ha-config-entry-row.ts`: the row's "Configure" gear picks one or the
other, and the overflow menu has no separate "Options" entry as a fallback).
That silently made the zone-fusion/tunable settings (`config_flow.py`)
unreachable through the UI the moment `config_panel_domain` was added, since
the sidebar entry (which *does* reach this panel) was the only surviving
path. Leaving `config_panel_domain` unset restores the options flow to the
gear icon — the standard, expected place for it — while the sidebar item
remains the primary, more-discoverable way to reach the topology panel. The
panel itself talks only to the websocket API in `websocket_api.py`; this
module never touches the topology store directly (docs/ARCHITECTURE.md
§1.6).

Registration must happen at most once per HA runtime, not once per
config-entry setup: `panel_custom.async_register_panel()` raises if called
twice for the same `frontend_url_path` (verified from
`homeassistant/components/frontend/__init__.py`'s
`async_register_built_in_panel`, which only tolerates a re-registration when
called with `update=True` — a parameter `panel_custom`'s wrapper never
passes), and `async_setup_entry` re-runs on every config-entry reload
(including the reload `websocket_api.py` triggers after every topology
save). The `hass.data` sentinel below is what makes this idempotent across
those reloads.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.http.server import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_DATA_PANEL_REGISTERED = f"{DOMAIN}_panel_registered"
_STATIC_URL_PATH = f"/{DOMAIN}_static"
_PANEL_MODULE = "topology-panel.js"
_WEBCOMPONENT_NAME = "occupancy-tracker-topology-panel"


async def async_setup(hass: HomeAssistant) -> None:
    """Serve `www/` and register the topology editor panel, once per runtime."""
    if hass.data.get(_DATA_PANEL_REGISTERED):
        return
    hass.data[_DATA_PANEL_REGISTERED] = True

    www_path = Path(__file__).parent / "www"
    await hass.http.async_register_static_paths([StaticPathConfig(_STATIC_URL_PATH, str(www_path))])

    await async_register_panel(
        hass,
        frontend_url_path=DOMAIN,
        webcomponent_name=_WEBCOMPONENT_NAME,
        module_url=f"{_STATIC_URL_PATH}/{_PANEL_MODULE}",
        sidebar_title="Occupancy Tracker",
        sidebar_icon="mdi:home-search",
        require_admin=True,
    )
