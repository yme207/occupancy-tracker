"""Tests for config-entry setup/unload wiring (custom_components/occupancy_tracker/__init__.py).

Uses the real `hass.config_entries.async_setup()`/`async_unload()` flow
(needs `enable_custom_integrations` so the loader can find this integration
on disk) rather than calling `async_setup_entry`/`async_unload_entry`
directly, since Phase 4 forwards to real sensor/binary_sensor platforms that
only that flow discovers.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.occupancy_tracker import OccupancyTrackerRuntimeData
from custom_components.occupancy_tracker.const import (
    CONF_CONFIRMED_FRESHNESS_WINDOW,
    CONF_HOUSEHOLD_SIZE_HINT,
    CONF_PRE_ARM_WINDOW,
    CONF_TRACKED_PERSONS,
    CONF_TRANSIT_CONFIRMATION_WINDOW,
)
from custom_components.occupancy_tracker.learned_timing_store import learned_timing_to_dict
from custom_components.occupancy_tracker.topology_store import Connector, TopologyData


async def test_setup_entry_wires_up_runtime_data(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, OccupancyTrackerRuntimeData)


async def test_setup_entry_seeds_engine_with_persisted_learned_transit_times(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    """docs/DECISIONS.md's "learned transit timing" entry: previously
    learned data survives a reload (mirrors what a real HA restart does)
    and is fed straight into the new engine instance, not lost.
    """
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    seeded = {frozenset({kitchen.id, hallway.id}): (6, 12.5, 0.0)}
    await entry.runtime_data.learned_timing_store._store.async_save(learned_timing_to_dict(seeded))

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.engine.learned_transit_times() == seeded


async def test_setup_entry_registers_a_device_linking_back_to_the_panel(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, enable_custom_integrations: None
) -> None:
    """With the options flow back on the gear icon (see the config_panel_domain
    fix), the only other navigation path from Settings -> Devices & Services
    back to the topology panel is a device's "Visit" link — verify it's
    actually registered and points at the panel, not just that setup succeeds.
    """
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device = device_registry.async_get_device(identifiers={("occupancy_tracker", entry.entry_id)})
    assert device is not None
    assert device.configuration_url == "homeassistant://occupancy_tracker"


async def test_setup_entry_creates_house_level_entities_regardless(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    """The house-level entities always exist, even with zero configured areas —
    only *per-Area* entities depend on that Area actually being tracked
    (see test_setup_entry_skips_entities_for_untracked_areas below).
    """
    area_registry.async_get_or_create("Kitchen")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.total_occupant_count") is not None
    assert hass.states.get("binary_sensor.pre_armed") is not None


async def test_setup_entry_skips_entities_for_untracked_areas(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    """A room with nothing selected (no activity evidence, not an access
    point) is one the user hasn't opted into tracking — project-owner
    feedback that a permanently-zero sensor for it is clutter, not a useful
    default. Once it does have something selected, its entities appear (and
    a later full deselect removes them again — see test_topology_store.py's
    own `active_area_ids` tests plus the pruning test below).
    """
    kitchen = area_registry.async_get_or_create("Kitchen")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_occupant_count") is None
    assert hass.states.get("binary_sensor.kitchen_occupied") is None

    # RegistrySync's house shape (registry_sync.py's _build_house_shape) is
    # built purely from the entity registry, not from bare states — an
    # entity has to actually be registered with this Area to be a valid
    # activity-evidence selection, matching how a real HA-known entity would
    # get here (see test_entities.py's identical registration pattern).
    motion_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen-motion-unique-id", suggested_object_id="kitchen_motion"
    )
    entity_registry.async_update_entity(motion_entry.entity_id, area_id=kitchen.id)
    hass.states.async_set(motion_entry.entity_id, "off")
    await entry.runtime_data.topology_store.async_save(
        TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_occupant_count") is not None
    assert hass.states.get("binary_sensor.kitchen_occupied") is not None


async def test_reload_removes_entities_for_areas_deselected_entirely(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    """Deselecting a room's last piece of evidence should remove its
    now-stale entities, not leave them registered-but-unavailable forever.
    """
    kitchen = area_registry.async_get_or_create("Kitchen")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    motion_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen-motion-unique-id", suggested_object_id="kitchen_motion"
    )
    entity_registry.async_update_entity(motion_entry.entity_id, area_id=kitchen.id)
    hass.states.async_set(motion_entry.entity_id, "off")
    await entry.runtime_data.topology_store.async_save(
        TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_occupant_count") is not None

    await entry.runtime_data.topology_store.async_save(TopologyData())
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.kitchen_occupant_count") is None
    assert hass.states.get("binary_sensor.kitchen_occupied") is None
    # Not just absent from states — actually gone from the entity registry,
    # not merely unavailable (that's the whole point of pruning, not just
    # skipping re-creation).
    entity_registry = er.async_get(hass)
    assert (
        entity_registry.async_get_entity_id(
            "sensor", "occupancy_tracker", f"{entry.entry_id}_{kitchen.id}_occupant_count"
        )
        is None
    )


async def test_setup_entry_registry_sync_reacts_to_live_changes(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    attic = area_registry.async_get_or_create("Attic")
    await hass.async_block_till_done()

    assert attic.id in entry.runtime_data.registry_sync.house_shape.areas


async def test_setup_entry_reconciles_topology_on_live_area_removal(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    kitchen = area_registry.async_get_or_create("Kitchen")
    hallway = area_registry.async_get_or_create("Hallway")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    topology_store = entry.runtime_data.topology_store
    await topology_store.async_save(
        TopologyData(connectors=(Connector("c1", kitchen.id, hallway.id),))
    )

    area_registry.async_delete(kitchen.id)
    await hass.async_block_till_done()

    assert topology_store.topology.connectors == ()


async def test_renaming_an_active_area_reloads_so_entity_names_stay_current(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    enable_custom_integrations: None,
) -> None:
    """A rename doesn't invalidate anything `TopologyStore.reconcile()`
    tracks (area_id is stable across a rename, only `.name` changes) — but
    the already-created `sensor.kitchen_occupant_count` entity's friendly
    name was captured once, at entity-creation time, from the *old* name.
    Without an explicit reload-on-rename, SPEC.md §5.3's "must not require
    the user to notice and manually fix things" would be silently violated
    for the single most common registry edit a real household makes.
    """
    kitchen = area_registry.async_get_or_create("Kitchen")
    motion_entry = entity_registry.async_get_or_create(
        "binary_sensor", "test", "kitchen-motion-unique-id", suggested_object_id="kitchen_motion"
    )
    entity_registry.async_update_entity(motion_entry.entity_id, area_id=kitchen.id)
    hass.states.async_set(motion_entry.entity_id, "off")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.topology_store.async_save(
        TopologyData(area_entity_selections={kitchen.id: (motion_entry.entity_id,)})
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_occupant_count").attributes["friendly_name"] == (
        "Kitchen Occupant Count"
    )

    area_registry.async_update(kitchen.id, name="Cucina")
    await hass.async_block_till_done()

    state = hass.states.get("sensor.kitchen_occupant_count")
    assert state is not None
    assert state.attributes["friendly_name"] == "Cucina Occupant Count"


async def test_renaming_an_untracked_area_does_not_reload(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    """The rename-triggers-reload behavior is scoped to Areas that actually
    have entities exposed — a rename of some other, untracked Area (or one
    with nothing selected) has nothing user-visible to fix, so it shouldn't
    cost a reload (brief entity-unavailable window) for no effect.
    """
    garage = area_registry.async_get_or_create("Garage")
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    engine_before_rename = entry.runtime_data.engine

    area_registry.async_update(garage.id, name="Workshop")
    await hass.async_block_till_done()

    # A reload would have replaced runtime_data with a brand-new engine
    # instance — same instance survives means no reload happened.
    assert entry.runtime_data.engine is engine_before_rename


async def test_unload_entry_stops_registry_sync_listening(
    hass: HomeAssistant, area_registry: ar.AreaRegistry, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry_sync = entry.runtime_data.registry_sync

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED

    area_registry.async_get_or_create("Loft")
    await hass.async_block_till_done()

    assert registry_sync.house_shape.areas == {}


async def test_options_change_reloads_entry_and_zone_fusion_picks_it_up(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.zone_fusion.house_zone_corroboration().name == "UNKNOWN"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRACKED_PERSONS: ["person.alice"]}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    hass.states.async_set("person.alice", "home")
    await hass.async_block_till_done()

    assert entry.runtime_data.zone_fusion.house_zone_corroboration().name == "CORROBORATED"


async def test_setup_entry_reads_tunables_from_options(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """SPEC.md §7.2's engine/zone-fusion tunables must actually reach the
    objects built at setup, not just round-trip through the options flow's
    own storage (see test_config_flow.py for that half).
    """
    entry = MockConfigEntry(
        domain="occupancy_tracker",
        options={
            CONF_HOUSEHOLD_SIZE_HINT: 3,
            CONF_TRANSIT_CONFIRMATION_WINDOW: {"minutes": 5},
            CONF_CONFIRMED_FRESHNESS_WINDOW: {"hours": 1},
            CONF_PRE_ARM_WINDOW: {"minutes": 20},
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    engine = entry.runtime_data.engine
    assert engine.household_size_hint == 3
    assert engine._config.transit_confirmation_window == timedelta(minutes=5)
    assert engine._config.confirmed_freshness_window == timedelta(hours=1)
    assert entry.runtime_data.zone_fusion._config.pre_arm_window == timedelta(minutes=20)


async def test_setup_entry_defaults_tunables_when_options_unset(
    hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    entry = MockConfigEntry(domain="occupancy_tracker")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    engine = entry.runtime_data.engine
    assert engine.household_size_hint is None
    assert engine._config.transit_confirmation_window == timedelta(seconds=90)
    assert engine._config.confirmed_freshness_window == timedelta(minutes=10)
    assert entry.runtime_data.zone_fusion._config.pre_arm_window == timedelta(minutes=5)
