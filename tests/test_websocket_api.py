"""Contract tests for the topology editor's websocket API (docs/SPEC.md §7.3)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker.engine_adapter import egress_connector_id
from custom_components.occupancy_tracker.occupancy_engine import ConnectorActivitySignal
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
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": [],
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
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": [],
        }
    )
    response = await client.receive_json()
    await hass.async_block_till_done()

    assert response["success"] is True
    assert entry.runtime_data.topology_store.topology.area_positions == {kitchen.id: (100.0, 50.0)}
    # area_positions is a pure display concern the engine never reads —
    # reloading the entry for it would just be unnecessary churn.
    assert entry.runtime_data is runtime_data_before


async def test_save_topology_outside_position_only_change_does_not_reload_the_entry(
    hass: HomeAssistant,
    hass_ws_client,
    enable_custom_integrations: None,
) -> None:
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
            "area_positions": {},
            "outside_position": {"x": -20.0, "y": 15.0},
            "area_kind_overrides": {},
            "outside_area_ids": [],
        }
    )
    response = await client.receive_json()
    await hass.async_block_till_done()

    assert response["success"] is True
    assert entry.runtime_data.topology_store.topology.outside_position == (-20.0, 15.0)
    # Like area_positions, outside_position is a pure display concern the
    # engine never reads — reloading the entry for it would be pure churn.
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
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": [],
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
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": [],
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
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": [],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"


async def test_save_topology_persists_area_kind_override_and_reloads_the_entry(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    hallway = area_registry.async_get_or_create("Hallway")
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
            "area_positions": {},
            "outside_position": None,
            "area_kind_overrides": {hallway.id: "transit"},
            "outside_area_ids": [],
        }
    )
    response = await client.receive_json()
    await hass.async_block_till_done()

    assert response["success"] is True
    assert entry.runtime_data.topology_store.topology.area_kind_overrides == {hallway.id: "transit"}
    # Unlike area_positions/outside_position, an area-kind override changes
    # transit-timing behavior — it must reload, the same as a structural
    # topology change.
    assert entry.runtime_data is not runtime_data_before


async def test_save_topology_rejects_area_kind_override_referencing_unknown_area(
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
            "area_positions": {},
            "outside_position": None,
            "area_kind_overrides": {"area.nonexistent": "transit"},
            "outside_area_ids": [],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"
    assert entry.runtime_data.topology_store.topology.area_kind_overrides == {}


async def test_save_topology_rejects_invalid_area_kind_override_value(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    hallway = area_registry.async_get_or_create("Hallway")
    entry = await _setup_entry(hass)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {
            "type": "occupancy_tracker/topology/save",
            "entry_id": entry.entry_id,
            "connectors": [],
            "egress_points": [],
            "area_entity_selections": {},
            "area_positions": {},
            "outside_position": None,
            "area_kind_overrides": {hallway.id: "not-a-real-kind"},
            "outside_area_ids": [],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False


async def test_save_topology_persists_outside_area_flag_and_reloads_the_entry(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    enable_custom_integrations: None,
) -> None:
    front_yard = area_registry.async_get_or_create("Front Yard")
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
            "area_positions": {},
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": [front_yard.id],
        }
    )
    response = await client.receive_json()
    await hass.async_block_till_done()

    assert response["success"] is True
    assert entry.runtime_data.topology_store.topology.outside_area_ids == {front_yard.id}
    # Excluding an Area from the whole-house total is engine-relevant, same
    # as an area-kind override — it must reload.
    assert entry.runtime_data is not runtime_data_before


async def test_save_topology_rejects_outside_area_flag_referencing_unknown_area(
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
            "area_positions": {},
            "outside_position": None,
            "area_kind_overrides": {},
            "outside_area_ids": ["area.nonexistent"],
        }
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "invalid_format"
    assert entry.runtime_data.topology_store.topology.outside_area_ids == frozenset()


async def test_get_engine_state_returns_area_states_and_total(
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
    hass.states.async_set(motion_entry.entity_id, "off")
    await hass.async_block_till_done()

    entry = await _setup_entry(hass)
    topology = TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    await entry.runtime_data.topology_store.async_save(topology)
    # Signal ingestion's own subscriptions are a setup-time snapshot too
    # (docs/STATUS.md's Phase 4 scope note) — restarting it is what the real
    # topology-save flow does via the entry reload the engine-relevant-change
    # check triggers; mirrored directly here since this test isn't about
    # that reload path.
    entry.runtime_data.signal_ingestion.async_start(topology)

    hass.states.async_set(motion_entry.entity_id, "on")
    await hass.async_block_till_done()

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/engine/get_state", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()

    assert response["success"] is True
    area_state = response["result"]["areas"][kitchen.id]
    assert area_state["occupant_count"] == 1
    assert area_state["quality"] == "CONFIRMED"
    assert area_state["last_provenance"] == "AMBIGUOUS_PHYSICAL"
    assert area_state["last_confirmed"] is not None
    assert area_state["needs_review"] is False
    assert area_state["area_kind"] == "room"
    assert response["result"]["total_occupant_count"] == 1
    assert response["result"]["pending_transits"] == []


async def test_get_engine_state_reports_pending_transit(
    hass: HomeAssistant,
    hass_ws_client,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    entryway = area_registry.async_get_or_create("Entryway")
    door_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "front_door", suggested_object_id="front_door"
    )
    entity_registry.async_update_entity(door_entry.entity_id, area_id=entryway.id)

    entry = await _setup_entry(hass)
    topology = TopologyData(
        egress_points=(EgressPoint(area_id=entryway.id, entity_ids=(door_entry.entity_id,)),)
    )
    await entry.runtime_data.topology_store.async_save(topology)
    # A new egress point is engine-relevant (websocket_save_topology reloads
    # for it), so a real save always rebuilds the graph — reload directly
    # here since this test saves straight to the store, not via that command.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    connector_id = egress_connector_id(entryway.id)
    entry.runtime_data.engine.process_signal(
        ConnectorActivitySignal(connector_id, dt_util.utcnow(), source=door_entry.entity_id)
    )

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/engine/get_state", "entry_id": entry.entry_id}
    )
    response = await client.receive_json()

    assert response["success"] is True
    assert response["result"]["pending_transits"] == [
        {"connector_id": connector_id, "area_id_a": entryway.id, "area_id_b": "outside"}
    ]
    assert response["result"]["areas"][entryway.id]["quality"] == "AMBIGUOUS"


async def test_get_engine_state_unknown_entry_id_errors(
    hass: HomeAssistant, hass_ws_client, enable_custom_integrations: None
) -> None:
    await _setup_entry(hass)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/engine/get_state", "entry_id": "not-a-real-entry"}
    )
    response = await client.receive_json()

    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_subscribe_engine_state_pushes_update_on_signal(
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
    hass.states.async_set(motion_entry.entity_id, "off")
    await hass.async_block_till_done()

    entry = await _setup_entry(hass)
    topology = TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    await entry.runtime_data.topology_store.async_save(topology)
    entry.runtime_data.signal_ingestion.async_start(topology)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/engine/subscribe_updates", "entry_id": entry.entry_id}
    )
    ack = await client.receive_json()
    assert ack["success"] is True

    hass.states.async_set(motion_entry.entity_id, "on")
    await hass.async_block_till_done()

    pushed = await client.receive_json()
    assert pushed["type"] == "event"
    assert pushed["event"]["areas"][kitchen.id]["occupant_count"] == 1


async def test_subscribe_engine_state_stops_after_entry_reload(
    hass: HomeAssistant,
    hass_ws_client,
    enable_custom_integrations: None,
) -> None:
    entry = await _setup_entry(hass)
    engine_before = entry.runtime_data.engine
    # The entity platforms already register their own listener on the engine
    # (Phase 4's push-update mechanism) — baseline against that rather than
    # assuming ours is the only one.
    baseline_listener_count = len(engine_before._listeners)

    client = await hass_ws_client()
    await client.send_json_auto_id(
        {"type": "occupancy_tracker/engine/subscribe_updates", "entry_id": entry.entry_id}
    )
    ack = await client.receive_json()
    assert ack["success"] is True
    assert len(engine_before._listeners) == baseline_listener_count + 1

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # A full reload tears down everything tied to the *old* engine — both
    # the entity platforms' own listener and, via entry.async_on_unload,
    # this subscription's — leaving zero, not just back to baseline. What
    # this actually proves: the websocket subscription's cleanup really did
    # fire on unload rather than only on browser disconnect (the websocket
    # connection itself outlives any single config-entry reload, so nothing
    # else would have removed it).
    assert len(engine_before._listeners) == 0
