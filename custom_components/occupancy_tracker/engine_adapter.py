"""Adapter: builds the engine's standalone `HouseGraph` from the live
registry sync + topology store layers (docs/ARCHITECTURE.md §1.4-1.5).

This is intentionally the *only* module that imports both
`occupancy_engine` (HA-independent) and `registry_sync`/`topology_store`
(HA-dependent) — keeping that bridging in one place is what lets
`occupancy_engine.py` itself stay import-clean (see docs/DECISIONS.md's
"standalone graph types" entry).
"""

from __future__ import annotations

from .occupancy_engine import OUTSIDE, GraphConnector, HouseGraph
from .registry_sync import HouseShape
from .topology_store import TopologyData


def egress_connector_id(area_id: str) -> str:
    """The synthetic Connector id representing an egress point's boundary crossing.

    Shared naming so `build_house_graph` (here) and the signal ingestion
    layer agree on which Connector an egress point's crossing-sensor
    activity belongs to.
    """
    return f"egress:{area_id}"


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
    return HouseGraph(area_ids=frozenset(house_shape.areas), connectors=tuple(connectors))
