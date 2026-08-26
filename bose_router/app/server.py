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

import aiohttp
import websockets
from websockets.server import WebSocketServerProtocol

from acoustid_lookup import async_identify_track, chromaprint_available
from airplay import AirPlayPlayer, AirPlayResumeStore
from bose_client import BoseSoundTouchClient
from stream_metadata import StreamMetadataTracker

_LOGGER = logging.getLogger(__name__)

# command -> (client_method_name, requires_args)
_READ_COMMANDS = {
    "info": "async_get_info",
    "volume": "async_get_volume",
    "now_playing": "async_get_now_playing",
    "presets": "async_get_presets",
    "sources": "async_get_sources",
    "zone": "async_get_zone",
    # "snapshot" is handled explicitly in _dispatch (merges in stream_meta/airplay).
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
        stream_meta_trackers: dict[str, StreamMetadataTracker] | None = None,
        session: aiohttp.ClientSession | None = None,
        acoustid_api_key: str = "",
    ) -> None:
        self._clients = clients
        self._airplay_players = airplay_players or {}
        self._resume_store = resume_store
        self._stream_meta_trackers = stream_meta_trackers or {}
        self._session = session
        self._acoustid_api_key = acoustid_api_key

    def _get_client(self, device_ip: str) -> BoseSoundTouchClient:
        client = self._clients.get(device_ip)
        if client is None:
            raise KeyError(f"Unknown device: {device_ip}")
        return client

    async def _handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        # DEBUG, not INFO: the HA client opens/closes a connection on every
        # poll cycle (every ~10-15s), so this fires constantly during normal
        # operation - not something worth surfacing at the default log level.
        _LOGGER.debug("Client connected: %s", websocket.remote_address)
        try:
            async for raw_message in websocket:
                await self._handle_message(websocket, raw_message)
        except websockets.ConnectionClosed:
            pass
        finally:
            _LOGGER.debug("Client disconnected: %s", websocket.remote_address)

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

        if command == "acoustid_status":
            await websocket.send(json.dumps({"success": True, "result": chromaprint_available()}))
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

        if command == "stream_meta":
            tracker = self._stream_meta_trackers.get(device_ip)
            return tracker.current_meta if tracker is not None else {}

        if command == "snapshot":
            snapshot = await client.async_fetch_snapshot()
            tracker = self._stream_meta_trackers.get(device_ip)
            player = self._airplay_players.get(device_ip)
            is_airplay_playing = player.is_playing if player is not None else False

            if tracker is not None and not is_airplay_playing:
                # Not our own AirPlay session (that already feeds the tracker
                # explicitly via play_stream) — pick up whatever URL the native
                # SoundTouch preset/source is actually playing, same source
                # production's coordinator polls (now_playing.location), so
                # track/cover metadata works no matter how playback started.
                location = str(snapshot.get("now_playing", {}).get("location") or "")
                if location and location != tracker.current_meta.get("stream_url"):
                    await tracker.async_set_stream(location)
                elif not location:
                    await tracker.async_clear()

            snapshot["stream_meta"] = tracker.current_meta if tracker is not None else {}
            resume_preset_id = self._resume_store.get(device_ip) if self._resume_store is not None else None
            snapshot["airplay"] = {
                "is_playing": is_airplay_playing,
                "preset_id": resume_preset_id if is_airplay_playing else None,
            }
            return snapshot

        if command == "identify_track":
            tracker = self._stream_meta_trackers.get(device_ip)
            stream_url = tracker.current_meta.get("stream_url") if tracker is not None else ""
            if not stream_url:
                raise ValueError("no_active_stream")
            if self._session is None:
                raise RuntimeError("no_http_session_configured")
            return await async_identify_track(self._session, stream_url, self._acoustid_api_key)

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
            await self._dispatch_select_preset(client, device_ip, int(args["preset_id"]))
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

        if command in ("set_zone", "add_zone_slaves", "remove_zone_slaves"):
            return await self._dispatch_zone(command, args)

        raise ValueError(f"unknown_command: {command}")

    async def _dispatch_select_preset(self, client: BoseSoundTouchClient, device_ip: str, preset_id: int) -> None:
        """Select a preset via this app's AirPlay pipeline, not UPnP or the
        native PRESET key.

        Two things ruled out UPnP AVTransport, both discovered live during
        Phase 7 testing: the native PRESET key can wedge the renderer
        (recovers only via a physical power cycle - see project memory
        avtransport-vs-native-preset), and pure AVTransport itself got a
        Wohnzimmer unit stuck in CurrentTransportState=PAUSED_PLAYBACK /
        now_playing source=INVALID_SOURCE across repeated retries - not a
        one-off. AirPlay (this app's existing, already-verified play_stream
        path from Phases 3-5) is what now_playing.source actually showed
        during every successful playback observed this session, so preset
        selection now just routes through it.
        """
        presets = await client.async_get_presets()
        preset = next((p for p in presets if int(p.get("id", 0)) == preset_id), None)
        if preset is None or not preset.get("location"):
            raise ValueError(f"preset_not_found_or_no_url: {preset_id}")

        # AirPlay's own connect-time default volume overrides whatever the
        # speaker was already set to (observed live: jumps to ~33%) unless
        # we explicitly re-assert it - so read the current volume first and
        # pass it straight back through, rather than accepting AirPlay's
        # default or hard-coding one of our own.
        try:
            current_volume = await client.async_get_volume()
            volume_percent = int(current_volume["actual"])
        except Exception as err:
            _LOGGER.debug("Could not read current volume before preset switch on %s: %s", device_ip, err)
            volume_percent = None

        await self._dispatch_airplay(
            device_ip,
            "play_stream",
            {
                "url": preset["location"],
                "title": preset.get("item_name", ""),
                "preset_id": preset_id,
                "volume_percent": volume_percent,
            },
        )

    async def async_resume_airplay_devices(self) -> None:
        """Replay whatever preset was last selected via AirPlay on each
        device, if anything - called once at App startup. Mirrors
        production bose_preset_router's async_resume_airplay_devices: skip
        (and clear the stale entry for) any device that's actually in
        standby now, since that means it was turned off some other way
        (Bose app, physical button) since the last resume-store write, and
        resuming it would un-intentionally power it back on.
        """
        if self._resume_store is None:
            return
        for device_ip, client in self._clients.items():
            preset_id = self._resume_store.get(device_ip)
            if preset_id is None:
                continue
            try:
                now_playing = await client.async_get_now_playing()
            except Exception as err:
                _LOGGER.debug("Could not check power state for AirPlay resume on %s: %s", device_ip, err)
                continue
            if str(now_playing.get("source", "")).upper() in ("STANDBY", ""):
                _LOGGER.info(
                    "Skipping AirPlay resume for %s (in standby - turned off since last playing preset %s)",
                    device_ip, preset_id,
                )
                await self._resume_store.async_clear(device_ip)
                continue
            _LOGGER.info("Resuming AirPlay preset %s for %s after App start", preset_id, device_ip)
            try:
                await self._dispatch_select_preset(client, device_ip, preset_id)
            except Exception as err:
                _LOGGER.warning("AirPlay resume failed for %s preset %s: %s", device_ip, preset_id, err)

    async def _dispatch_zone(self, command: str, args: dict) -> None:
        """Zone commands take IPs ("master_ip", "member_ips") and resolve each
        to its Bose deviceID internally — Bose's setZone/addZoneSlave/
        removeZoneSlave endpoints require deviceIDs, but callers shouldn't
        need to know or cache them.
        """
        master_ip = str(args["master_ip"])
        member_ips = [str(ip) for ip in args.get("member_ips") or []]

        master_client = self._get_client(master_ip)
        master_info = await master_client.async_get_info()
        master_device_id = str(master_info["device_id"])

        members: list[dict[str, str]] = []
        for member_ip in member_ips:
            member_client = self._get_client(member_ip)
            member_info = await member_client.async_get_info()
            members.append({"ip_address": member_ip, "device_id": str(member_info["device_id"])})

        if command == "set_zone":
            await master_client.async_set_zone(master_device_id=master_device_id, members=members)
        elif command == "add_zone_slaves":
            await master_client.async_add_zone_slaves(master_device_id=master_device_id, members=members)
        elif command == "remove_zone_slaves":
            await master_client.async_remove_zone_slaves(master_device_id=master_device_id, members=members)
        return None

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
            tracker = self._stream_meta_trackers.get(device_ip)
            if tracker is not None:
                await tracker.async_clear()
            return None

        if command == "play_stream":
            url = str(args["url"])
            started = await player.play(
                url,
                title=str(args.get("title", "")),
                artist=str(args.get("artist", "")),
                album=str(args.get("album", "")),
                volume_percent=args.get("volume_percent"),
            )
            if started:
                if self._resume_store is not None and "preset_id" in args:
                    await self._resume_store.async_set(device_ip, int(args["preset_id"]))
                tracker = self._stream_meta_trackers.get(device_ip)
                if tracker is not None:
                    await tracker.async_set_stream(url)
            return {"started": started}

        raise ValueError(f"unknown_command: {command}")

    async def serve_forever(self, *, port: int) -> None:
        async with websockets.serve(self._handle_connection, "0.0.0.0", port):
            _LOGGER.info("WebSocket server listening on port %d", port)
            await asyncio.Future()  # run forever
