"""Adapter: builds the engine's standalone `HouseGraph` from the live
registry sync + topology store layers (docs/ARCHITECTURE.md §1.4-1.5).

This is intentionally the *only* module that imports both
`occupancy_engine` (HA-independent) and `registry_sync`/`topology_store`
(HA-dependent) — keeping that bridging in one place is what lets
`occupancy_engine.py` itself stay import-clean (see docs/DECISIONS.md's
"standalone graph types" entry).
"""

from __future__ import annotations

from collections.abc import Mapping

from .occupancy_engine import OUTSIDE, AreaKind, GraphConnector, HouseGraph
from .registry_sync import HouseShape
from .topology_store import TopologyData


def egress_connector_id(area_id: str) -> str:
    """The synthetic Connector id representing an egress point's boundary crossing.

    Shared naming so `build_house_graph` (here) and the signal ingestion
    layer agree on which Connector an egress point's crossing-sensor
    activity belongs to.
    """
    return f"egress:{area_id}"


def _connector_degree(topology: TopologyData) -> dict[str, int]:
    """How many Connectors (including a synthesized egress crossing) touch
    each Area — the topology-shape half of the area-kind inference below.
    """
    degree: dict[str, int] = {}
    for connector in topology.connectors:
        degree[connector.area_id_a] = degree.get(connector.area_id_a, 0) + 1
        degree[connector.area_id_b] = degree.get(connector.area_id_b, 0) + 1
    for egress in topology.egress_points:
        degree[egress.area_id] = degree.get(egress.area_id, 0) + 1
    return degree


def _infer_area_kind(area_id: str, degree: Mapping[str, int], topology: TopologyData) -> AreaKind:
    """Default `AreaKind` classification (docs/DECISIONS.md's "area-kind
    classification" entry): a through-node (2+ Connectors) with no activity-
    evidence entity of its own reads as a passage people cross rather than
    occupy — exactly SPEC.md §5.1's own example ("a hallway with no sensors
    at all, connecting two sensored rooms"). A dead-end (1 Connector) never
    qualifies regardless of evidence, since a room with nothing selected yet
    is "not configured," not "a busy passage." Purely topology-shape-derived,
    never guessed from anything the engine observes at runtime.
    """
    has_evidence = bool(topology.area_entity_selections.get(area_id))
    if not has_evidence and degree.get(area_id, 0) >= 2:
        return AreaKind.TRANSIT
    return AreaKind.ROOM


def _area_kinds(house_shape: HouseShape, topology: TopologyData) -> dict[str, AreaKind]:
    """Per-Area kind: the user's manual override (`TopologyData.area_kind_overrides`,
    topology panel) if set, else the topology-shape inference above.
    """
    degree = _connector_degree(topology)
    kinds: dict[str, AreaKind] = {}
    for area_id in house_shape.areas:
        override = topology.area_kind_overrides.get(area_id)
        if override is not None:
            kinds[area_id] = AreaKind[override.upper()]
        else:
            kinds[area_id] = _infer_area_kind(area_id, degree, topology)
    return kinds


#: The one `binary_sensor` device class documented (verified against the
#: installed `homeassistant` package's `BinarySensorDeviceClass`, not
#: assumed) to mean "On means occupied, Off means not occupied" — a real
#: continuous-presence sensor (e.g. mmWave/radar), unlike `motion` ("On means
#: motion detected, Off means no motion (clear)", which says nothing about
#: whether someone still-but-motionless is in the room) or `presence`
#: ("On means home, Off means away" — a *person's* home/away state, the same
#: concept `zone_fusion.py` already covers, not room-level occupancy).
#: docs/DECISIONS.md's decay entry.
_CONTINUOUS_PRESENCE_DEVICE_CLASS = "occupancy"


def _decay_eligible_area_ids(house_shape: HouseShape, topology: TopologyData) -> frozenset[str]:
    """Areas whose *entire* selected activity-evidence list is continuous-
    presence-class sensors — eligible for `OccupancyEngine.expire_vacant_area`
    (docs/DECISIONS.md's decay entry). An Area with no evidence, or evidence
    that's an ordinary motion sensor (or any mix including one), is never
    eligible — SPEC.md §6.2's "never decay" guarantee holds for it unchanged.
    """
    eligible: set[str] = set()
    for area_id, entity_ids in topology.area_entity_selections.items():
        if not entity_ids:
            continue
        if all(
            (entity := house_shape.entities.get(entity_id)) is not None
            and entity.device_class == _CONTINUOUS_PRESENCE_DEVICE_CLASS
            for entity_id in entity_ids
        ):
            eligible.add(area_id)
    return frozenset(eligible)


def build_house_graph(house_shape: HouseShape, topology: TopologyData) -> HouseGraph:
    """Build a `HouseGraph` snapshot from the current registries + topology.

    Defensively filters out any Connector/egress point referencing an Area
    that isn't in `house_shape` — `TopologyStore.reconcile()` already keeps
    stored topology consistent with the registries, but this is cheap
    insurance against building a graph from a not-yet-reconciled snapshot.
    """
    connectors = [
        GraphConnector(connector.connector_id, connector.area_id_a, connector.area_id_b)
        for connector in topology.connectors
        if connector.area_id_a in house_shape.areas and connector.area_id_b in house_shape.areas
    ]
    connectors.extend(
        GraphConnector(egress_connector_id(egress.area_id), egress.area_id, OUTSIDE)
        for egress in topology.egress_points
        if egress.area_id in house_shape.areas
    )
    return HouseGraph(
        area_ids=frozenset(house_shape.areas),
        connectors=tuple(connectors),
        area_kinds=_area_kinds(house_shape, topology),
        outside_area_ids=frozenset(topology.outside_area_ids) & house_shape.areas.keys(),
        decay_eligible_area_ids=_decay_eligible_area_ids(house_shape, topology),
    )
