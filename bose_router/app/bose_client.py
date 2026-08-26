"""Framework-independent Bose SoundTouch HTTP client.

Ported from bose_preset_router's api.py (BoseSoundTouchApi), stripped of its
Home Assistant dependency (async_get_clientsession(hass) -> a plain
aiohttp.ClientSession this app owns and manages itself). UPnP AVTransport
streaming (async_play_upnp_stream/_build_didl) is intentionally left out —
that belongs to the later AirPlay/streaming phase, not device polling/control.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any
from xml.sax.saxutils import escape

import aiohttp

_LOGGER = logging.getLogger(__name__)


class BoseSoundTouchClient:
    def __init__(self, session: aiohttp.ClientSession, *, host: str, device_name: str = "") -> None:
        self._session = session
        self.host = host
        self.device_name = device_name

    async def _async_get_xml(self, path: str) -> ET.Element:
        url = f"http://{self.host}:8090/{path.lstrip('/')}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            payload = await response.text()
        return ET.fromstring(payload)

    async def _async_get_text(self, path: str) -> str:
        url = f"http://{self.host}:8090/{path.lstrip('/')}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            return await response.text()

    async def _async_post_xml(self, path: str, body: str) -> ET.Element:
        url = f"http://{self.host}:8090/{path.lstrip('/')}"
        async with self._session.post(
            url,
            data=body.encode("utf-8"),
            timeout=aiohttp.ClientTimeout(total=5),
            headers={"Content-Type": "application/xml"},
        ) as response:
            response.raise_for_status()
            payload = await response.text()
        return ET.fromstring(payload)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def async_get_info(self) -> dict[str, Any]:
        root = await self._async_get_xml("info")
        components: list[dict[str, str]] = []
        for component in root.findall("components/component"):
            components.append(
                {
                    "name": component.get("name", ""),
                    "device_id": component.get("deviceID", ""),
                    "type": component.get("type", ""),
                    "ip_address": component.get("ipAddress", ""),
                }
            )
        return {
            "name": root.findtext("name", default=""),
            "device_id": root.get("deviceID", ""),
            "type": root.findtext("type", default=""),
            "network_type": root.findtext("networkType", default=""),
            "marge_account_uuid": root.findtext("margeAccountUUID", default=""),
            "account": root.findtext("account", default=""),
            "components": components,
        }

    async def async_get_volume(self) -> dict[str, Any]:
        root = await self._async_get_xml("volume")
        return {
            "target": int(root.findtext("targetvolume", default="0") or 0),
            "actual": int(root.findtext("actualvolume", default="0") or 0),
            "muted": (root.findtext("muteenabled", default="false") or "").strip().lower() == "true",
        }

    async def async_get_now_playing(self) -> dict[str, Any]:
        root = await self._async_get_xml("now_playing")
        content_item = root.find("ContentItem")
        return {
            "source": root.get("source", ""),
            "source_account": root.get("sourceAccount", ""),
            "device_id": root.get("deviceID", ""),
            "item_name": root.findtext("itemName", default=""),
            "track": root.findtext("track", default=""),
            "artist": root.findtext("artist", default=""),
            "album": root.findtext("album", default=""),
            "station_name": root.findtext("stationName", default=""),
            "play_status": root.findtext("playStatus", default=""),
            "description": root.findtext("description", default=""),
            "image": root.findtext("art", default=""),
            "location": content_item.get("location", "") if content_item is not None else "",
            "source_type": content_item.get("source", "") if content_item is not None else "",
        }

    async def async_get_presets(self) -> list[dict[str, Any]]:
        root = await self._async_get_xml("presets")
        presets: list[dict[str, Any]] = []
        for preset in root.findall("preset"):
            content_item = preset.find("ContentItem")
            presets.append(
                {
                    "id": int(preset.get("id", "0") or 0),
                    "item_name": preset.findtext("ContentItem/itemName", default=""),
                    "image": preset.findtext("ContentItem/containerArt", default=""),
                    "source": content_item.get("source", "") if content_item is not None else "",
                    "location": content_item.get("location", "") if content_item is not None else "",
                    "source_account": (
                        content_item.get("sourceAccount", "") if content_item is not None else ""
                    ),
                }
            )
        return presets

    async def async_get_sources(self) -> list[dict[str, Any]]:
        root = await self._async_get_xml("sources")
        sources: list[dict[str, Any]] = []
        for source in root.findall("sourceItem"):
            sources.append(
                {
                    "source": source.get("source", ""),
                    "source_account": source.get("sourceAccount", ""),
                    "status": source.get("status", ""),
                    "location": source.get("location", ""),
                    "item_name": source.findtext("itemName", default=""),
                }
            )
        return sources

    async def async_get_zone(self) -> dict[str, Any]:
        root = await self._async_get_xml("zone")
        members: list[dict[str, str]] = []
        for member in root.findall("member"):
            members.append(
                {
                    "ip_address": member.get("ipaddress", ""),
                    "mac_address": member.get("macaddress", ""),
                    "device_id": (member.text or "").strip(),
                }
            )
        return {
            "master_id": root.get("master", ""),
            "sender_ip": root.get("senderIPAddress", ""),
            "members": members,
        }

    async def async_fetch_snapshot(self) -> dict[str, Any]:
        info, volume, now_playing, presets, sources, zone = await _gather_tolerant(
            self.async_get_info(),
            self.async_get_volume(),
            self.async_get_now_playing(),
            self.async_get_presets(),
            self.async_get_sources(),
            self.async_get_zone(),
        )
        return {
            "info": info,
            "volume": volume,
            "now_playing": now_playing,
            "presets": presets,
            "sources": sources,
            "zone": zone,
        }

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def async_send_key(self, key: str) -> None:
        for state in ("press", "release"):
            await self._async_post_xml("key", f'<key state="{state}" sender="Gabbo">{key}</key>')

    async def async_set_volume(self, level: int) -> None:
        clamped = max(0, min(100, int(level)))
        await self._async_post_xml("volume", f"<volume>{clamped}</volume>")

    async def async_set_muted(self, muted: bool) -> None:
        value = "true" if muted else "false"
        await self._async_post_xml("volume", f"<volume><muteenabled>{value}</muteenabled></volume>")

    async def async_standby(self) -> None:
        await self._async_get_text("standby")

    async def async_power_on(self) -> None:
        await self.async_send_key("POWER")

    async def async_play(self) -> None:
        await self._async_post_xml("userPlayControl", "<PlayControl>PLAY_CONTROL</PlayControl>")

    async def async_pause(self) -> None:
        await self._async_post_xml("userPlayControl", "<PlayControl>PAUSE_CONTROL</PlayControl>")

    async def async_stop(self) -> None:
        await self._async_post_xml("userPlayControl", "<PlayControl>STOP_CONTROL</PlayControl>")

    async def async_play_pause(self) -> None:
        await self._async_post_xml("userPlayControl", "<PlayControl>PLAY_PAUSE_CONTROL</PlayControl>")

    async def async_next_track(self) -> None:
        await self.async_send_key("NEXT_TRACK")

    async def async_previous_track(self) -> None:
        await self.async_send_key("PREV_TRACK")

    async def async_select_preset(self, preset_id: int) -> None:
        if preset_id < 1 or preset_id > 6:
            raise ValueError(f"Preset id must be between 1 and 6, got {preset_id}")
        await self.async_send_key(f"PRESET_{preset_id}")

    @staticmethod
    def _to_http(url: str) -> str:
        """Downgrade https:// to http:// — older Bose hardware rejects HTTPS on storePreset."""
        if url.startswith("https://"):
            return "http://" + url[len("https://"):]
        return url

    async def async_store_preset(self, preset_id: int, url: str, name: str) -> None:
        url = self._to_http(url)
        body = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            f'<preset id="{preset_id}">'
            f'<ContentItem source="UPNP" location="{escape(url)}" '
            f'sourceAccount="UPnPUserName" isPresetable="true">'
            f"<itemName>{escape(name)}</itemName>"
            "</ContentItem>"
            "</preset>"
        )
        await self._async_post_xml("storePreset", body)

    async def async_select_source(
        self, *, source: str, source_account: str = "", item_name: str = "", location: str = ""
    ) -> None:
        await self._async_post_xml(
            "select",
            (
                f'<ContentItem source="{escape(source)}" location="{escape(location)}" '
                f'sourceAccount="{escape(source_account)}" isPresetable="false">'
                f"<itemName>{escape(item_name)}</itemName>"
                "</ContentItem>"
            ),
        )

    @staticmethod
    def _build_zone_body(*, master_device_id: str, members: list[dict[str, str]]) -> str:
        member_xml = "".join(
            f'<member ipaddress="{escape(member["ip_address"])}">{escape(member["device_id"])}</member>'
            for member in members
        )
        return f'<zone master="{escape(master_device_id)}">{member_xml}</zone>'

    async def async_set_zone(self, *, master_device_id: str, members: list[dict[str, str]]) -> None:
        await self._async_post_xml("setZone", self._build_zone_body(master_device_id=master_device_id, members=members))

    async def async_add_zone_slaves(self, *, master_device_id: str, members: list[dict[str, str]]) -> None:
        await self._async_post_xml("addZoneSlave", self._build_zone_body(master_device_id=master_device_id, members=members))

    async def async_remove_zone_slaves(self, *, master_device_id: str, members: list[dict[str, str]]) -> None:
        await self._async_post_xml("removeZoneSlave", self._build_zone_body(master_device_id=master_device_id, members=members))


async def _gather_tolerant(*coros):
    """Like asyncio.gather, but a single failed call returns {} / [] instead
    of failing the whole snapshot — mirrors api.py's async_fetch_snapshot
    behavior of tolerating a device being briefly unreachable on one endpoint.
    """
    import asyncio

    results = await asyncio.gather(*coros, return_exceptions=True)
    out = []
    for result in results:
        if isinstance(result, Exception):
            _LOGGER.debug("Snapshot sub-request failed: %s", result)
            out.append({})
        else:
            out.append(result)
    return out
