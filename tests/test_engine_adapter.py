"""Tests for the registry/topology -> HouseGraph adapter (docs/ARCHITECTURE.md §1.4-1.5)."""

from __future__ import annotations

from types import MappingProxyType

from custom_components.occupancy_tracker.engine_adapter import (
    build_house_graph,
    egress_connector_id,
)
from custom_components.occupancy_tracker.occupancy_engine import OUTSIDE, AreaKind
from custom_components.occupancy_tracker.registry_sync import (
    AreaSnapshot,
    EntitySnapshot,
    HouseShape,
)
from custom_components.occupancy_tracker.topology_store import Connector, EgressPoint, TopologyData


def _house_shape(
    area_ids: tuple[str, ...], entity_device_classes: dict[str, str | None] | None = None
) -> HouseShape:
    areas = {
        area_id: AreaSnapshot(area_id=area_id, name=area_id, floor_id=None, entity_ids=())
        for area_id in area_ids
    }
    entities = {
        entity_id: EntitySnapshot(
            entity_id=entity_id,
            device_id=None,
            area_id=None,
            platform="test",
            disabled=False,
            hidden=False,
            name=entity_id,
            device_class=device_class,
        )
        for entity_id, device_class in (entity_device_classes or {}).items()
    }
    return HouseShape(
        areas=MappingProxyType(areas),
        floors=MappingProxyType({}),
        entities=MappingProxyType(entities),
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


# -- Area-kind inference (docs/DECISIONS.md's "area-kind classification"
# entry) -----------------------------------------------------------------


def test_area_kind_infers_transit_for_an_unsensored_through_node() -> None:
    """A hallway with two Connectors and no activity-evidence entity of its
    own is exactly SPEC.md §5.1's example of a passage, not a room.
    """
    shape = _house_shape(("kitchen", "hallway", "living_room"))
    topology = TopologyData(
        connectors=(
            Connector("c1", "kitchen", "hallway"),
            Connector("c2", "hallway", "living_room"),
        )
    )

    graph = build_house_graph(shape, topology)

    assert graph.kind_of("hallway") is AreaKind.TRANSIT
    assert graph.kind_of("kitchen") is AreaKind.ROOM
    assert graph.kind_of("living_room") is AreaKind.ROOM


def test_area_kind_stays_room_for_an_unsensored_dead_end() -> None:
    """A dead-end Area (one Connector) with nothing selected yet is "not
    configured," not "a busy passage" — degree alone isn't enough.
    """
    shape = _house_shape(("kitchen", "pantry"))
    topology = TopologyData(connectors=(Connector("c1", "kitchen", "pantry"),))

    graph = build_house_graph(shape, topology)

    assert graph.kind_of("pantry") is AreaKind.ROOM


def test_area_kind_stays_room_for_a_through_node_with_its_own_evidence() -> None:
    """A through-node the user *has* selected activity evidence for (e.g. a
    large open-plan room that also happens to connect several others) is a
    real room, not a passage — evidence selection wins over degree alone.
    """
    shape = _house_shape(("kitchen", "landing", "living_room"))
    topology = TopologyData(
        connectors=(
            Connector("c1", "kitchen", "landing"),
            Connector("c2", "landing", "living_room"),
        ),
        area_entity_selections=MappingProxyType({"landing": ("binary_sensor.landing_motion",)}),
    )

    graph = build_house_graph(shape, topology)

    assert graph.kind_of("landing") is AreaKind.ROOM


def test_area_kind_override_wins_over_inference() -> None:
    shape = _house_shape(("kitchen", "hallway", "living_room"))
    topology = TopologyData(
        connectors=(
            Connector("c1", "kitchen", "hallway"),
            Connector("c2", "hallway", "living_room"),
        ),
        area_kind_overrides=MappingProxyType({"hallway": "room"}),
    )

    graph = build_house_graph(shape, topology)

    assert graph.kind_of("hallway") is AreaKind.ROOM


def test_area_kind_defaults_to_room_for_an_area_with_no_connectors_at_all() -> None:
    shape = _house_shape(("kitchen",))

    graph = build_house_graph(shape, TopologyData())

    assert graph.kind_of("kitchen") is AreaKind.ROOM


# -- Outdoor-Area total exclusion (docs/DECISIONS.md) ----------------------


def test_build_house_graph_passes_through_outside_area_ids() -> None:
    shape = _house_shape(("kitchen", "front_yard"))
    topology = TopologyData(outside_area_ids=frozenset({"front_yard"}))

    graph = build_house_graph(shape, topology)

    assert graph.outside_area_ids == frozenset({"front_yard"})


def test_build_house_graph_drops_outside_area_id_for_missing_area() -> None:
    shape = _house_shape(("kitchen",))  # "front_yard" no longer exists
    topology = TopologyData(outside_area_ids=frozenset({"front_yard"}))

    graph = build_house_graph(shape, topology)

    assert graph.outside_area_ids == frozenset()


# -- Decay eligibility (docs/DECISIONS.md's decay entry) --------------------


def test_decay_eligible_when_all_evidence_is_occupancy_device_class() -> None:
    shape = _house_shape(("landing",), {"binary_sensor.landing_presence": "occupancy"})
    topology = TopologyData(
        area_entity_selections=MappingProxyType({"landing": ("binary_sensor.landing_presence",)})
    )

    graph = build_house_graph(shape, topology)

    assert graph.decay_eligible_area_ids == frozenset({"landing"})


def test_not_decay_eligible_when_evidence_is_an_ordinary_motion_sensor() -> None:
    shape = _house_shape(("kitchen",), {"binary_sensor.kitchen_motion": "motion"})
    topology = TopologyData(
        area_entity_selections=MappingProxyType({"kitchen": ("binary_sensor.kitchen_motion",)})
    )

    graph = build_house_graph(shape, topology)

    assert graph.decay_eligible_area_ids == frozenset()


def test_not_decay_eligible_when_evidence_is_mixed() -> None:
    """A room with even one non-occupancy-class sensor stays fully latch-only
    — decay eligibility requires *every* selected entity to qualify.
    """
    shape = _house_shape(
        ("landing",),
        {
            "binary_sensor.landing_presence": "occupancy",
            "binary_sensor.landing_motion": "motion",
        },
    )
    topology = TopologyData(
        area_entity_selections=MappingProxyType(
            {"landing": ("binary_sensor.landing_presence", "binary_sensor.landing_motion")}
        )
    )

    graph = build_house_graph(shape, topology)

    assert graph.decay_eligible_area_ids == frozenset()


def test_not_decay_eligible_with_no_evidence_selected() -> None:
    shape = _house_shape(("kitchen",))
    topology = TopologyData(area_entity_selections=MappingProxyType({"kitchen": ()}))

    graph = build_house_graph(shape, topology)

    assert graph.decay_eligible_area_ids == frozenset()
