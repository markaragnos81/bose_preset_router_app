"""AcoustID/Chromaprint audio fingerprinting — identifies a track from the
raw audio of a live stream when the stream sends no usable ICY song data
(e.g. RadioBob/Radio21, which only ever send station branding). This is the
feature that originally motivated moving off the HA custom-component
architecture: it needs the native libchromaprint library, which a custom
component cannot install (see Dockerfile).

Mirrors Music Assistant's own AcoustidLookupProvider approach (same
DEFAULT_MIN_SCORE, same ~2s cadence per lookup call, same acoustid.org
lookup endpoint) but scoped down for on-demand use here rather than an
automatic per-track background job — fingerprinting + lookup is too
expensive/rate-limited to run on every 15s metadata refresh, so it's wired
in server.py as an explicit "identify_track" command instead.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import aiohttp

try:
    import acoustid

    HAVE_ACOUSTID = True
except ImportError:
    HAVE_ACOUSTID = False

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_SCORE = 0.85
CAPTURE_SECONDS = 15
_CAPTURE_TIMEOUT_SECONDS = 20


def chromaprint_available() -> dict[str, object]:
    """Diagnostic: report whether the native fingerprinting backend loaded."""
    if not HAVE_ACOUSTID:
        return {"available": False, "reason": "pyacoustid not importable"}
    return {
        "available": True,
        "have_chromaprint": bool(getattr(acoustid, "have_chromaprint", False)),
        "have_audioread": bool(getattr(acoustid, "have_audioread", False)),
    }


async def _async_capture_stream(session: aiohttp.ClientSession, url: str, dest: Path) -> None:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=_CAPTURE_TIMEOUT_SECONDS)) as resp:
        resp.raise_for_status()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CAPTURE_SECONDS
        with dest.open("wb") as f:
            async for chunk in resp.content.iter_chunked(8192):
                f.write(chunk)
                if loop.time() >= deadline:
                    break


def _fingerprint_and_lookup(path: str, api_key: str) -> dict:
    duration, fingerprint = acoustid.fingerprint_file(path, maxlength=CAPTURE_SECONDS)
    response = acoustid.lookup(api_key, fingerprint, duration)
    best: dict | None = None
    for score, recording_id, title, artist in acoustid.parse_lookup_result(response):
        if best is None or score > best["score"]:
            best = {"score": score, "recording_id": recording_id, "title": title, "artist": artist}
    return best or {}


async def async_identify_track(
    session: aiohttp.ClientSession, stream_url: str, api_key: str, *, min_score: float = DEFAULT_MIN_SCORE
) -> dict[str, object]:
    """Capture a short snippet of `stream_url`, fingerprint it, and look it
    up against AcoustID. Returns {} if nothing matched with enough
    confidence, or {"title", "artist", "score", "recording_id"}.
    """
    if not HAVE_ACOUSTID:
        raise RuntimeError("pyacoustid not installed")
    if not api_key:
        raise RuntimeError("no AcoustID API key configured")

    with tempfile.TemporaryDirectory(prefix="acoustid_") as tmp_dir:
        tmp_path = Path(tmp_dir) / "capture.audio"
        await _async_capture_stream(session, stream_url, tmp_path)

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, _fingerprint_and_lookup, str(tmp_path), api_key)
        except acoustid.AcoustidError as err:
            _LOGGER.warning("AcoustID lookup failed for %s: %s", stream_url, err)
            return {}

    if not result or result.get("title") is None or result["score"] < min_score:
        return {}
    return {
        "title": result["title"],
        "artist": result.get("artist") or "",
        "score": result["score"],
        "recording_id": result["recording_id"],
    }
