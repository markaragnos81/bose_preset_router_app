"""Advertise this app over mDNS so the thin HA integration can find it —
same pattern Music Assistant's server uses (ServerInfoMessage properties:
server_id, server_version, base_url), verified in home-assistant/core's
music_assistant/config_flow.py before building this.
"""
from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

SERVICE_TYPE = "_bose-router._tcp.local."


class ZeroconfAdvertiser:
    def __init__(self, *, server_id: str, server_version: str, port: int) -> None:
        self._server_id = server_id
        self._server_version = server_version
        self._port = port
        self._aiozc: AsyncZeroconf | None = None
        self._service_info: ServiceInfo | None = None

    async def async_start(self) -> None:
        self._aiozc = AsyncZeroconf()
        local_ip = socket.gethostbyname(socket.gethostname())
        self._service_info = ServiceInfo(
            SERVICE_TYPE,
            name=f"{self._server_id}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=self._port,
            properties={
                "server_id": self._server_id,
                "server_version": self._server_version,
                "base_url": f"ws://{local_ip}:{self._port}",
            },
            server=f"{self._server_id}.local.",
        )
        await self._aiozc.async_register_service(self._service_info)
        _LOGGER.info("Advertising via mDNS: %s on %s:%d", self._server_id, local_ip, self._port)

    async def async_stop(self) -> None:
        if self._aiozc is not None and self._service_info is not None:
            await self._aiozc.async_unregister_service(self._service_info)
            await self._aiozc.async_close()
