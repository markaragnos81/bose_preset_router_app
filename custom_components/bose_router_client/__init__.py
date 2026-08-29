from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BASE_URL, DATA_CLIENTS, DATA_COORDINATORS, DOMAIN
from .coordinator import BoseRouterDeviceCoordinator
from .ws_client import BoseRouterAppClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["media_player"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # One WebSocket connection per device rather than one shared connection
    # for the whole entry: BoseRouterAppClient serializes every request
    # behind a single lock, so with a shared client, a slow request for one
    # device (e.g. an AirPlay preset switch that hits the discovery-fallback
    # path, which can take up to ~20s) blocked polling and control commands
    # for every other device too. Confirmed as the right threshold to fix
    # this at once the device count grew to 4 in daily use.
    discovery_client = BoseRouterAppClient(entry.data[CONF_BASE_URL])
    await discovery_client.async_connect()
    device_ips = await discovery_client.async_list_devices()
    await discovery_client.async_close()

    clients = {device_ip: BoseRouterAppClient(entry.data[CONF_BASE_URL]) for device_ip in device_ips}
    coordinators = {
        device_ip: BoseRouterDeviceCoordinator(hass, client=clients[device_ip], device_ip=device_ip)
        for device_ip in device_ips
    }
    for coordinator in coordinators.values():
        await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_CLIENTS: clients,
        DATA_COORDINATORS: coordinators,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        for client in entry_data[DATA_CLIENTS].values():
            await client.async_close()
    return unload_ok
