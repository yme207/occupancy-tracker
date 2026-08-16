"""Constants for the Occupancy Tracker integration."""

DOMAIN = "occupancy_tracker"

#: Options-flow key: person/device_tracker entities tracked for zone-presence
#: fusion (SPEC.md §6.7, §7.2).
CONF_TRACKED_PERSONS = "tracked_persons"
#: Options-flow key: zone entities the user has picked as "near the house"
#: for pre-arming (SPEC.md §6.7 — explicitly user-picked, not auto-detected
#: by proximity to zone.home's radius).
CONF_NEAR_HOUSE_ZONES = "near_house_zones"
#: Options-flow key: optional whole-house "typical household size" hint
#: (SPEC.md §6.4, §7.2) — tunes confidence only, never caps the actual count.
CONF_HOUSEHOLD_SIZE_HINT = "household_size_hint"
#: Options-flow key: `EngineConfig.transit_confirmation_window` (SPEC.md §7.2's
#: "transit confirmation/grace windows"), stored as a `selector.DurationSelector`
#: dict (e.g. {"minutes": 1, "seconds": 30}), converted to a `timedelta` at
#: entry setup.
CONF_TRANSIT_CONFIRMATION_WINDOW = "transit_confirmation_window"
#: Options-flow key: `EngineConfig.confirmed_freshness_window` (SPEC.md §7.2),
#: same duration-dict storage as above.
CONF_CONFIRMED_FRESHNESS_WINDOW = "confirmed_freshness_window"
#: Options-flow key: `ZoneFusionConfig.pre_arm_window` (SPEC.md §6.7/§7.2),
#: same duration-dict storage as above.
CONF_PRE_ARM_WINDOW = "pre_arm_window"
#: Options-flow key: `EngineConfig.transit_area_hop_extension` (docs/DECISIONS.md's
#: "area-kind classification" entry), same duration-dict storage as above.
CONF_TRANSIT_AREA_HOP_EXTENSION = "transit_area_hop_extension"
#: Options-flow key: `EngineConfig.decay_grace_period` (docs/DECISIONS.md's
#: decay entry), same duration-dict storage as above.
CONF_DECAY_GRACE_PERIOD = "decay_grace_period"
#: Options-flow key: `EngineConfig.long_latched_review_threshold` (docs/DECISIONS.md's
#: decay entry), same duration-dict storage as above.
CONF_LONG_LATCHED_REVIEW_THRESHOLD = "long_latched_review_threshold"
#: Options-flow key: `EngineConfig.uncertain_birth_resolution_delay`
#: (docs/DECISIONS.md's "uncertain births" entry), same duration-dict
#: storage as above.
CONF_UNCERTAIN_BIRTH_RESOLUTION_DELAY = "uncertain_birth_resolution_delay"
