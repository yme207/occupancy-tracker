"""Contract tests for the topology editor's websocket API (docs/SPEC.md §7.3)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.topology_store import Connector, EgressPoint, TopologyData


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_get_topology_returns_house_shape_and_current_topology(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    motion_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen_motion", suggested_object_id="kitchen_motion"
    )
    entity_registry.async_update_entity(motion_entry.entity_id, area_id=kitchen.id)

    entry = await _setup_entry(hass)
    egress_point = EgressPoint(area_id=kitchen.id, entity_ids=(motion_entry.entity_id,))
    topology = TopologyData(
        egress_points=(egress_point,), area_positions={kitchen.id: (12.0, -4.5)}
    )
    await entry.runtime_data.topology_store.async_save(topology)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/topology/get", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()

    assert response["success"] is True
    area_ids = {area["area_id"] for area in response["result"]["house_shape"]["areas"]}
    assert kitchen.id in area_ids
    entity_ids = {e["entity_id"] for e in response["result"]["house_shape"]["entities"]}
    assert motion_entry.entity_id in entity_ids
    assert response["result"]["topology"]["egress_points"] == [
        {"area_id": kitchen.id, "entity_ids": [motion_entry.entity_id]}
    ]
    assert response["result"]["topology"]["area_positions"] == {kitchen.id: {"x": 12.0, "y": -4.5}}


async def test_get_topology_unknown_entry_id_errors(
    hass: HomeAssistant, hass_ws_client, enable_custom_integrations: None
) -> None:
    await _setup_entry(hass)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/topology/get", "entry_id": "not-a-real-entry"}
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_save_topology_persists_and_reloads_the_entry(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    entry = await _setup_entry(hass)
    runtime_data_before = entry.runtime_data

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "occupancy_tracker/topology/save",
            "entry_id": entry.entry_id,
            "connectors": [
                {"connector_id": "c1", "area_id_a": kitchen.id, "area_id_b": hallway.id}
            ],
            "egress_points": [],
            "area_entity_selections": {},
            "area_positions": {},
        }
    )
    response = await client.receive_json()
    await hass.async_block_till_done()

    assert response["success"] is True
    assert response["result"]["topology"]["connectors"] == [
        {"connector_id": "c1", "area_id_a": kitchen.id, "area_id_b": hallway.id}
    ]
    assert entry.runtime_data.topology_store.topology.connectors == (
        Connector("c1", kitchen.id, hallway.id),
    )
    # A structural change (a new connector) must reload the entry, since the
    # engine's graph is built once at setup from the topology at that time.
    assert entry.runtime_data is not runtime_data_before


async def test_save_topology_position_only_change_does_not_reload_the_entry(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    entry = await _setup_entry(hass)
    runtime_data_before = entry.runtime_data

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "occupancy_tracker/topology/save",
            "entry_id": entry.entry_id,
            "connectors": [],
            "egress_points": [],
            "area_entity_selections": {},
            "area_positions": {kitchen.id: {"x": 100.0, "y": 50.0}},
        }
    )
    response = await client.receive_json()
    await hass.async_block_till_done()

    assert response["success"] is True
    assert entry.runtime_data.topology_store.topology.area_positions == {kitchen.id: (100.0, 50.0)}
    # area_positions is a pure display concern the engine never reads —
    # reloading the entry for it would just be unnecessary churn.
    assert entry.runtime_data is runtime_data_before


async def test_save_topology_rejects_reference_to_unknown_area(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    entry = await _setup_entry(hass)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "occupancy_tracker/topology/save",
            "entry_id": entry.entry_id,
            "connectors": [
                {"connector_id": "c1", "area_id_a": kitchen.id, "area_id_b": "area.nonexistent"}
            ],
            "egress_points": [],
            "area_entity_selections": {},
            "area_positions": {},
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"
    assert entry.runtime_data.topology_store.topology.connectors == ()


async def test_save_topology_rejects_position_referencing_unknown_area(
    hass: HomeAssistant, hass_ws_client, enable_custom_integrations: None
) -> None:
    entry = await _setup_entry(hass)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "occupancy_tracker/topology/save",
            "entry_id": entry.entry_id,
            "connectors": [],
            "egress_points": [],
            "area_entity_selections": {},
            "area_positions": {"area.nonexistent": {"x": 0.0, "y": 0.0}},
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"
    assert entry.runtime_data.topology_store.topology.area_positions == {}


async def test_save_topology_requires_admin(
    hass: HomeAssistant,
    hass_ws_client,
    hass_read_only_access_token: str,
    enable_custom_integrations: None,
) -> None:
    entry = await _setup_entry(hass)

    client = await hass_ws_client(access_token=hass_read_only_access_token)
    await client.send_json_auto_id(
        {
            "type": "occupancy_tracker/topology/save",
            "entry_id": entry.entry_id,
            "connectors": [],
            "egress_points": [],
            "area_entity_selections": {},
            "area_positions": {},
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"
