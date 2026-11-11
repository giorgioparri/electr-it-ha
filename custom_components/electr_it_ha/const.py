# SPDX-License-Identifier: GPL-3.0-or-later
"""Constants for the electrITHA integration."""

DOMAIN = "electr_it_ha"
NAME = "electrITHA"

# Config entry data.
CONF_CONTRACT = "contract"  # the validated contract, as a JSON-style dict
CONF_RESIDENT = "resident"
CONF_POWER_KW = "power_kw"
CONF_METERS = "meters"  # entity ids of the cumulative grid import sensors
# Market value name (see engine.market) -> entity id (str) or EUR/kWh (number).
CONF_MARKET = "market"

# Persistent storage of the per-band counters, one file per config entry.
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = DOMAIN
SAVE_DELAY_SECONDS = 60

# How often prices and month-to-date cost are recomputed when no meter changes.
UPDATE_INTERVAL_SECONDS = 60
