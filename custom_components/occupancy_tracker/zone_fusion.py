"""Zone-presence fusion (SPEC.md §6.7, docs/ARCHITECTURE.md's layering diagram
places this alongside signal ingestion and the provenance resolver, feeding
the occupancy engine — not a core engine mechanism).

Two pieces, same split as `provenance.py`:

- `classify_zone_membership` is a pure function — unit-testable with a
  constructed `homeassistant.core.State`, no running `hass` needed.
- `ZoneFusion` is the stateful, HA-dependent half: subscribes to the
  user-picked `person`/`device_tracker` entities (SPEC.md §7.2's options
  flow) and derives two SPEC.md §6.7 behaviors — house-level occupant-total
  corroboration and near-house-zone pre-arming — without touching the
  occupancy engine's per-Area counts, since zone presence alone must never
  *silently* change them (SPEC.md §6.7). One narrow, opt-in exception exists
  (docs/DECISIONS.md's "zone-fusion away-clear" entry): once every tracked
  person has read AWAY continuously for `ZoneFusionConfig.
  zone_away_clear_delay`, and `ZoneFusionConfig.clear_house_when_all_away`
  is on, `ZoneFusion` calls `OccupancyEngine.clear_house()` directly — never
  silent (the delay and the opt-in default-off both exist specifically so
  this isn't a surprise), and never per-Area corroboration nudging a count,
  which SPEC.md §6.7 still rules out entirely.
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
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .occupancy_engine import OccupancyEngine

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
    extension point). Exposed via the options flow as `CONF_PRE_ARM_WINDOW`
    (see config_flow.py) — `__init__.py` builds this dataclass from
    `entry.options`, falling back to the default below when unset.
    """

    #: How long after entering a near-house zone a tracked person keeps the
    #: house "pre-armed" (SPEC.md §6.7).
    pre_arm_window: timedelta = timedelta(minutes=5)
    #: Opt-in, default off (docs/DECISIONS.md's "zone-fusion away-clear"
    #: entry): once True, `ZoneFusion` force-clears the whole house's
    #: occupant count to 0 once every tracked person has read AWAY (not just
    #: outside a near-house zone) continuously for `zone_away_clear_delay`.
    #: Only trustworthy when everyone who might realistically be home
    #: carries a tracked person/device_tracker entity — a guest, child, or
    #: relative without one would make an auto-clear wrong, which is exactly
    #: why this defaults off rather than turning on automatically once a
    #: person is configured; the options-flow copy for this toggle must
    #: state that caveat plainly, not just enable it quietly.
    clear_house_when_all_away: bool = False
    #: How long every tracked person must continuously read AWAY before
    #: `clear_house_when_all_away` (see above) actually fires — guards
    #: against a momentary GPS dropout or zone flap triggering a wrong
    #: clear. Only consulted when `clear_house_when_all_away` is True.
    zone_away_clear_delay: timedelta = timedelta(minutes=15)


class ZoneFusion:
    """Tracks configured persons' zone state and derives corroboration/pre-arm."""

    def __init__(
        self,
        hass: HomeAssistant,
        tracked_entity_ids: Collection[str],
        near_house_zone_ids: Collection[str],
        config: ZoneFusionConfig | None = None,
        engine: OccupancyEngine | None = None,
    ) -> None:
        self._hass = hass
        self._tracked_entity_ids = tuple(tracked_entity_ids)
        self._near_house_zone_ids = frozenset(near_house_zone_ids)
        self._config = config or ZoneFusionConfig()
        #: The engine to force-clear (docs/DECISIONS.md's "zone-fusion
        #: away-clear" entry) — `None` for callers (mainly tests of the
        #: corroboration/pre-arm behaviors) that don't need this feature at
        #: all; `clear_house_when_all_away` is also a no-op without one.
        self._engine = engine
        self._memberships: dict[str, ZoneMembership] = {}
        self._last_near_house_entry: dict[str, datetime] = {}
        self._unsub: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        #: Cancel callable for the in-progress away-clear countdown, present
        #: only while every tracked person currently reads AWAY and the
        #: delay hasn't elapsed yet (mirrors `signal_ingestion.py`'s decay
        #: timer pattern).
        self._away_clear_cancel: CALLBACK_TYPE | None = None

    @callback
    def async_start(self) -> None:
        """Subscribe to the configured tracked entities, if any."""
        if not self._tracked_entity_ids:
            return
        # Seed from whatever these entities' states already are at startup —
        # `async_track_state_change_event` only fires on a future *change*,
        # so without this, a restart while the house was already confirmed
        # empty (the exact "HA rebooting" case in a real overnight walkthrough)
        # would never even start the away-clear countdown until something's
        # zone state next changed, which could be hours away.
        for entity_id in self._tracked_entity_ids:
            state = self._hass.states.get(entity_id)
            if state is not None:
                self._memberships[entity_id] = classify_zone_membership(
                    state, self._near_house_zone_ids
                )
        self._unsub.append(
            async_track_state_change_event(
                self._hass, list(self._tracked_entity_ids), self._handle_tracker_update
            )
        )
        self._sync_away_clear_timer()

    @callback
    def async_stop(self) -> None:
        """Unsubscribe and forget everything tracked so far."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        self._memberships.clear()
        self._last_near_house_entry.clear()
        self._cancel_away_clear_timer()

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
        self._sync_away_clear_timer()
        for listener in list(self._listeners):
            listener()

    def _all_tracked_away(self) -> bool:
        """True only if every tracked entity has reported, and every one of
        them currently reads AWAY (docs/DECISIONS.md's "zone-fusion
        away-clear" entry) — HOME obviously disqualifies, but so does
        NEAR_HOUSE, since "approaching" is the opposite of "confirmed gone."
        A tracked entity that's never reported at all is treated the same as
        one that's home: not enough to conclude the house is empty.
        """
        if not self._tracked_entity_ids:
            return False
        return all(
            self._memberships.get(entity_id) is ZoneMembership.AWAY
            for entity_id in self._tracked_entity_ids
        )

    def _sync_away_clear_timer(self) -> None:
        """Start or cancel the away-clear countdown to match current state
        (docs/DECISIONS.md's "zone-fusion away-clear" entry) — called after
        every membership update, from both a live event and the startup seed.
        """
        if not self._config.clear_house_when_all_away or self._engine is None:
            return
        if self._all_tracked_away():
            if self._away_clear_cancel is not None:
                return  # already counting down
            self._away_clear_cancel = async_call_later(
                self._hass, self._config.zone_away_clear_delay, self._fire_away_clear
            )
        else:
            self._cancel_away_clear_timer()

    @callback
    def _fire_away_clear(self, now: datetime) -> None:
        self._away_clear_cancel = None
        # Re-check rather than trust the state at scheduling time: someone
        # could have come home (or a tracker gone stale) in the meantime.
        if self._engine is not None and self._all_tracked_away():
            self._engine.clear_house(now)

    def _cancel_away_clear_timer(self) -> None:
        if self._away_clear_cancel is not None:
            self._away_clear_cancel()
            self._away_clear_cancel = None
