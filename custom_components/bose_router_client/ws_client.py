"""Thin async WebSocket client for the bose_router_app App.

Phase 2 protocol is simple request/response over one persistent connection
(no request IDs yet — one in-flight request at a time, enforced by a lock).
Good enough to prove the full HA <-> App <-> Bose round trip; a later phase
can add request IDs for real concurrency if the entity count grows enough
to need it.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

_LOGGER = logging.getLogger(__name__)


class BoseRouterAppError(Exception):
    """Raised when the App returns success: false, or the connection fails."""


class BoseRouterAppClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._lock = asyncio.Lock()

    async def async_connect(self) -> None:
        self._ws = await websockets.connect(self.base_url, open_timeout=5)

    async def async_close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def async_send(self, command: str, *, device: str | None = None, args: dict | None = None) -> object:
        payload: dict = {"command": command}
        if device:
            payload["device"] = device
        if args:
            payload["args"] = args
        raw_payload = json.dumps(payload)

        async with self._lock:
            if self._ws is None:
                await self.async_connect()
            try:
                await self._ws.send(raw_payload)
                raw = await self._ws.recv()
            except websockets.ConnectionClosed:
                # The `.closed` attribute this used to check for pre-flight
                # doesn't exist on newer websockets versions' connection
                # object (AttributeError, confirmed live) — reconnecting
                # reactively on ConnectionClosed instead is both simpler and
                # robust across library versions.
                _LOGGER.debug("WebSocket connection to App was closed, reconnecting")
                await self.async_connect()
                await self._ws.send(raw_payload)
                raw = await self._ws.recv()
        response = json.loads(raw)
        if not response.get("success"):
            raise BoseRouterAppError(response.get("error") or "unknown_error")
        return response.get("result")

    async def async_list_devices(self) -> list[str]:
        return await self.async_send("devices")
