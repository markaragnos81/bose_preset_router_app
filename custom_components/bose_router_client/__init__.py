from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BASE_URL, DATA_COORDINATORS, DATA_WS_CLIENT, DOMAIN
from .coordinator import BoseRouterDeviceCoordinator
from .ws_client import BoseRouterAppClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["media_player"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = BoseRouterAppClient(entry.data[CONF_BASE_URL])
    await client.async_connect()
    device_ips = await client.async_list_devices()

    coordinators = {
        device_ip: BoseRouterDeviceCoordinator(hass, client=client, device_ip=device_ip)
        for device_ip in device_ips
    }
    for coordinator in coordinators.values():
        await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_WS_CLIENT: client,
        DATA_COORDINATORS: coordinators,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        await entry_data[DATA_WS_CLIENT].async_close()
    return unload_ok
