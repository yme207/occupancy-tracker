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
from datetime import datetime

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

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
#: The only state value treated as positive vacancy evidence for the decay
#: mechanism below (docs/DECISIONS.md) — deliberately *not* "anything that
#: isn't on" (e.g. "unavailable"/"unknown"): a sensor going offline is an
#: absence of *data*, not positive evidence of an empty room, and treating it
#: as such would silently reintroduce exactly the failure mode SPEC.md §6.2's
#: "never decay on absence of signal" rule exists to prevent.
_INACTIVE_STATE = "off"


class SignalIngestion:
    """Wires topology-selected entities' state changes into the engine."""

    def __init__(self, hass: HomeAssistant, engine: OccupancyEngine) -> None:
        self._hass = hass
        self._engine = engine
        self._context_tracker = AutomationContextTracker(hass)
        self._unsub: list[CALLBACK_TYPE] = []
        #: Per-Area pending decay timers (docs/DECISIONS.md) — a cancel
        #: callable, present only while that Area's decay-eligible evidence
        #: is currently believed fully off and its grace period hasn't
        #: elapsed yet.
        self._decay_timer_cancel: dict[str, CALLBACK_TYPE] = {}
        #: Snapshot of each decay-eligible Area's own evidence entity ids, so
        #: the timer callback (which fires with no other context) knows which
        #: entities to re-check before actually clearing.
        self._decay_entity_ids: dict[str, tuple[str, ...]] = {}

    @callback
    def async_start(self, topology: TopologyData) -> None:
        """Subscribe to every entity the given topology selects as evidence."""
        self._context_tracker.async_start()

        decay_eligible = self._engine.graph.decay_eligible_area_ids
        for area_id, entity_ids in topology.area_entity_selections.items():
            if not entity_ids:
                continue
            self._unsub.append(
                async_track_state_change_event(
                    self._hass, list(entity_ids), self._area_listener(area_id)
                )
            )
            if area_id in decay_eligible:
                self._decay_entity_ids[area_id] = entity_ids
                self._unsub.append(
                    async_track_state_change_event(
                        self._hass, list(entity_ids), self._decay_listener(area_id, entity_ids)
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
        for cancel in list(self._decay_timer_cancel.values()):
            cancel()
        self._decay_timer_cancel.clear()

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

    def _all_confirmed_off(self, entity_ids: tuple[str, ...]) -> bool:
        """True only if *every* entity is currently, positively `off` — a
        missing/`unavailable`/`unknown` entity is absence of data, not
        evidence of vacancy, so it counts as "not confirmed off" (see the
        module-level `_INACTIVE_STATE` docstring).
        """
        return all(
            (state := self._hass.states.get(entity_id)) is not None
            and state.state == _INACTIVE_STATE
            for entity_id in entity_ids
        )

    def _decay_listener(
        self, area_id: str, entity_ids: tuple[str, ...]
    ) -> Callable[[Event[EventStateChangedData]], None]:
        """Schedules/cancels `area_id`'s decay timer (docs/DECISIONS.md) as
        its own decay-eligible evidence entities turn on/off. Independent of
        `_area_listener` above (which still separately produces the ordinary
        `AreaActivitySignal` on an "on" transition) — this one only ever
        drives the auto-clear-after-quiet mechanism, never the count going up.
        """

        @callback
        def listener(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None or new_state.state != _INACTIVE_STATE:
                # A real "on", or the entity going unavailable/unknown/gone —
                # none of these are positive vacancy evidence; cancel any
                # in-progress countdown rather than let it complete on stale
                # grounds.
                self._cancel_decay_timer(area_id)
                return
            if not self._all_confirmed_off(entity_ids):
                return  # at least one is still on (or has no data) — not vacant yet
            self._schedule_decay_timer(area_id, entity_ids)

        return listener

    def _cancel_decay_timer(self, area_id: str) -> None:
        cancel = self._decay_timer_cancel.pop(area_id, None)
        if cancel is not None:
            cancel()

    def _schedule_decay_timer(self, area_id: str, entity_ids: tuple[str, ...]) -> None:
        if area_id in self._decay_timer_cancel:
            return  # already counting down

        @callback
        def _fire(now: datetime) -> None:
            self._decay_timer_cancel.pop(area_id, None)
            if not self._all_confirmed_off(entity_ids):
                return  # re-occupied (or lost data) since scheduling — do nothing
            self._engine.expire_vacant_area(area_id, now)

        self._decay_timer_cancel[area_id] = async_call_later(
            self._hass, self._engine.effective_decay_grace_period(area_id), _fire
        )
