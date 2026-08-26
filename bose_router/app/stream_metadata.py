"""Continuously resolve station + track metadata for an active radio stream.
Ported from bose_preset_router's stream_metadata.py, with `hass` /
`hass.async_create_background_task` replaced by a plain aiohttp.ClientSession
this app owns and asyncio.create_task.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from radio_browser import async_fetch_icy_meta, async_lookup_track_art, parse_icy_stream_title

_LOGGER = logging.getLogger(__name__)

_TRACKER_INTERVAL_SECONDS = 15
# AcoustID fingerprinting is only tried when ICY genuinely has no real title
# (see async_fetch_icy_meta's multi-second listen — most stations that ever
# send song titles will be caught by that already). It's a last resort for
# stations that never embed song data at all, so it's cooldown-gated to keep
# free-tier API usage low rather than retried every _TRACKER_INTERVAL_SECONDS.
_ACOUSTID_FALLBACK_COOLDOWN_SECONDS = 180
_GENERIC_BRANDING_TOKENS = {
    "radio", "fm", "am", "dab", "stream", "streams", "live", "livestream",
    "music", "channel", "station", "hits", "rock", "pop", "dance", "mix",
    "best", "greatest", "chart", "charts",
}


class StreamMetadataTracker:
    """Continuously resolve station + track metadata for the active radio stream."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        station_meta_resolver: Callable[[str], Awaitable[dict[str, str]]],
        update_callback: Callable[[dict[str, Any]], Awaitable[None]],
        acoustid_resolver: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._session = session
        self._station_meta_resolver = station_meta_resolver
        self._update_callback = update_callback
        self._acoustid_resolver = acoustid_resolver
        self._stream_url = ""
        self._current_meta: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._last_acoustid_attempt: float = 0.0

    @property
    def current_meta(self) -> dict[str, Any]:
        return dict(self._current_meta)

    async def async_set_stream(self, url: str) -> dict[str, Any]:
        url = str(url or "").strip()
        if not url:
            await self.async_clear()
            return {}

        if url != self._stream_url:
            await self._async_stop_task()
            self._stream_url = url
            self._current_meta = {}

        meta = await self._async_build_metadata(url)
        self._current_meta = meta

        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._async_run(), name=f"stream_meta_{url}")

        return dict(meta)

    async def async_clear(self) -> None:
        self._stream_url = ""
        self._current_meta = {}
        await self._async_stop_task()

    async def _async_stop_task(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _async_run(self) -> None:
        while self._stream_url:
            await asyncio.sleep(_TRACKER_INTERVAL_SECONDS)
            url = self._stream_url
            if not url:
                return
            try:
                meta = await self._async_build_metadata(url)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Stream metadata refresh failed for %s: %s", url, err)
                continue
            if url != self._stream_url or meta == self._current_meta:
                continue
            self._current_meta = meta
            await self._update_callback(dict(meta))

    async def _async_build_metadata(self, url: str) -> dict[str, Any]:
        station_meta = await self._station_meta_resolver(url)
        icy_meta = await async_fetch_icy_meta(self._session, url)

        station_name = str(station_meta.get("name") or "").strip()
        station_logo = str(station_meta.get("favicon") or "").strip()
        icy_name = str(icy_meta.get("icy_name") or "").strip()
        stream_title = str(icy_meta.get("stream_title") or "").strip()

        track_artist = ""
        track_title = ""
        decision_reason = "no_stream_title"
        is_branding = True
        if stream_title:
            parsed_artist, parsed_title = parse_icy_stream_title(stream_title)
            is_real_track, decision_reason = _classify_stream_title(
                stream_title, artist=parsed_artist, title=parsed_title,
                station_name=station_name, icy_name=icy_name,
            )
            is_branding = not is_real_track
            if is_real_track:
                track_artist, track_title = parsed_artist, parsed_title

        if is_branding and self._acoustid_resolver is not None:
            loop = asyncio.get_running_loop()
            if loop.time() - self._last_acoustid_attempt >= _ACOUSTID_FALLBACK_COOLDOWN_SECONDS:
                self._last_acoustid_attempt = loop.time()
                try:
                    match = await self._acoustid_resolver(url)
                except Exception as err:
                    _LOGGER.debug("AcoustID fallback failed for %s: %s", url, err)
                    match = {}
                if match:
                    track_artist = str(match.get("artist") or "")
                    track_title = str(match.get("title") or "")
                    is_branding = False
                    decision_reason = "acoustid_fingerprint_match"

        track_image = ""
        if track_title:
            track_image = await async_lookup_track_art(self._session, artist=track_artist, title=track_title)

        return {
            "stream_url": url,
            "station_name": station_name,
            "station_logo": station_logo,
            "icy_name": icy_name,
            "stream_title": stream_title,
            "track_artist": track_artist,
            "track_title": track_title,
            "track_image": track_image,
            "title_classification": "track" if track_title else "branding",
            "title_decision_reason": decision_reason,
            "is_station_branding": is_branding,
        }


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[!\-_.,/()'–—]", " ", value.lower())).strip()


def _looks_like_station_branding(stream_title: str, *, station_name: str, icy_name: str) -> bool:
    normalized_stream = _normalize_text(stream_title)
    if not normalized_stream:
        return True
    for candidate in (station_name, icy_name):
        normalized_candidate = _normalize_text(candidate)
        if not normalized_candidate:
            continue
        if normalized_candidate == normalized_stream:
            return True
        if _branding_remainder_is_generic(normalized_stream, normalized_candidate):
            return True
    return False


def _classify_stream_title(
    stream_title: str, *, artist: str, title: str, station_name: str, icy_name: str
) -> tuple[bool, str]:
    if _looks_like_station_branding(stream_title, station_name=station_name, icy_name=icy_name):
        return False, "matches_station_branding"

    normalized_stream = _normalize_text(stream_title)
    if any(marker in normalized_stream for marker in ("http", "www", ".com", ".net", ".de")):
        return False, "contains_url_or_domain"

    if any(token in normalized_stream for token in ("listen live", "on air", "webradio", "unknown", "advert", "jingle")):
        return False, "contains_generic_broadcast_phrase"

    normalized_artist = _normalize_text(artist)
    normalized_title = _normalize_text(title)
    if not normalized_title:
        return False, "missing_track_title"

    if normalized_artist == normalized_title:
        return False, "artist_equals_title"

    for candidate in (station_name, icy_name):
        normalized_candidate = _normalize_text(candidate)
        if not normalized_candidate:
            continue
        if normalized_title == normalized_candidate:
            return False, "title_equals_station_name"
        if normalized_artist and normalized_artist == normalized_candidate:
            return False, "artist_equals_station_name"
        if _branding_remainder_is_generic(normalized_title, normalized_candidate):
            return False, "title_is_station_branding_variant"

    return True, "artist_title_accepted"


def _branding_remainder_is_generic(value: str, candidate: str) -> bool:
    if not value or not candidate:
        return False
    if candidate not in value:
        return False
    remainder = value.replace(candidate, " ")
    tokens = [token for token in remainder.split() if token]
    if not tokens:
        return True
    return all(token in _GENERIC_BRANDING_TOKENS for token in tokens)
