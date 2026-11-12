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

# Kinds of config entry. Entries without the key are supplies (version 0.1.0).
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_SUPPLY = "supply"
ENTRY_TYPE_GME = "gme_account"

# The GME account: one per installation, shared by every supply.
GME_UNIQUE_ID = "gme_account"
GME_STORAGE_KEY = f"{DOMAIN}.gme_prices"
# Day-ahead prices for tomorrow are published around 13:00 Italian time.
GME_PUBLICATION_HOUR = 14
GME_UPDATE_INTERVAL_MINUTES = 60
GME_TIMEOUT_SECONDS = 60
# Market sensors that report the month they refer to, as YYYY-MM.
ATTR_MONTH = "month"
