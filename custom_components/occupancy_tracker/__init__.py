"""The Occupancy Tracker integration.

Registry sync, topology store, the occupancy engine, signal ingestion,
provenance resolution, zone-presence fusion, the sensor/binary_sensor entity
platforms, the topology editor's WebSocket API (websocket_api.py), and its
frontend panel (panel.py, www/) are all wired up — see docs/STATUS.md for
exactly what that does and doesn't cover yet (notably: the engine's graph
and signal ingestion's subscriptions are built once at setup from that
moment's topology, not live-updated in-place on later topology edits —
SPEC.md §7.3 explicitly allows "immediately (or on next reload)", and both
an options-flow change and a websocket topology save trigger an automatic
reload to deliver that).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CLEAR_HOUSE_WHEN_ALL_AWAY,
    CONF_CONFIRMED_FRESHNESS_WINDOW,
    CONF_DECAY_GRACE_PERIOD,
    CONF_HOUSEHOLD_SIZE_HINT,
    CONF_LONG_LATCHED_REVIEW_THRESHOLD,
    CONF_NEAR_HOUSE_ZONES,
    CONF_PRE_ARM_WINDOW,
    CONF_TRACKED_PERSONS,
    CONF_TRANSIT_AREA_HOP_EXTENSION,
    CONF_TRANSIT_CONFIRMATION_WINDOW,
    CONF_UNCERTAIN_BIRTH_RESOLUTION_DELAY,
    CONF_ZONE_AWAY_CLEAR_DELAY,
    DOMAIN,
)
from .engine_adapter import build_house_graph
from .learned_timing_store import LearnedTimingStore
from .occupancy_engine import EngineConfig, OccupancyEngine
from .panel import async_setup as async_setup_panel
from .registry_sync import HouseShape, RegistrySync
from .services import async_setup as async_setup_services
from .signal_ingestion import SignalIngestion
from .topology_store import TopologyData, TopologyStore, active_area_ids
from .websocket_api import async_setup as async_setup_websocket_api
from .zone_fusion import ZoneFusion, ZoneFusionConfig

_PLATFORMS = (Platform.SENSOR, Platform.BINARY_SENSOR)


@dataclass
class OccupancyTrackerRuntimeData:
    """Runtime objects shared across this config entry (docs/ARCHITECTURE.md §1)."""

    registry_sync: RegistrySync
    topology_store: TopologyStore
    learned_timing_store: LearnedTimingStore
    engine: OccupancyEngine
    signal_ingestion: SignalIngestion
    zone_fusion: ZoneFusion


type OccupancyTrackerConfigEntry = ConfigEntry[OccupancyTrackerRuntimeData]


def _duration_option(options: Mapping[str, Any], key: str, default: timedelta) -> timedelta:
    """Read a `selector.DurationSelector` option (a plain dict, e.g.
    `{"minutes": 1, "seconds": 30}`) as a `timedelta`, falling back to
    `default` when unset (options-flow fields default to a duration-dict
    already matching the engine's own default, so this only actually differs
    from that default once a user has changed it).
    """
    value = options.get(key)
    if not value:
        return default
    return timedelta(**value)


def _prune_inactive_area_entities(
    hass: HomeAssistant,
    entry: OccupancyTrackerConfigEntry,
    house_shape: HouseShape,
    topology: TopologyData,
) -> None:
    """Remove per-Area entities for Areas with nothing selected any more.

    `sensor.py`/`binary_sensor.py` only *create* entities for
    `active_area_ids(topology)` — that alone leaves a stale, no-longer-
    recreated entity registered but unavailable rather than actually gone.
    Project-owner feedback: an Area the user has fully deselected (no
    activity evidence, not an access point) is one they've said they don't
    want tracked, and its old sensor/binary_sensor entities should disappear,
    not linger as unavailable clutter. Runs before platform forwarding so
    this reload's entity list is already correct by the time entities are
    (re)created.
    """
    active = active_area_ids(topology)
    entity_registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if registered.unique_id is None:
            continue
        for suffix in ("_occupant_count", "_occupied"):
            if not registered.unique_id.endswith(suffix):
                continue
            area_id = registered.unique_id.removeprefix(f"{entry.entry_id}_").removesuffix(suffix)
            # The `area_id in house_shape.areas` check is what tells a real
            # per-Area entity apart from the house-level
            # "{entry_id}_total_occupant_count" (which also ends in
            # "_occupant_count") without hardcoding the literal "total".
            if area_id in house_shape.areas and area_id not in active:
                entity_registry.async_remove(registered.entity_id)
            break


def _household_size_hint_option(options: Mapping[str, Any]) -> int | None:
    """Read the optional household-size hint, stored as a `float` by
    `selector.NumberSelector` (verified: `NumberSelector.__call__` always
    coerces to `float`, regardless of `step`) — `EngineConfig` wants a plain
    `int` count of people.
    """
    value = options.get(CONF_HOUSEHOLD_SIZE_HINT)
    return int(value) if value is not None else None


async def async_setup_entry(hass: HomeAssistant, entry: OccupancyTrackerConfigEntry) -> bool:
    """Set up Occupancy Tracker from a config entry."""
    # Hass-global, not per-entry — safe (and necessary) to call again on
    # every reload, since it just re-registers the same command handlers.
    async_setup_websocket_api(hass)
    # Also hass-global, but *not* safe to repeat blindly (panel_custom
    # raises on a duplicate frontend_url_path) — async_setup_panel guards
    # itself with its own hass.data sentinel, see panel.py.
    await async_setup_panel(hass)
    # Hass-global, and self-guarded against double-registration (see
    # services.py) — the manual occupant-count override is a per-entity
    # service registered separately in sensor.py's own async_setup_entry.
    async_setup_services(hass)

    # A single virtual "hub" device (docs/UX_GUIDELINES.md, project-owner
    # feedback: with the options flow back on the gear icon, there was no
    # navigation path at all from Settings -> Devices & Services back to the
    # topology panel, only the sidebar). `configuration_url`'s
    # "homeassistant://" scheme (verified: the same one core's own
    # backup/adguard integrations use, e.g. "homeassistant://config/backup")
    # is resolved by the frontend as an internal navigation, not a real
    # network request — `homeassistant://occupancy_tracker` points at this
    # panel's own `frontend_url_path` (panel.py). `entry_type=SERVICE` marks
    # it as non-physical, matching `iot_class: calculated` in manifest.json.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Occupancy Tracker",
        entry_type=dr.DeviceEntryType.SERVICE,
        configuration_url=f"homeassistant://{DOMAIN}",
    )

    registry_sync = RegistrySync(hass)
    registry_sync.async_setup()
    entry.async_on_unload(registry_sync.async_unload)

    topology_store = TopologyStore(hass, entry.entry_id)
    await topology_store.async_load()
    # Registries may have changed while HA was stopped; reconcile once now,
    # then again on every live registry change (docs/SPEC.md §5.3).
    await topology_store.async_reconcile_and_save(registry_sync.house_shape)

    learned_timing_store = LearnedTimingStore(hass, entry.entry_id)
    await learned_timing_store.async_load()
    learned_timing_store.reconcile(registry_sync.house_shape)

    _previous_house_shape = registry_sync.house_shape

    async def _reconcile_and_reload_if_stale(previous: HouseShape, current: HouseShape) -> None:
        """Reconcile the stored topology, then reload if the *live* engine/
        entities are now stale relative to it (SPEC.md §5.3: a registry
        change must not require the user to notice and manually fix things).

        `TopologyStore.reconcile()` only strips references that became
        outright invalid (an Area/entity removed, an entity moved to a
        different Area) — a pure rename changes nothing it tracks, since
        `area_id` (what topology data keys on) is stable across a rename;
        only `.name` (what the already-created sensor/binary_sensor entities
        captured once, at entity-creation time, into their `_attr_name`)
        changes. Both cases need a reload to actually show up: the
        reconciliation case rebuilds the engine's graph and signal
        ingestion's subscriptions from the cleaned-up topology; the rename
        case just needs the entity platforms to re-read the current name.
        Scoped to `active_area_ids` (areas that actually have entities) so a
        rename of some *other*, untracked Area in a busy house doesn't
        trigger a reload for no visible effect.
        """
        removed = await topology_store.async_reconcile_and_save(current)
        learned_timing_store.reconcile(current)
        active = active_area_ids(topology_store.topology)
        renamed = any(
            area_id in previous.areas
            and previous.areas[area_id].name != current.areas[area_id].name
            for area_id in active
        )
        if removed or renamed:
            await hass.config_entries.async_reload(entry.entry_id)

    @callback
    def _handle_house_shape_changed() -> None:
        nonlocal _previous_house_shape
        previous, current = _previous_house_shape, registry_sync.house_shape
        _previous_house_shape = current
        entry.async_create_task(
            hass,
            _reconcile_and_reload_if_stale(previous, current),
            "occupancy_tracker_topology_reconcile",
        )

    entry.async_on_unload(registry_sync.async_add_listener(_handle_house_shape_changed))

    _prune_inactive_area_entities(hass, entry, registry_sync.house_shape, topology_store.topology)

    # One shared engine instance for the lifetime of this entry
    # (docs/ARCHITECTURE.md §1.4-1.5) — entities read from it, never
    # recreate it. Its graph is a snapshot as of *now*; later topology edits
    # need a reload to take effect (see module docstring). Tunables (SPEC.md
    # §7.2) are read from options the same way, so an options-flow change to
    # any of them also needs a reload to take effect — the same mechanism
    # `_async_reload_entry` below already provides for the zone-fusion
    # settings.
    default_engine_config = EngineConfig()
    engine_config = EngineConfig(
        transit_confirmation_window=_duration_option(
            entry.options,
            CONF_TRANSIT_CONFIRMATION_WINDOW,
            default_engine_config.transit_confirmation_window,
        ),
        confirmed_freshness_window=_duration_option(
            entry.options,
            CONF_CONFIRMED_FRESHNESS_WINDOW,
            default_engine_config.confirmed_freshness_window,
        ),
        transit_area_hop_extension=_duration_option(
            entry.options,
            CONF_TRANSIT_AREA_HOP_EXTENSION,
            default_engine_config.transit_area_hop_extension,
        ),
        decay_grace_period=_duration_option(
            entry.options, CONF_DECAY_GRACE_PERIOD, default_engine_config.decay_grace_period
        ),
        long_latched_review_threshold=_duration_option(
            entry.options,
            CONF_LONG_LATCHED_REVIEW_THRESHOLD,
            default_engine_config.long_latched_review_threshold,
        ),
        uncertain_birth_resolution_delay=_duration_option(
            entry.options,
            CONF_UNCERTAIN_BIRTH_RESOLUTION_DELAY,
            default_engine_config.uncertain_birth_resolution_delay,
        ),
        household_size_hint=_household_size_hint_option(entry.options),
    )
    graph = build_house_graph(registry_sync.house_shape, topology_store.topology)
    # Seeded from whatever this house has already learned (docs/DECISIONS.md's
    # "learned transit timing" and "per-Area sensor reliability" entries) —
    # resumes refining from where it left off across a restart/reload rather
    # than forgetting it every time.
    engine = OccupancyEngine(
        graph,
        engine_config,
        learned_timing_store.data,
        learned_timing_store.sensor_reliability,
    )
    # Any signal could have recorded a new learned sample — schedule a
    # (debounced, see learned_timing_store.py) save after every one, rather
    # than trying to detect specifically which signals actually learned
    # something; a redundant save of unchanged data is harmless.
    entry.async_on_unload(
        engine.add_listener(
            lambda: learned_timing_store.async_schedule_save(
                engine.learned_transit_times, engine.learned_sensor_reliability
            )
        )
    )

    signal_ingestion = SignalIngestion(hass, engine)
    signal_ingestion.async_start(topology_store.topology)
    entry.async_on_unload(signal_ingestion.async_stop)

    zone_fusion = ZoneFusion(
        hass,
        tracked_entity_ids=entry.options.get(CONF_TRACKED_PERSONS, []),
        near_house_zone_ids=entry.options.get(CONF_NEAR_HOUSE_ZONES, []),
        config=ZoneFusionConfig(
            pre_arm_window=_duration_option(
                entry.options, CONF_PRE_ARM_WINDOW, ZoneFusionConfig().pre_arm_window
            ),
            clear_house_when_all_away=entry.options.get(
                CONF_CLEAR_HOUSE_WHEN_ALL_AWAY, ZoneFusionConfig().clear_house_when_all_away
            ),
            zone_away_clear_delay=_duration_option(
                entry.options,
                CONF_ZONE_AWAY_CLEAR_DELAY,
                ZoneFusionConfig().zone_away_clear_delay,
            ),
        ),
        engine=engine,
    )
    zone_fusion.async_start()
    entry.async_on_unload(zone_fusion.async_stop)

    entry.runtime_data = OccupancyTrackerRuntimeData(
        registry_sync=registry_sync,
        topology_store=topology_store,
        learned_timing_store=learned_timing_store,
        engine=engine,
        signal_ingestion=signal_ingestion,
        zone_fusion=zone_fusion,
    )

    # Zone-fusion settings (which persons/zones) live in options, and the
    # objects above are built from a snapshot of them at setup time — reload
    # the whole entry when they change rather than trying to live-patch a
    # running ZoneFusion instance (same "reload to pick up config changes"
    # precedent as the topology snapshot above).
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: OccupancyTrackerConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: OccupancyTrackerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
