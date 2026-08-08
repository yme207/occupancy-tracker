"""Tests for zone-presence fusion (docs/SPEC.md §6.7)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.occupancy_tracker.zone_fusion import (
    ZoneCorroboration,
    ZoneFusion,
    ZoneFusionConfig,
    ZoneMembership,
    classify_zone_membership,
)

# -- classify_zone_membership: pure, no HA instance needed --


def test_home_state_is_classified_home() -> None:
    state = State("person.alice", "home")

    assert classify_zone_membership(state, near_house_zone_ids=set()) is ZoneMembership.HOME


def test_in_zones_matching_a_near_house_zone_is_classified_near_house() -> None:
    state = State("person.alice", "not_home", attributes={"in_zones": ["zone.front_yard"]})

    membership = classify_zone_membership(state, near_house_zone_ids={"zone.front_yard"})

    assert membership is ZoneMembership.NEAR_HOUSE


def test_in_zones_not_matching_any_near_house_zone_is_away() -> None:
    state = State("person.alice", "not_home", attributes={"in_zones": ["zone.work"]})

    membership = classify_zone_membership(state, near_house_zone_ids={"zone.front_yard"})

    assert membership is ZoneMembership.AWAY


def test_not_home_with_no_in_zones_attribute_is_away() -> None:
    state = State("person.alice", "not_home")

    membership = classify_zone_membership(state, near_house_zone_ids={"zone.front_yard"})

    assert membership is ZoneMembership.AWAY


# -- ZoneFusion: needs a real hass to fire state-changed events --


async def test_no_tracked_entities_is_unknown_corroboration(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(hass, tracked_entity_ids=(), near_house_zone_ids=())
    zone_fusion.async_start()

    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.UNKNOWN


async def test_tracked_entity_never_reported_is_unknown_corroboration(
    hass: HomeAssistant,
) -> None:
    zone_fusion = ZoneFusion(hass, tracked_entity_ids=("person.alice",), near_house_zone_ids=())
    zone_fusion.async_start()

    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.UNKNOWN


async def test_tracked_entity_home_corroborates(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(hass, tracked_entity_ids=("person.alice",), near_house_zone_ids=())
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "home")
    await hass.async_block_till_done()

    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.CORROBORATED


async def test_all_tracked_entities_away_contradicts(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(
        hass, tracked_entity_ids=("person.alice", "person.bob"), near_house_zone_ids=()
    )
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "not_home")
    hass.states.async_set("person.bob", "not_home")
    await hass.async_block_till_done()

    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.CONTRADICTED


async def test_one_home_one_away_still_corroborates(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(
        hass, tracked_entity_ids=("person.alice", "person.bob"), near_house_zone_ids=()
    )
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "not_home")
    hass.states.async_set("person.bob", "home")
    await hass.async_block_till_done()

    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.CORROBORATED


async def test_near_house_zone_entry_pre_arms(hass: HomeAssistant) -> None:
    config = ZoneFusionConfig(pre_arm_window=timedelta(minutes=5))
    zone_fusion = ZoneFusion(
        hass,
        tracked_entity_ids=("person.alice",),
        near_house_zone_ids=("zone.front_yard",),
        config=config,
    )
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "not_home", attributes={"in_zones": ["zone.front_yard"]})
    await hass.async_block_till_done()

    assert zone_fusion.is_pre_armed(dt_util.utcnow()) is True


async def test_pre_arm_expires_after_the_window(hass: HomeAssistant) -> None:
    config = ZoneFusionConfig(pre_arm_window=timedelta(minutes=5))
    zone_fusion = ZoneFusion(
        hass,
        tracked_entity_ids=("person.alice",),
        near_house_zone_ids=("zone.front_yard",),
        config=config,
    )
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "not_home", attributes={"in_zones": ["zone.front_yard"]})
    await hass.async_block_till_done()

    assert zone_fusion.is_pre_armed(dt_util.utcnow() + timedelta(minutes=6)) is False


async def test_zone_entry_that_is_not_near_house_does_not_pre_arm(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(
        hass, tracked_entity_ids=("person.alice",), near_house_zone_ids=("zone.front_yard",)
    )
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "not_home", attributes={"in_zones": ["zone.work"]})
    await hass.async_block_till_done()

    assert zone_fusion.is_pre_armed(dt_util.utcnow()) is False


async def test_home_zone_does_not_pre_arm(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(
        hass, tracked_entity_ids=("person.alice",), near_house_zone_ids=("zone.front_yard",)
    )
    zone_fusion.async_start()

    hass.states.async_set("person.alice", "home")
    await hass.async_block_till_done()

    assert zone_fusion.is_pre_armed(dt_util.utcnow()) is False


async def test_listener_is_called_on_tracked_entity_update(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(hass, tracked_entity_ids=("person.alice",), near_house_zone_ids=())
    zone_fusion.async_start()
    calls = 0

    def on_change() -> None:
        nonlocal calls
        calls += 1

    zone_fusion.add_listener(on_change)
    hass.states.async_set("person.alice", "home")
    await hass.async_block_till_done()

    assert calls == 1


async def test_async_stop_unsubscribes_and_clears_state(hass: HomeAssistant) -> None:
    zone_fusion = ZoneFusion(hass, tracked_entity_ids=("person.alice",), near_house_zone_ids=())
    zone_fusion.async_start()
    hass.states.async_set("person.alice", "home")
    await hass.async_block_till_done()
    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.CORROBORATED

    zone_fusion.async_stop()

    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.UNKNOWN
    hass.states.async_set("person.alice", "not_home")
    await hass.async_block_till_done()
    assert zone_fusion.house_zone_corroboration() is ZoneCorroboration.UNKNOWN
