"""Config flow — Zeroconf discovery + manual URL fallback.

Mirrors homeassistant/components/music_assistant/config_flow.py's pattern
(verified against home-assistant/core before writing this): the App
advertises itself via mDNS with server_id/server_version/base_url
properties, HA's zeroconf integration dispatches async_step_zeroconf
automatically (manifest.json declares the service type), and a manual
step covers the case where discovery doesn't fire (different subnet, no
multicast reachability, etc.).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import CONF_BASE_URL, CONF_SERVER_ID, DOMAIN
from .ws_client import BoseRouterAppClient, BoseRouterAppError

_LOGGER = logging.getLogger(__name__)


class BoseRouterClientConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._base_url: str | None = None
        self._server_id: str | None = None

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> FlowResult:
        properties = discovery_info.properties
        base_url = properties.get("base_url")
        server_id = properties.get("server_id")
        if not base_url or not server_id:
            return self.async_abort(reason="invalid_discovery_info")

        await self.async_set_unique_id(server_id)
        self._abort_if_unique_id_configured()

        self._base_url = base_url
        self._server_id = server_id
        self.context["title_placeholders"] = {"name": f"Bose Router App ({server_id})"}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self._async_finish()

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"base_url": self._base_url or ""},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip()
            client = BoseRouterAppClient(base_url)
            try:
                await client.async_connect()
                devices = await client.async_list_devices()
            except (BoseRouterAppError, OSError) as err:
                _LOGGER.debug("Manual connection test failed for %s: %s", base_url, err)
                errors["base"] = "cannot_connect"
            else:
                await client.async_close()
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._base_url = base_url
                    self._server_id = base_url  # no mDNS server_id available via manual entry
                    await self.async_set_unique_id(base_url)
                    self._abort_if_unique_id_configured()
                    return await self._async_finish()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_BASE_URL): str}),
            errors=errors,
        )

    async def _async_finish(self) -> FlowResult:
        return self.async_create_entry(
            title=f"Bose Router App ({self._server_id})",
            data={CONF_BASE_URL: self._base_url, CONF_SERVER_ID: self._server_id},
        )
