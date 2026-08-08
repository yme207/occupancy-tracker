"""Signal ingestion layer (docs/ARCHITECTURE.md §1.3).

Subscribes to state changes for entities selected in the topology store and
converts them into normalized Signals for the occupancy engine. Event-driven
throughout (`async_track_state_change_event`), never polled (docs/SPEC.md
§9). Automation-vs-manual provenance is resolved per Signal (SPEC.md §6.6,
`provenance.py`) — an automation/script-caused change is suppressed
entirely and never becomes a Signal. Zone-presence fusion (Phase 6, SPEC.md
§6.7) doesn't exist yet. A state transitioning to "on" is the only thing
currently treated as activity evidence — richer, device-class-aware
classification is future work.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .engine_adapter import egress_connector_id
from .occupancy_engine import (
    AreaActivitySignal,
    ConnectorActivitySignal,
    OccupancyEngine,
    ProvenanceTier,
)
from .provenance import AutomationContextTracker, resolve_provenance
from .topology_store import TopologyData

#: The only state value currently treated as "activity" evidence (see
#: module docstring — richer, device-class-aware classification is future
#: work, not this first pass).
_ACTIVE_STATE = "on"


class SignalIngestion:
    """Wires topology-selected entities' state changes into the engine."""

    def __init__(self, hass: HomeAssistant, engine: OccupancyEngine) -> None:
        self._hass = hass
        self._engine = engine
        self._context_tracker = AutomationContextTracker(hass)
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_start(self, topology: TopologyData) -> None:
        """Subscribe to every entity the given topology selects as evidence."""
        self._context_tracker.async_start()

        for area_id, entity_ids in topology.area_entity_selections.items():
            if not entity_ids:
                continue
            self._unsub.append(
                async_track_state_change_event(
                    self._hass, list(entity_ids), self._area_listener(area_id)
                )
            )

        for egress in topology.egress_points:
            if not egress.entity_ids:
                continue
            connector_id = egress_connector_id(egress.area_id)
            self._unsub.append(
                async_track_state_change_event(
                    self._hass, list(egress.entity_ids), self._connector_listener(connector_id)
                )
            )

    @callback
    def async_stop(self) -> None:
        """Unsubscribe from all currently-tracked entities."""
        self._context_tracker.async_stop()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def _area_listener(self, area_id: str) -> Callable[[Event[EventStateChangedData]], None]:
        @callback
        def listener(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None or new_state.state != _ACTIVE_STATE:
                return
            provenance = resolve_provenance(new_state.context, self._context_tracker)
            if provenance is ProvenanceTier.AUTOMATION_SUPPRESSED:
                return
            self._engine.process_signal(
                AreaActivitySignal(
                    area_id=area_id,
                    timestamp=new_state.last_changed,
                    source=new_state.entity_id,
                    provenance=provenance,
                )
            )

        return listener

    def _connector_listener(
        self, connector_id: str
    ) -> Callable[[Event[EventStateChangedData]], None]:
        @callback
        def listener(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None or new_state.state != _ACTIVE_STATE:
                return
            provenance = resolve_provenance(new_state.context, self._context_tracker)
            if provenance is ProvenanceTier.AUTOMATION_SUPPRESSED:
                return
            self._engine.process_signal(
                ConnectorActivitySignal(
                    connector_id=connector_id,
                    timestamp=new_state.last_changed,
                    source=new_state.entity_id,
                    provenance=provenance,
                )
            )

        return listener
