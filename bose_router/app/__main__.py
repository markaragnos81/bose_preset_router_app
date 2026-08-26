"""Entry point. Reads Supervisor App options from /data/options.json (the
standard mechanism per developers.home-assistant.io/docs/apps/configuration),
falling back to environment variables for local (non-Supervisor) testing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import aiohttp

from airplay import AirPlayDiscovery, AirPlayPlayer, AirPlayResumeStore
from bose_client import BoseSoundTouchClient
from server import BoseRouterServer
from station_meta import async_resolve_station_meta
from stream_metadata import StreamMetadataTracker
from zeroconf_advertise import ZeroconfAdvertiser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger(__name__)

APP_VERSION = "0.5.0"


async def _noop_update_callback(meta: dict) -> None:
    """StreamMetadataTracker refreshes current_meta internally either way;
    clients poll it on demand via the "stream_meta" command rather than this
    app pushing updates — no server-push/event protocol yet.
    """
OPTIONS_PATH = Path("/data/options.json")


def _load_options() -> dict:
    if OPTIONS_PATH.exists():
        return json.loads(OPTIONS_PATH.read_text())
    # Local (non-Supervisor) testing: BOSE_DEVICES="Büro:192.168.20.139,Wohnzimmer:192.168.20.20"
    devices = []
    for entry in os.environ.get("BOSE_DEVICES", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, ip = entry.partition(":")
        devices.append({"name": name.strip(), "ip": ip.strip()})
    return {
        "devices": devices,
        "ws_port": int(os.environ.get("WS_PORT", "8765")),
        "acoustid_api_key": os.environ.get("ACOUSTID_API_KEY", ""),
    }


async def async_main() -> None:
    options = _load_options()
    devices = options.get("devices") or []
    ws_port = int(options.get("ws_port") or 8765)
    acoustid_api_key = str(options.get("acoustid_api_key") or "")

    if not devices:
        raise SystemExit("At least one device is required (options.devices: [{name, ip}, ...])")

    server_id_seed = ",".join(sorted(d["ip"] for d in devices))
    server_id = f"bose-router-{uuid.uuid5(uuid.NAMESPACE_DNS, server_id_seed).hex[:8]}"

    airplay_discovery = AirPlayDiscovery()
    await airplay_discovery.async_start()

    resume_store = AirPlayResumeStore()
    await resume_store.async_load()

    async with aiohttp.ClientSession() as session:
        clients = {
            device["ip"]: BoseSoundTouchClient(session, host=device["ip"], device_name=device.get("name", ""))
            for device in devices
        }
        airplay_players = {
            device["ip"]: AirPlayPlayer(device["ip"], airplay_discovery) for device in devices
        }

        async def _resolve(url: str) -> dict:
            return await async_resolve_station_meta(session, url)

        stream_meta_trackers = {
            device["ip"]: StreamMetadataTracker(
                session, station_meta_resolver=_resolve, update_callback=_noop_update_callback
            )
            for device in devices
        }
        for ip, client in clients.items():
            _LOGGER.info("Configured device: %s (%s)", client.device_name or "?", ip)

        server = BoseRouterServer(
            clients=clients,
            airplay_players=airplay_players,
            resume_store=resume_store,
            stream_meta_trackers=stream_meta_trackers,
            session=session,
            acoustid_api_key=acoustid_api_key,
        )
        advertiser = ZeroconfAdvertiser(server_id=server_id, server_version=APP_VERSION, port=ws_port)

        await advertiser.async_start()
        try:
            await server.serve_forever(port=ws_port)
        finally:
            await advertiser.async_stop()
            for player in airplay_players.values():
                await player.stop()
            for tracker in stream_meta_trackers.values():
                await tracker.async_clear()
            await airplay_discovery.async_stop()


if __name__ == "__main__":
    asyncio.run(async_main())
