"""WebSocket server exposing Bose control/status over a small JSON protocol.

  Client sends:  {"command": "now_playing", "device": "192.168.20.139"}
  Server replies: {"success": true, "result": {...}}  or  {"success": false, "error": "..."}

"device" selects which configured Bose speaker (by IP) the command targets.
Read commands mirror api.py's BoseSoundTouchApi getters; control commands
mirror its setters. Kept as a flat command dispatch table rather than a
generic RPC framework — Phase 2 scope is proving full read/write control
works end-to-end, not protocol elegance.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.server import WebSocketServerProtocol

from airplay import AirPlayPlayer, AirPlayResumeStore
from bose_client import BoseSoundTouchClient

_LOGGER = logging.getLogger(__name__)

# command -> (client_method_name, requires_args)
_READ_COMMANDS = {
    "info": "async_get_info",
    "volume": "async_get_volume",
    "now_playing": "async_get_now_playing",
    "presets": "async_get_presets",
    "sources": "async_get_sources",
    "zone": "async_get_zone",
    "snapshot": "async_fetch_snapshot",
}

_CONTROL_COMMANDS = {
    "play": "async_play",
    "pause": "async_pause",
    "stop": "async_stop",
    "play_pause": "async_play_pause",
    "next_track": "async_next_track",
    "previous_track": "async_previous_track",
    "power_on": "async_power_on",
    "standby": "async_standby",
}


class BoseRouterServer:
    def __init__(
        self,
        *,
        clients: dict[str, BoseSoundTouchClient],
        airplay_players: dict[str, AirPlayPlayer] | None = None,
        resume_store: AirPlayResumeStore | None = None,
    ) -> None:
        self._clients = clients
        self._airplay_players = airplay_players or {}
        self._resume_store = resume_store

    def _get_client(self, device_ip: str) -> BoseSoundTouchClient:
        client = self._clients.get(device_ip)
        if client is None:
            raise KeyError(f"Unknown device: {device_ip}")
        return client

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
            device_ip = message.get("device")
            args = message.get("args") or {}
        except (json.JSONDecodeError, AttributeError):
            await websocket.send(json.dumps({"success": False, "error": "invalid_message"}))
            return

        if command == "devices":
            await websocket.send(json.dumps({"success": True, "result": list(self._clients)}))
            return

        try:
            client = self._get_client(device_ip)
        except KeyError as err:
            await websocket.send(json.dumps({"success": False, "error": str(err)}))
            return

        try:
            result = await self._dispatch(client, device_ip, command, args)
            await websocket.send(json.dumps({"success": True, "result": result}))
        except Exception as err:
            _LOGGER.warning("Command %r for %s failed: %s", command, device_ip, err)
            await websocket.send(json.dumps({"success": False, "error": str(err)}))

    async def _dispatch(self, client: BoseSoundTouchClient, device_ip: str, command: str, args: dict) -> object:
        if command in ("play_stream", "stop_stream", "stream_status"):
            return await self._dispatch_airplay(device_ip, command, args)

        if command in _READ_COMMANDS:
            return await getattr(client, _READ_COMMANDS[command])()

        if command in _CONTROL_COMMANDS:
            await getattr(client, _CONTROL_COMMANDS[command])()
            return None

        if command == "set_volume":
            await client.async_set_volume(int(args["level"]))
            return None
        if command == "set_muted":
            await client.async_set_muted(bool(args["muted"]))
            return None
        if command == "select_preset":
            await client.async_select_preset(int(args["preset_id"]))
            return None
        if command == "store_preset":
            await client.async_store_preset(int(args["preset_id"]), str(args["url"]), str(args.get("name", "")))
            return None
        if command == "select_source":
            await client.async_select_source(
                source=str(args["source"]),
                source_account=str(args.get("source_account", "")),
                item_name=str(args.get("item_name", "")),
                location=str(args.get("location", "")),
            )
            return None
        if command == "send_key":
            await client.async_send_key(str(args["key"]))
            return None

        raise ValueError(f"unknown_command: {command}")

    async def _dispatch_airplay(self, device_ip: str, command: str, args: dict) -> object:
        player = self._airplay_players.get(device_ip)
        if player is None:
            raise KeyError(f"No AirPlay player configured for device: {device_ip}")

        if command == "stream_status":
            return {"is_playing": player.is_playing}

        if command == "stop_stream":
            await player.stop()
            if self._resume_store is not None:
                await self._resume_store.async_clear(device_ip)
            return None

        if command == "play_stream":
            started = await player.play(
                str(args["url"]),
                title=str(args.get("title", "")),
                artist=str(args.get("artist", "")),
                album=str(args.get("album", "")),
                volume_percent=args.get("volume_percent"),
            )
            if started and self._resume_store is not None and "preset_id" in args:
                await self._resume_store.async_set(device_ip, int(args["preset_id"]))
            return {"started": started}

        raise ValueError(f"unknown_command: {command}")

    async def serve_forever(self, *, port: int) -> None:
        async with websockets.serve(self._handle_connection, "0.0.0.0", port):
            _LOGGER.info("WebSocket server listening on port %d", port)
            await asyncio.Future()  # run forever
