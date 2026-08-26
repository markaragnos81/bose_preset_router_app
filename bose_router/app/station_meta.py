"""Station name/logo resolution — ported from bose_preset_router's
coordinator.py (_async_resolve_station_meta), carrying over two fixes found
live in that project:

- v0.7.19: fall back to the stream's own live ICY icy-name when Radio
  Browser doesn't know the URL (e.g. a private/local proxy), instead of a
  name derived from the URL's hostname.
- v0.7.20: don't cache that hostname-derived placeholder permanently — if
  the URL becomes resolvable later (Radio Browser catches up, or the ICY
  fetch that failed at boot succeeds once the source is reachable), the next
  lookup should retry instead of being stuck on the placeholder forever.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import aiohttp

from radio_browser import async_fetch_icy_meta, async_lookup_radio_logo, async_lookup_station

_LOGGER = logging.getLogger(__name__)

_station_meta_cache: dict[str, dict[str, str]] = {}


def get_station_meta(url: str) -> dict[str, str]:
    return _station_meta_cache.get(url, {})


async def async_resolve_station_meta(session: aiohttp.ClientSession, url: str) -> dict[str, str]:
    if url in _station_meta_cache:
        return _station_meta_cache[url]

    meta = await async_lookup_station(session, url)
    if not meta:
        meta = {"name": "", "favicon": ""}

    used_hostname_fallback = False
    if not meta.get("name"):
        icy_name = ""
        try:
            icy = await async_fetch_icy_meta(session, url)
            icy_name = str(icy.get("icy_name") or "").strip()
        except Exception as err:
            _LOGGER.debug("ICY name lookup failed for %s: %s", url, err)
        if icy_name:
            meta["name"] = icy_name
        else:
            meta["name"] = _station_name_from_url(url)
            used_hostname_fallback = True

    try:
        logo = await async_lookup_radio_logo(session, meta.get("name", ""), url)
    except Exception as err:
        _LOGGER.debug("radio.net logo lookup failed for %s: %s", url, err)
        logo = ""
    if logo:
        meta["favicon"] = logo

    if not used_hostname_fallback:
        _station_meta_cache[url] = meta
    return meta


def _station_name_from_url(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
        skip = {"www", "stream", "streams", "live", "listen", "audio", "icecast", "ice", "cdn", "media"}
        for part in host.split("."):
            if part and part not in skip and not part.isdigit():
                return part.replace("-", " ").title()
    except Exception:
        pass
    return ""
