"""Zone-presence fusion (SPEC.md §6.7, docs/ARCHITECTURE.md's layering diagram
places this alongside signal ingestion and the provenance resolver, feeding
the occupancy engine — not a core engine mechanism).

Two pieces, same split as `provenance.py`:

- `classify_zone_membership` is a pure function — unit-testable with a
  constructed `homeassistant.core.State`, no running `hass` needed.
- `ZoneFusion` is the stateful, HA-dependent half: subscribes to the
  user-picked `person`/`device_tracker` entities (SPEC.md §7.2's options
  flow) and derives two SPEC.md §6.7 behaviors — house-level occupant-total
  corroboration and near-house-zone pre-arming — deliberately *without*
  touching the occupancy engine's per-Area counts, since zone presence alone
  must never silently change them (SPEC.md §6.7).
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, auto

from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

#: The state value Home Assistant uses for "in the home zone" (verified:
#: homeassistant.const.STATE_HOME = "home", and person/device_tracker
#: entities' own state is set directly from this — see docs/DECISIONS.md).
_STATE_HOME = "home"
#: Attribute key carrying the list of zone entity ids a tracked entity is
#: currently in (verified: DeviceTrackerEntityStateAttribute.IN_ZONES /
#: PersonEntityStateAttribute.IN_ZONES both resolve to "in_zones" — the
#: modern, entity-id-based way to check zone membership; not populated by
#: every legacy tracker, see docs/DECISIONS.md for why this module doesn't
#: also try to string-match the legacy free-text zone-name state value).
_ATTR_IN_ZONES = "in_zones"


class ZoneMembership(Enum):
    """Where a tracked person/device_tracker currently is, as far as SPEC.md
    §6.7 cares — not a precise zone identity, just the three-way distinction
    the fusion behaviors below need.
    """

    #: In the home zone.
    HOME = auto()
    #: In a zone the user picked as "near the house" (not home).
    NEAR_HOUSE = auto()
    #: Neither — away, or zone membership can't be determined.
    AWAY = auto()


def classify_zone_membership(state: State, near_house_zone_ids: Collection[str]) -> ZoneMembership:
    """Classify one tracked entity's current `State` (SPEC.md §6.7)."""
    if state.state == _STATE_HOME:
        return ZoneMembership.HOME
    in_zones = state.attributes.get(_ATTR_IN_ZONES) or ()
    if any(zone_id in near_house_zone_ids for zone_id in in_zones):
        return ZoneMembership.NEAR_HOUSE
    return ZoneMembership.AWAY


class ZoneCorroboration(Enum):
    """House-level occupant-total corroboration from tracked zone presence."""

    #: At least one tracked person is currently home.
    CORROBORATED = auto()
    #: At least one tracked person's zone is known, and none are home.
    CONTRADICTED = auto()
    #: No tracked persons configured, or none reporting yet.
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class ZoneFusionConfig:
    """Tunables for zone fusion (docs/ARCHITECTURE.md §2's typed-config
    extension point). `SPEC.md` §7.2 lists this as an options-flow candidate
    ("transit confirmation/grace windows") — not yet exposed there; the
    entity/zone *selection* fields are (see config_flow.py), but this
    specific window is a sensible fixed default for now rather than
    speculative UI surface with no evidence a fixed default is wrong.
    """

    #: How long after entering a near-house zone a tracked person keeps the
    #: house "pre-armed" (SPEC.md §6.7).
    pre_arm_window: timedelta = timedelta(minutes=5)


class ZoneFusion:
    """Tracks configured persons' zone state and derives corroboration/pre-arm."""

    def __init__(
        self,
        hass: HomeAssistant,
        tracked_entity_ids: Collection[str],
        near_house_zone_ids: Collection[str],
        config: ZoneFusionConfig | None = None,
    ) -> None:
        self._hass = hass
        self._tracked_entity_ids = tuple(tracked_entity_ids)
        self._near_house_zone_ids = frozenset(near_house_zone_ids)
        self._config = config or ZoneFusionConfig()
        self._memberships: dict[str, ZoneMembership] = {}
        self._last_near_house_entry: dict[str, datetime] = {}
        self._unsub: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_start(self) -> None:
        """Subscribe to the configured tracked entities, if any."""
        if not self._tracked_entity_ids:
            return
        self._unsub.append(
            async_track_state_change_event(
                self._hass, list(self._tracked_entity_ids), self._handle_tracker_update
            )
        )

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and forget everything tracked so far."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        self._memberships.clear()
        self._last_near_house_entry.clear()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register a callback invoked whenever a tracked entity's zone changes."""
        self._listeners.append(listener)

        def remove_listener() -> None:
            self._listeners.remove(listener)

        return remove_listener

    def house_zone_corroboration(self) -> ZoneCorroboration:
        """Whole-house corroboration for the current occupant total (SPEC.md §6.7)."""
        known = [
            self._memberships[entity_id]
            for entity_id in self._tracked_entity_ids
            if entity_id in self._memberships
        ]
        if not known:
            return ZoneCorroboration.UNKNOWN
        if any(membership is ZoneMembership.HOME for membership in known):
            return ZoneCorroboration.CORROBORATED
        return ZoneCorroboration.CONTRADICTED

    def is_pre_armed(self, now: datetime) -> bool:
        """Whether any tracked person entered a near-house zone recently enough
        to still count as "approaching" (SPEC.md §6.7).
        """
        return any(
            now - entry_time <= self._config.pre_arm_window
            for entry_time in self._last_near_house_entry.values()
        )

    @callback
    def _handle_tracker_update(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is None:
            return
        membership = classify_zone_membership(new_state, self._near_house_zone_ids)
        previous = self._memberships.get(new_state.entity_id)
        self._memberships[new_state.entity_id] = membership
        if membership is ZoneMembership.NEAR_HOUSE and previous is not ZoneMembership.NEAR_HOUSE:
            self._last_near_house_entry[new_state.entity_id] = new_state.last_changed
        for listener in list(self._listeners):
            listener()
