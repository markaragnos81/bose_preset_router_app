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

from acoustid_lookup import async_identify_track
from airplay import AirPlayDiscovery, AirPlayPlayer, AirPlayResumeStore
from bose_client import BoseSoundTouchClient
from server import BoseRouterServer
from station_meta import async_resolve_station_meta
from stream_metadata import StreamMetadataTracker
from zeroconf_advertise import ZeroconfAdvertiser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger(__name__)

APP_VERSION = "0.9.2"


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
        "presets": [],
        "device_preset_overrides": [],
    }


def _resolve_device_presets(
    device_ip: str, global_presets: list[dict], overrides: list[dict]
) -> dict[int, tuple[str, str]]:
    """Merge global default presets with any per-device overrides for this IP.

    Config lives in the App's own Supervisor Configuration tab (like
    acoustid_api_key already does), not a HA config_flow/service — presets
    is the global default list, device_preset_overrides replaces individual
    slots (matched by ip + id) for devices that need something different.
    """
    resolved: dict[int, tuple[str, str]] = {}
    for preset in global_presets:
        try:
            preset_id = int(preset["id"])
        except (KeyError, ValueError, TypeError):
            continue
        if preset.get("url"):
            resolved[preset_id] = (str(preset["url"]), str(preset.get("name", "")))
    for override in overrides:
        if str(override.get("ip", "")) != device_ip:
            continue
        try:
            preset_id = int(override["id"])
        except (KeyError, ValueError, TypeError):
            continue
        if override.get("url"):
            resolved[preset_id] = (str(override["url"]), str(override.get("name", "")))
    return resolved


async def _apply_configured_presets(
    session: aiohttp.ClientSession,
    clients: dict[str, BoseSoundTouchClient],
    global_presets: list[dict],
    overrides: list[dict],
) -> None:
    """Push configured presets to each physical device on every App start —
    same mechanism production used (Bose's native storePreset endpoint),
    just driven by App config instead of a HA config_flow. `name` is
    optional in the config; when omitted it's resolved the same way
    station names already are elsewhere (Radio Browser -> live ICY
    icy-name -> hostname fallback) rather than requiring the user to type
    it in by hand.
    """
    name_cache: dict[str, str] = {}
    for device_ip, client in clients.items():
        resolved = _resolve_device_presets(device_ip, global_presets, overrides)
        for preset_id, (url, name) in resolved.items():
            if not name:
                if url not in name_cache:
                    try:
                        meta = await async_resolve_station_meta(session, url)
                        name_cache[url] = str(meta.get("name") or "")
                    except Exception as err:
                        _LOGGER.debug("Could not resolve a name for preset URL %s: %s", url, err)
                        name_cache[url] = ""
                name = name_cache[url]
            try:
                await client.async_store_preset(preset_id, url, name or f"Preset {preset_id}")
                _LOGGER.info("Applied preset %d on %s: %r (%s)", preset_id, device_ip, name, url)
            except Exception as err:
                _LOGGER.warning("Could not apply preset %d on %s: %s", preset_id, device_ip, err)


async def async_main() -> None:
    options = _load_options()
    all_devices = options.get("devices") or []
    devices = [d for d in all_devices if d.get("enabled", True)]
    for device in all_devices:
        if device not in devices:
            _LOGGER.info("Skipping disabled device: %s (%s)", device.get("name") or "?", device.get("ip"))
    ws_port = int(options.get("ws_port") or 8765)
    acoustid_api_key = str(options.get("acoustid_api_key") or "")
    global_presets = options.get("presets") or []
    device_preset_overrides = options.get("device_preset_overrides") or []

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

        async def _acoustid_identify(url: str) -> dict:
            return await async_identify_track(session, url, acoustid_api_key)

        acoustid_fallback = _acoustid_identify if acoustid_api_key else None

        stream_meta_trackers = {
            device["ip"]: StreamMetadataTracker(
                session,
                station_meta_resolver=_resolve,
                update_callback=_noop_update_callback,
                acoustid_resolver=acoustid_fallback,
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
        if global_presets or device_preset_overrides:
            asyncio.create_task(
                _apply_configured_presets(session, clients, global_presets, device_preset_overrides),
                name="apply_presets",
            )
        asyncio.create_task(server.async_resume_airplay_devices(), name="airplay_resume")
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
