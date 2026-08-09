"""Tests for the topology store (docs/ARCHITECTURE.md §1.2, docs/SPEC.md §5.4/§7.4)."""

from __future__ import annotations

from types import MappingProxyType

import pytest
from homeassistant.core import HomeAssistant

from custom_components.occupancy_tracker.registry_sync import (
    AreaSnapshot,
    EntitySnapshot,
    HouseShape,
)
from custom_components.occupancy_tracker.topology_store import (
    STORAGE_VERSION_MAJOR,
    STORAGE_VERSION_MINOR,
    Connector,
    EgressPoint,
    TopologyData,
    TopologyStore,
    active_area_ids,
)


def _house_shape(
    area_ids: tuple[str, ...] = (), entities: dict[str, str | None] | None = None
) -> HouseShape:
    """Build a minimal HouseShape: area_ids that exist, entities as {entity_id: area_id}."""
    areas = {
        area_id: AreaSnapshot(area_id=area_id, name=area_id, floor_id=None, entity_ids=())
        for area_id in area_ids
    }
    entity_snapshots = {
        entity_id: EntitySnapshot(
            entity_id=entity_id,
            device_id=None,
            area_id=area_id,
            platform="test",
            disabled=False,
            hidden=False,
            name=entity_id,
        )
        for entity_id, area_id in (entities or {}).items()
    }
    return HouseShape(
        areas=MappingProxyType(areas),
        floors=MappingProxyType({}),
        entities=MappingProxyType(entity_snapshots),
    )


