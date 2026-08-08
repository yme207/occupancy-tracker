"""The Occupancy Tracker integration.

Phase 4: registry sync, topology store, the occupancy engine, signal
ingestion, and the sensor/binary_sensor entity platforms are all wired up —
see docs/STATUS.md for exactly what that does and doesn't cover yet
(notably: the engine's graph and signal ingestion's subscriptions are built
once at setup from that moment's topology, not live-updated on later
topology edits — SPEC.md §7.3 explicitly allows "immediately (or on next
reload)"). Provenance resolution, zone-presence fusion, and the topology
editor's WebSocket API land in later phases.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .engine_adapter import build_house_graph
from .occupancy_engine import OccupancyEngine
from .registry_sync import RegistrySync
from .signal_ingestion import SignalIngestion
from .topology_store import TopologyStore

_PLATFORMS = (Platform.SENSOR, Platform.BINARY_SENSOR)


@dataclass
class OccupancyTrackerRuntimeData:
    """Runtime objects shared across this config entry (docs/ARCHITECTURE.md §1)."""

    registry_sync: RegistrySync
    topology_store: TopologyStore
    engine: OccupancyEngine
    signal_ingestion: SignalIngestion


type OccupancyTrackerConfigEntry = ConfigEntry[OccupancyTrackerRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: OccupancyTrackerConfigEntry) -> bool:
    """Set up Occupancy Tracker from a config entry."""
    registry_sync = RegistrySync(hass)
    registry_sync.async_setup()
    entry.async_on_unload(registry_sync.async_unload)

    topology_store = TopologyStore(hass, entry.entry_id)
    await topology_store.async_load()
    # Registries may have changed while HA was stopped; reconcile once now,
    # then again on every live registry change (docs/SPEC.md §5.3).
    await topology_store.async_reconcile_and_save(registry_sync.house_shape)

    @callback
    def _handle_house_shape_changed() -> None:
        entry.async_create_task(
            hass,
            topology_store.async_reconcile_and_save(registry_sync.house_shape),
            "occupancy_tracker_topology_reconcile",
        )

    entry.async_on_unload(registry_sync.async_add_listener(_handle_house_shape_changed))

    # One shared engine instance for the lifetime of this entry
    # (docs/ARCHITECTURE.md §1.4-1.5) — entities read from it, never
    # recreate it. Its graph is a snapshot as of *now*; later topology edits
    # need a reload to take effect (see module docstring).
    graph = build_house_graph(registry_sync.house_shape, topology_store.topology)
    engine = OccupancyEngine(graph)

    signal_ingestion = SignalIngestion(hass, engine)
    signal_ingestion.async_start(topology_store.topology)
    entry.async_on_unload(signal_ingestion.async_stop)

    entry.runtime_data = OccupancyTrackerRuntimeData(
        registry_sync=registry_sync,
        topology_store=topology_store,
        engine=engine,
        signal_ingestion=signal_ingestion,
    )

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OccupancyTrackerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
