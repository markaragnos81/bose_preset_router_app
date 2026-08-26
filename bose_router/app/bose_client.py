"""Minimal, framework-independent Bose SoundTouch HTTP client.

Phase 1 scope: just enough to poll /now_playing. Ported from
bose_preset_router's api.py (BoseSoundTouchApi.async_get_now_playing),
stripped of its Home Assistant dependency (async_get_clientsession(hass) ->
a plain aiohttp.ClientSession this app owns and manages itself).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import aiohttp


class BoseSoundTouchClient:
    def __init__(self, session: aiohttp.ClientSession, *, host: str) -> None:
        self._session = session
        self.host = host

    async def _async_get_xml(self, path: str) -> ET.Element:
        url = f"http://{self.host}:8090/{path.lstrip('/')}"
        async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
            response.raise_for_status()
            payload = await response.text()
        return ET.fromstring(payload)

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
