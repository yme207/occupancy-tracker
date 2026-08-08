"""Provenance resolution: classify a state change's causal `Context` as
automation-caused, user-caused, or neither (SPEC.md §6.6, ARCHITECTURE.md §4).

Two pieces, deliberately separated:

- `resolve_provenance` is a pure function — unit-testable with constructed
  `Context` objects, independent of a running HA instance, per
  ARCHITECTURE.md §4. It only needs `homeassistant.core.Context`, a plain
  object constructible without a live `hass` (unlike `HomeAssistant`,
  registries, or `Store`), so this stays a light, verification-only import.
- `AutomationContextTracker` is the stateful, HA-dependent half: it builds
  the live set of context ids known to belong to an automation/script
  trigger by listening for the same events Home Assistant's own Logbook
  integrates against (see docs/DECISIONS.md for how this was verified).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Collection, Iterator
from typing import Any

from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.components.script.const import EVENT_SCRIPT_STARTED
from homeassistant.core import CALLBACK_TYPE, Context, Event, HomeAssistant, callback

from .occupancy_engine import ProvenanceTier

#: Bounded so this never grows unbounded over an installation's uptime
#: (SPEC.md §9) — a context is only useful here for the brief window between
#: an automation/script starting and the state changes it directly causes.
_DEFAULT_MAX_TRACKED_CONTEXTS = 256


def resolve_provenance(
    context: Context, known_automation_context_ids: Collection[str]
) -> ProvenanceTier:
    """Classify one state change's `Context` (SPEC.md §6.6).

    Checked in order:
    1. `context.id` itself is a known automation/script trigger context —
       the common case, since Home Assistant passes that same `Context`
       object through unchanged to the entities it updates (verified against
       `automation`/`script`/`helpers/script.py` source — see
       docs/DECISIONS.md).
    2. `context.parent_id` is a known automation/script trigger context — a
       one-hop fallback for when an intermediate entity/service deliberately
       mints a fresh child `Context` (the same single-hop depth Home
       Assistant's own Logbook uses, not an unbounded walk).
    3. `context.user_id` is present — direct human action.
    4. Neither — an origin-less physical-world event, weak-positive evidence
       by default (SPEC.md §6.6).
    """
    if context.id in known_automation_context_ids:
        return ProvenanceTier.AUTOMATION_SUPPRESSED
    if context.parent_id is not None and context.parent_id in known_automation_context_ids:
        return ProvenanceTier.AUTOMATION_SUPPRESSED
    if context.user_id is not None:
        return ProvenanceTier.USER_CONFIRMED
    return ProvenanceTier.AMBIGUOUS_PHYSICAL


class AutomationContextTracker(Collection[str]):
    """Live set of context ids known to belong to an automation/script trigger.

    Built by listening for `EVENT_AUTOMATION_TRIGGERED`/`EVENT_SCRIPT_STARTED`
    — verified this is how Home Assistant's own Logbook attributes
    automation-caused changes (`components/automation/logbook.py`,
    `components/script/logbook.py` register descriptions for these same
    events; see docs/DECISIONS.md for the full trace through
    `helpers/script.py` showing the triggering `Context` is reused unchanged
    for the resulting state changes).
    """

    def __init__(self, hass: HomeAssistant, max_size: int = _DEFAULT_MAX_TRACKED_CONTEXTS) -> None:
        self._hass = hass
        self._max_size = max_size
        self._context_ids: OrderedDict[str, None] = OrderedDict()
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_start(self) -> None:
        """Start listening for automation/script trigger events."""
        self._unsub.append(
            self._hass.bus.async_listen(EVENT_AUTOMATION_TRIGGERED, self._remember_context)
        )
        self._unsub.append(
            self._hass.bus.async_listen(EVENT_SCRIPT_STARTED, self._remember_context)
        )

    @callback
    def async_stop(self) -> None:
        """Stop listening and forget everything tracked so far."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        self._context_ids.clear()

    @callback
    def _remember_context(self, event: Event[Any]) -> None:
        context_id = event.context.id
        self._context_ids[context_id] = None
        self._context_ids.move_to_end(context_id)
        if len(self._context_ids) > self._max_size:
            self._context_ids.popitem(last=False)

    def __contains__(self, context_id: object) -> bool:
        return context_id in self._context_ids

    def __iter__(self) -> Iterator[str]:
        return iter(self._context_ids)

    def __len__(self) -> int:
        return len(self._context_ids)
