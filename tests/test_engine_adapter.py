"""Tests for the registry/topology -> HouseGraph adapter (docs/ARCHITECTURE.md §1.4-1.5)."""

from __future__ import annotations

from types import MappingProxyType

from custom_components.occupancy_tracker.engine_adapter import (
    build_house_graph,
    egress_connector_id,
)
from custom_components.occupancy_tracker.occupancy_engine import OUTSIDE
from custom_components.occupancy_tracker.registry_sync import AreaSnapshot, HouseShape
from custom_components.occupancy_tracker.topology_store import Connector, EgressPoint, TopologyData


def _house_shape(area_ids: tuple[str, ...]) -> HouseShape:
    areas = {
        area_id: AreaSnapshot(area_id=area_id, name=area_id, floor_id=None, entity_ids=())
        for area_id in area_ids
    }
    return HouseShape(
        areas=MappingProxyType(areas), floors=MappingProxyType({}), entities=MappingProxyType({})
    )


def test_build_house_graph_includes_all_areas() -> None:
    shape = _house_shape(("kitchen", "hallway"))

    graph = build_house_graph(shape, TopologyData())

    assert graph.area_ids == frozenset({"kitchen", "hallway"})
    assert graph.connectors == ()


def test_build_house_graph_includes_regular_connectors() -> None:
    shape = _house_shape(("kitchen", "hallway"))
    topology = TopologyData(connectors=(Connector("c1", "kitchen", "hallway"),))

    graph = build_house_graph(shape, topology)

    assert len(graph.connectors) == 1
    connector = graph.connectors[0]
    assert connector.connector_id == "c1"
    assert {connector.area_id_a, connector.area_id_b} == {"kitchen", "hallway"}


def test_build_house_graph_synthesizes_egress_connectors() -> None:
    shape = _house_shape(("entryway",))
    topology = TopologyData(egress_points=(EgressPoint("entryway", ("binary_sensor.door",)),))

    graph = build_house_graph(shape, topology)

    assert len(graph.connectors) == 1
    connector = graph.connectors[0]
    assert connector.connector_id == egress_connector_id("entryway")
    assert {connector.area_id_a, connector.area_id_b} == {"entryway", OUTSIDE}


def test_build_house_graph_drops_connector_referencing_missing_area() -> None:
    """Defensive: a not-yet-reconciled topology snapshot shouldn't produce a
    dangling Connector.
    """
    shape = _house_shape(("kitchen",))  # "hallway" no longer exists
    topology = TopologyData(connectors=(Connector("c1", "kitchen", "hallway"),))

    graph = build_house_graph(shape, topology)

    assert graph.connectors == ()


def test_build_house_graph_drops_egress_point_referencing_missing_area() -> None:
    shape = _house_shape(())  # "entryway" no longer exists
    topology = TopologyData(egress_points=(EgressPoint("entryway", ("binary_sensor.door",)),))

    graph = build_house_graph(shape, topology)

    assert graph.connectors == ()