async def test_default_topology_is_empty(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    await store.async_load()

    assert store.topology == TopologyData()


async def test_save_and_load_roundtrip(hass: HomeAssistant) -> None:
    topology = TopologyData(
        connectors=(Connector("c1", "area_kitchen", "area_hallway"),),
        egress_points=(EgressPoint("area_hallway", ("binary_sensor.front_door",)),),
        area_entity_selections=MappingProxyType(
            {"area_kitchen": ("binary_sensor.kitchen_motion",)}
        ),
        area_positions=MappingProxyType({"area_kitchen": (12.5, -30.0)}),
        outside_position=(1.0, -50.0),
    )

    writer = TopologyStore(hass, "entry-1")
    await writer.async_save(topology)

    reader = TopologyStore(hass, "entry-1")
    await reader.async_load()

    assert reader.topology == topology


async def test_different_entries_do_not_share_storage(hass: HomeAssistant) -> None:
    topology = TopologyData(connectors=(Connector("c1", "a", "b"),))
    store_a = TopologyStore(hass, "entry-a")
    await store_a.async_save(topology)

    store_b = TopologyStore(hass, "entry-b")
    await store_b.async_load()

    assert store_b.topology == TopologyData()


async def test_migrate_func_passes_through_current_minor_version(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    raw = {
        "connectors": [],
        "egress_points": [],
        "area_entity_selections": {},
        "area_positions": {},
    }

    result = await store._store._async_migrate_func(
        STORAGE_VERSION_MAJOR, STORAGE_VERSION_MINOR, raw
    )

    assert result == raw


async def test_migrate_func_defaults_area_positions_for_pre_1_2_data(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    raw = {"connectors": [], "egress_points": [], "area_entity_selections": {}}

    result = await store._store._async_migrate_func(1, 1, raw)

    assert result["area_positions"] == {}


async def test_migrate_func_defaults_outside_position_for_pre_1_3_data(
    hass: HomeAssistant,
) -> None:
    store = TopologyStore(hass, "entry-1")
    raw = {
        "connectors": [],
        "egress_points": [],
        "area_entity_selections": {},
        "area_positions": {},
    }

    result = await store._store._async_migrate_func(1, 2, raw)

    assert result["outside_position"] is None


async def test_migrate_func_rejects_newer_major_version(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")

    with pytest.raises(ValueError, match="newer than this integration supports"):
        await store._store._async_migrate_func(STORAGE_VERSION_MAJOR + 1, 0, {})


async def test_reconcile_keeps_connector_when_both_areas_exist(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    connector = Connector("c1", "kitchen", "hallway")
    store._topology = TopologyData(connectors=(connector,))

    cleaned, removed = store.reconcile(_house_shape(("kitchen", "hallway")))

    assert cleaned.connectors == (connector,)
    assert removed == []


async def test_reconcile_drops_connector_referencing_missing_area(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(connectors=(Connector("c1", "kitchen", "hallway"),))

    cleaned, removed = store.reconcile(_house_shape(("kitchen",)))

    assert cleaned.connectors == ()
    assert len(removed) == 1
    assert "c1" in removed[0]


async def test_reconcile_drops_egress_point_referencing_missing_area(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(
        egress_points=(EgressPoint("hallway", ("binary_sensor.front_door",)),)
    )

    cleaned, removed = store.reconcile(_house_shape(("kitchen",)))

    assert cleaned.egress_points == ()
    assert removed


async def test_reconcile_filters_missing_egress_entities(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(
        egress_points=(
            EgressPoint("hallway", ("binary_sensor.front_door", "binary_sensor.side_door")),
        )
    )
    shape = _house_shape(
        ("hallway",), {"binary_sensor.front_door": "hallway"}
    )  # side_door entity no longer exists

    cleaned, removed = store.reconcile(shape)

    assert cleaned.egress_points == (EgressPoint("hallway", ("binary_sensor.front_door",)),)
    assert removed


async def test_reconcile_drops_egress_point_when_all_entities_missing(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(
        egress_points=(EgressPoint("hallway", ("binary_sensor.front_door",)),)
    )

    cleaned, removed = store.reconcile(_house_shape(("hallway",)))  # entity gone entirely

    assert cleaned.egress_points == ()
    assert removed


async def test_reconcile_drops_entity_selection_for_missing_area(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(
        area_entity_selections=MappingProxyType({"kitchen": ("binary_sensor.kitchen_motion",)})
    )

    cleaned, removed = store.reconcile(_house_shape(()))

    assert cleaned.area_entity_selections == {}
    assert removed


async def test_reconcile_drops_selection_entity_moved_elsewhere(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(
        area_entity_selections=MappingProxyType({"kitchen": ("binary_sensor.motion",)})
    )
    # The entity still exists, but its current area is now "hallway", not "kitchen".
    shape = _house_shape(("kitchen", "hallway"), {"binary_sensor.motion": "hallway"})

    cleaned, removed = store.reconcile(shape)

    assert cleaned.area_entity_selections == {}
    assert removed


async def test_reconcile_drops_node_position_for_missing_area(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    store._topology = TopologyData(
        area_positions=MappingProxyType({"kitchen": (1.0, 2.0), "hallway": (3.0, 4.0)})
    )

    cleaned, removed = store.reconcile(_house_shape(("hallway",)))

    assert cleaned.area_positions == {"hallway": (3.0, 4.0)}
    assert removed


async def test_reconcile_is_a_noop_when_nothing_changed(hass: HomeAssistant) -> None:
    store = TopologyStore(hass, "entry-1")
    topology = TopologyData(connectors=(Connector("c1", "kitchen", "hallway"),))
    store._topology = topology

    cleaned, removed = store.reconcile(_house_shape(("kitchen", "hallway")))

    assert cleaned == topology
    assert removed == []


async def test_reconcile_and_save_only_persists_when_something_changed(
    hass: HomeAssistant,
) -> None:
    store = TopologyStore(hass, "entry-1")
    save_calls = 0
    original_async_save = store.async_save

    async def counting_async_save(topology: TopologyData) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_async_save(topology)

    store.async_save = counting_async_save  # type: ignore[method-assign]
    store._topology = TopologyData(connectors=(Connector("c1", "kitchen", "hallway"),))

    removed = await store.async_reconcile_and_save(_house_shape(("kitchen", "hallway")))
    assert removed == []
    assert save_calls == 0

    removed = await store.async_reconcile_and_save(_house_shape(("kitchen",)))
    assert removed
    assert save_calls == 1


def test_active_area_ids_includes_areas_with_activity_evidence() -> None:
    topology = TopologyData(area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)})

    assert active_area_ids(topology) == frozenset({"kitchen"})


def test_active_area_ids_includes_access_points() -> None:
    topology = TopologyData(
        egress_points=(EgressPoint(area_id="hallway", entity_ids=("binary_sensor.front_door",)),)
    )

    assert active_area_ids(topology) == frozenset({"hallway"})


def test_active_area_ids_excludes_areas_with_nothing_selected() -> None:
    # An empty-tuple entry (rather than the key being absent entirely) must
    # still count as "nothing selected" — the frontend always deletes the
    # key on full deselect, but the store itself shouldn't rely on that.
    topology = TopologyData(area_entity_selections={"study": ()})

    assert active_area_ids(topology) == frozenset()


def test_active_area_ids_unions_evidence_and_access_points() -> None:
    topology = TopologyData(
        area_entity_selections={"kitchen": ("binary_sensor.kitchen_motion",)},
        egress_points=(EgressPoint(area_id="hallway", entity_ids=("binary_sensor.front_door",)),),
    )

    assert active_area_ids(topology) == frozenset({"kitchen", "hallway"})
