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

# Market values published by Terna and ARERA (engine.published), shared by
# every supply. A workflow of the repository keeps them on a branch of their
# own; the integration ships a copy for when the download does not work.
PUBLISHED_URL = (
    "https://raw.githubusercontent.com/giorgioparri/electr-it-ha/"
    "market-data/market_values.json"
)
PUBLISHED_FILE = "market_values.json"
PUBLISHED_STORAGE_KEY = f"{DOMAIN}.market_values"
PUBLISHED_REFRESH_HOURS = 24
PUBLISHED_RETRY_MINUTES = 60
PUBLISHED_TIMEOUT_SECONDS = 30
# Days without a good download before a repair issue says so.
PUBLISHED_STALE_DAYS = 3
ISSUE_PUBLISHED_DOWNLOAD = "published_download_failed"
# Days after the month change a month waits for an official value of its own,
# which ARERA publishes only once the month is over.
PUBLISHED_CLOSE_WAIT_DAYS = 15

# Offers of the Portale Offerte (engine.catalogue), converted by a workflow of
# the repository and kept in the offers/ folder of the same branch, split by
# the hash of the offer code. Downloaded only while a contract is set up: the
# index, then the one file of the code looked up (a few hundred KB). Both are
# kept in memory for a while, so a second lookup downloads nothing it has.
OFFERS_BASE_URL = (
    "https://raw.githubusercontent.com/giorgioparri/electr-it-ha/market-data/offers/"
)
OFFERS_TIMEOUT_SECONDS = 30
OFFERS_CACHE_MINUTES = 10
