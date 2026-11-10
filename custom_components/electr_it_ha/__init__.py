# SPDX-License-Identifier: GPL-3.0-or-later
"""electrITHA: near-real-time cost of grid electricity under Italian contracts."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import ElectrIthaConfigEntry, ElectrIthaCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ElectrIthaConfigEntry) -> bool:
    """Set up one supply from its config entry."""
    coordinator = ElectrIthaCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ElectrIthaConfigEntry) -> bool:
    """Unload a supply; the coordinator saves its counters on shutdown."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
