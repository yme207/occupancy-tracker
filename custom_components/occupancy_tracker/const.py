"""Constants for the Occupancy Tracker integration."""

DOMAIN = "occupancy_tracker"

#: Options-flow key: person/device_tracker entities tracked for zone-presence
#: fusion (SPEC.md §6.7, §7.2).
CONF_TRACKED_PERSONS = "tracked_persons"
#: Options-flow key: zone entities the user has picked as "near the house"
#: for pre-arming (SPEC.md §6.7 — explicitly user-picked, not auto-detected
#: by proximity to zone.home's radius).
CONF_NEAR_HOUSE_ZONES = "near_house_zones"
