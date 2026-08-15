"""Integration-level services that aren't tied to a specific entity.

Manual occupant-count override (SPEC.md §8) is an *entity* service instead —
see `sensor.py`, registered via `entity_platform.async_register_entity_service`
so the user targets the specific room's sensor through HA's own entity
picker. Topology export/import (SPEC.md §8, "for backup or copying a config
between installs") has no natural entity to target, so it's registered here
as plain `hass.services.async_register` calls instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import selector

from .const import DOMAIN
from .topology_store import async_replace_topology, topology_from_dict, topology_to_dict

if TYPE_CHECKING:
    from . import OccupancyTrackerConfigEntry

SERVICE_EXPORT_TOPOLOGY = "export_topology"
SERVICE_IMPORT_TOPOLOGY = "import_topology"

_IMPORT_TOPOLOGY_SCHEMA = vol.Schema({vol.Required("topology"): selector.ObjectSelector()})


def _get_loaded_entry(hass: HomeAssistant) -> OccupancyTrackerConfigEntry:
    """Resolve the single loaded config entry.

    A plain (non-entity) service call has no entity/device to resolve a
    config entry from the way the websocket API's explicit `entry_id`
    parameter does — safe to just take "the" entry here specifically because
    `manifest.json` declares `single_config_entry: true`, so there is never
    more than one.
    """
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise ServiceValidationError("Occupancy Tracker is not set up")
    return loaded[0]


async def _async_require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    """Restrict a service to admin users, matching the topology editor's own gate.

    The topology panel (`panel.py`, `require_admin=True`) and the websocket
    save command (`websocket_api.py`'s `websocket_save_topology`, `@require_admin`)
    already restrict topology *editing* to admins. `import_topology` is an
    equally destructive full-topology overwrite reachable outside the panel
    (Developer Tools, an automation), so it needs the same gate directly —
    `helpers.service.verify_domain_control` doesn't fit (it checks per-entity
    domain control, not admin status, and is deprecated for removal in HA
    2026.10 anyway), so this mirrors its own internal admin-lookup pattern
    instead. A call with no `user_id` (e.g. from an automation triggered by
    the system itself, not a person) is left unrestricted, same as HA core's
    own equivalent checks treat that case.
    """
    if not call.context.user_id:
        return
    user = await hass.auth.async_get_user(call.context.user_id)
    if user is not None and not user.is_admin:
        raise Unauthorized(context=call.context)


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the topology export/import services.

    Hass-global, like `websocket_api.async_setup`/`panel.async_setup` —
    called on every config-entry setup (including a reload), so it must be
    idempotent; guarded by `has_service` rather than relying on
    `async_register`'s own (undocumented) behavior for a repeat call.
    """
    if hass.services.has_service(DOMAIN, SERVICE_EXPORT_TOPOLOGY):
        return

    async def _async_export_topology(call: ServiceCall) -> ServiceResponse:
        entry = _get_loaded_entry(hass)
        # TopologyDict is a TypedDict of plain JSON-safe data (see
        # topology_store.py) but doesn't structurally satisfy mypy's
        # recursive JsonValueType without a cast.
        topology = topology_to_dict(entry.runtime_data.topology_store.topology)
        return cast(ServiceResponse, {"topology": topology})

    async def _async_import_topology(call: ServiceCall) -> ServiceResponse:
        await _async_require_admin(hass, call)
        entry = _get_loaded_entry(hass)
        try:
            topology = topology_from_dict(call.data["topology"])
        except (KeyError, TypeError, AttributeError) as err:
            raise ServiceValidationError(f"Malformed topology payload: {err}") from err

        errors = await async_replace_topology(hass, entry, topology)
        if errors:
            raise ServiceValidationError("; ".join(errors))
        return {"applied": True}

    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_TOPOLOGY,
        _async_export_topology,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_TOPOLOGY,
        _async_import_topology,
        schema=_IMPORT_TOPOLOGY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
