"""Tests for the services module (docs/SPEC.md §8): manual occupant-count
override (an entity service, registered in sensor.py) and topology
export/import (services.py).
"""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.const import DOMAIN
from custom_components.occupancy_tracker.topology_store import (
    Connector,
    TopologyData,
    topology_to_dict,
)


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _setup_entry_with_tracked_kitchen(
    hass: HomeAssistant, area_registry: ar.AreaRegistry
) -> MockConfigEntry:
    """A Kitchen Area with at least one activity-evidence entity selected —
    otherwise it's untracked and has no `sensor.kitchen_occupant_count` at
    all (project-owner feedback: an Area with nothing selected gets no
    entities, see test_init.py's own tests for that behavior).
    """
    kitchen = area_registry.async_get_or_create("Kitchen")
    # RegistrySync's house shape is built purely from the entity registry
    # (registry_sync.py's _build_house_shape), not from bare states — the
    # selected entity has to actually be registered with this Area or
    # TopologyStore.reconcile() strips it as a dangling reference (see
    # test_init.py's identical registration pattern).
    entity_registry = er.async_get(hass)
    motion_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen-motion-unique-id", suggested_object_id="kitchen_motion"
    )
    entity_registry.async_update_entity(motion_entry.entity_id, area_id=kitchen.id)
    hass.states.async_set(motion_entry.entity_id, "off")
    entry = await _setup_entry(hass)
    await entry.runtime_data.topology_store.async_save(
        TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_set_occupant_count_overrides_area_sensor(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    await _setup_entry_with_tracked_kitchen(hass, area_registry)

    assert hass.states.get("sensor.kitchen_occupant_count").state == "0"

    await hass.services.async_call(
        DOMAIN,
        "set_occupant_count",
        {"entity_id": "sensor.kitchen_occupant_count", "count": 3},
        blocking=True,
    )

    state = hass.states.get("sensor.kitchen_occupant_count")
    assert state.state == "3"
    assert state.attributes["provenance"] == "user_confirmed"


async def test_set_occupant_count_rejects_negative_count(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    await _setup_entry_with_tracked_kitchen(hass, area_registry)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "set_occupant_count",
            {"entity_id": "sensor.kitchen_occupant_count", "count": -1},
            blocking=True,
        )


async def test_export_topology_returns_current_topology(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    entry = await _setup_entry(hass)
    topology = TopologyData(connectors=(Connector("c1", kitchen.id, hallway.id),))
    await entry.runtime_data.topology_store.async_save(topology)

    response = await hass.services.async_call(
        DOMAIN, "export_topology", {}, blocking=True, return_response=True
    )

    assert response["topology"] == topology_to_dict(topology)


async def test_export_topology_fails_after_entry_unloaded(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """The services are hass-global (registered once, not torn down on
    unload — see services.py's own idempotency guard), so calling one after
    the only entry is unloaded must fail cleanly rather than operate on a
    stale/nonexistent entry.
    """
    entry = await _setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(
            DOMAIN, "export_topology", {}, blocking=True, return_response=True
        )


async def test_import_topology_replaces_current_topology(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    entry = await _setup_entry(hass)
    topology = TopologyData(connectors=(Connector("c1", kitchen.id, hallway.id),))

    await hass.services.async_call(
        DOMAIN,
        "import_topology",
        {"topology": topology_to_dict(topology)},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert entry.runtime_data.topology_store.topology.connectors == topology.connectors


async def test_import_topology_rejects_unknown_area_reference(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    entry = await _setup_entry(hass)
    bad_connector = Connector("c1", "ghost_a", "ghost_b")
    bad_topology = topology_to_dict(TopologyData(connectors=(bad_connector,)))

    with pytest.raises(ServiceValidationError, match="unknown area"):
        await hass.services.async_call(
            DOMAIN, "import_topology", {"topology": bad_topology}, blocking=True
        )

    assert entry.runtime_data.topology_store.topology.connectors == ()
