"""Phase 1 WebSocket server: exposes a single "now_playing" command.

Protocol (deliberately minimal for Phase 1 — will grow into something closer
to Music Assistant's command/event JSON-RPC style once more commands exist):

  Client sends:  {"command": "now_playing"}
  Server replies: {"success": true, "result": {...}}  or  {"success": false, "error": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.server import WebSocketServerProtocol

from bose_client import BoseSoundTouchClient

_LOGGER = logging.getLogger(__name__)


class BoseRouterServer:
    def __init__(self, *, bose_client: BoseSoundTouchClient) -> None:
        self._bose_client = bose_client

    async def _handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        _LOGGER.info("Client connected: %s", websocket.remote_address)
        try:
            async for raw_message in websocket:
                await self._handle_message(websocket, raw_message)
        except websockets.ConnectionClosed:
            pass
        finally:
            _LOGGER.info("Client disconnected: %s", websocket.remote_address)

    async def _handle_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
            command = message.get("command")
        except (json.JSONDecodeError, AttributeError):
            await websocket.send(json.dumps({"success": False, "error": "invalid_message"}))
            return

        if command == "now_playing":
            try:
                result = await self._bose_client.async_get_now_playing()
                await websocket.send(json.dumps({"success": True, "result": result}))
            except Exception as err:
                _LOGGER.warning("now_playing lookup failed: %s", err)
                await websocket.send(json.dumps({"success": False, "error": str(err)}))
            return

        await websocket.send(json.dumps({"success": False, "error": f"unknown_command: {command}"}))

    async def serve_forever(self, *, port: int) -> None:
        async with websockets.serve(self._handle_connection, "0.0.0.0", port):
            _LOGGER.info("WebSocket server listening on port %d", port)
            await asyncio.Future()  # run forever
