# SPDX-License-Identifier: GPL-3.0-or-later
"""electrITHA: near-real-time cost of grid electricity under Italian contracts.

A config entry is either a supply or the GME account, which downloads the
PUN for every supply that chooses its sensors.
"""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ENTRY_TYPE, ENTRY_TYPE_GME
from .coordinator import ElectrIthaCoordinator
from .gme_client import GmeClient
from .gme_coordinator import GmeCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a supply or the GME account from its config entry."""
    coordinator: ElectrIthaCoordinator | GmeCoordinator
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_GME:
        client = GmeClient(
            async_get_clientsession(hass),
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
        )
        coordinator = GmeCoordinator(hass, entry, client)
    else:
        coordinator = ElectrIthaCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry; the coordinators save their data on shutdown."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
