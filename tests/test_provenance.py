"""Tests for provenance resolution (docs/SPEC.md §6.6, docs/ARCHITECTURE.md §4)."""

from __future__ import annotations

from homeassistant.components.automation import EVENT_AUTOMATION_TRIGGERED
from homeassistant.components.script.const import EVENT_SCRIPT_STARTED
from homeassistant.core import Context, HomeAssistant

from custom_components.occupancy_tracker.occupancy_engine import ProvenanceTier
from custom_components.occupancy_tracker.provenance import (
    AutomationContextTracker,
    resolve_provenance,
)

# -- resolve_provenance: pure, no HA instance needed for these assertions --


def test_context_id_matching_a_known_automation_is_suppressed() -> None:
    context = Context(id="ctx-1")

    tier = resolve_provenance(context, known_automation_context_ids={"ctx-1"})

    assert tier is ProvenanceTier.AUTOMATION_SUPPRESSED


def test_parent_id_matching_a_known_automation_is_suppressed() -> None:
    context = Context(id="ctx-child", parent_id="ctx-automation")

    tier = resolve_provenance(context, known_automation_context_ids={"ctx-automation"})

    assert tier is ProvenanceTier.AUTOMATION_SUPPRESSED


def test_user_id_with_no_automation_ancestry_is_user_confirmed() -> None:
    context = Context(id="ctx-1", user_id="user-abc")

    tier = resolve_provenance(context, known_automation_context_ids=set())

    assert tier is ProvenanceTier.USER_CONFIRMED


def test_neither_automation_nor_user_id_is_ambiguous_physical() -> None:
    context = Context(id="ctx-1")

    tier = resolve_provenance(context, known_automation_context_ids=set())

    assert tier is ProvenanceTier.AMBIGUOUS_PHYSICAL


def test_automation_match_takes_priority_over_a_present_user_id() -> None:
    """A user_id can be present on an automation-originated context in some
    integrations' propagation; automation ancestry is still the stronger,
    more specific signal (SPEC.md §6.6 checks parent_id resolution first).
    """
    context = Context(id="ctx-1", user_id="user-abc")

    tier = resolve_provenance(context, known_automation_context_ids={"ctx-1"})

    assert tier is ProvenanceTier.AUTOMATION_SUPPRESSED


# -- AutomationContextTracker: needs a real hass to fire bus events --


async def test_tracker_remembers_automation_triggered_context(hass: HomeAssistant) -> None:
    tracker = AutomationContextTracker(hass)
    tracker.async_start()
    context = Context(id="automation-ctx")

    hass.bus.async_fire(EVENT_AUTOMATION_TRIGGERED, {}, context=context)
    await hass.async_block_till_done()

    assert "automation-ctx" in tracker


async def test_tracker_remembers_script_started_context(hass: HomeAssistant) -> None:
    tracker = AutomationContextTracker(hass)
    tracker.async_start()
    context = Context(id="script-ctx")

    hass.bus.async_fire(EVENT_SCRIPT_STARTED, {}, context=context)
    await hass.async_block_till_done()

    assert "script-ctx" in tracker


async def test_tracker_stops_listening_and_forgets_on_stop(hass: HomeAssistant) -> None:
    tracker = AutomationContextTracker(hass)
    tracker.async_start()
    hass.bus.async_fire(EVENT_AUTOMATION_TRIGGERED, {}, context=Context(id="ctx-1"))
    await hass.async_block_till_done()

    tracker.async_stop()

    assert "ctx-1" not in tracker
    hass.bus.async_fire(EVENT_AUTOMATION_TRIGGERED, {}, context=Context(id="ctx-2"))
    await hass.async_block_till_done()
    assert "ctx-2" not in tracker


async def test_tracker_evicts_oldest_context_beyond_max_size(hass: HomeAssistant) -> None:
    tracker = AutomationContextTracker(hass, max_size=2)
    tracker.async_start()

    for context_id in ("ctx-1", "ctx-2", "ctx-3"):
        hass.bus.async_fire(EVENT_AUTOMATION_TRIGGERED, {}, context=Context(id=context_id))
        await hass.async_block_till_done()

    assert "ctx-1" not in tracker
    assert "ctx-2" in tracker
    assert "ctx-3" in tracker
