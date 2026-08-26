"""Radio Browser API lookup and ICY stream metadata for internet radio
stations. Ported from bose_preset_router's radio_browser.py, with
`hass: HomeAssistant` / `async_get_clientsession(hass)` replaced by a plain
`aiohttp.ClientSession` this app owns and passes in directly.
"""
from __future__ import annotations

import logging
import re
import struct
from urllib.parse import urlencode, urlsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

_RADIO_BROWSER_HOSTS = [
    "de1.api.radio-browser.info",
    "at1.api.radio-browser.info",
    "nl1.api.radio-browser.info",
]

# In-memory cache: url -> {"name": str, "favicon": str}
_station_cache: dict[str, dict[str, str]] = {}
_track_art_cache: dict[tuple[str, str], str] = {}


# ---------------------------------------------------------------------------
# Radio Browser station lookup
# ---------------------------------------------------------------------------

async def async_lookup_station(session: aiohttp.ClientSession, url: str) -> dict[str, str]:
    """Return {"name": str, "favicon": str} for a stream URL, or {} if not found.

    Tries the exact URL then the http/https variant across multiple mirrors.
    Augments with a favicon fallback when Radio Browser returns none.
    Results are cached in-process to avoid repeated API calls.
    """
    url = url.strip()
    if not url:
        return {}

    if url in _station_cache:
        return _station_cache[url]

    candidates = [url]
    if url.startswith("http://"):
        candidates.append("https://" + url[7:])
    elif url.startswith("https://"):
        candidates.append("http://" + url[8:])

    result: dict[str, str] = {}
    for candidate in candidates:
        result = await _query_by_url(session, candidate)
        if result:
            break

    if not result.get("favicon"):
        fav = _best_favicon(url)
        if result:
            result["favicon"] = fav
        else:
            result = {"name": "", "favicon": fav}

    _station_cache[url] = result
    return result


async def _query_by_url(session: aiohttp.ClientSession, url: str) -> dict[str, str]:
    body = urlencode({"url": url}).encode()
    for host in _RADIO_BROWSER_HOSTS:
        try:
            async with session.post(
                f"https://{host}/json/stations/byurl",
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "bose_router_app/homeassistant",
                },
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                if resp.status != 200:
                    continue
                stations = await resp.json()
                if stations:
                    s = stations[0]
                    return {
                        "name": str(s.get("name") or "").strip(),
                        "favicon": str(s.get("favicon") or "").strip(),
                    }
                return {}
        except Exception as err:
            _LOGGER.debug("Radio Browser %s failed for %s: %s", host, url, err)
            continue
    return {}


def _best_favicon(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
        if host:
            return f"https://www.google.com/s2/favicons?domain={host}&sz=128"
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# radio.net (radio-api.net) high-resolution station logo lookup
# ---------------------------------------------------------------------------

_RADIO_API_SEARCH = "https://prod.radio-api.net/stations/search"


def _stream_key(url: str) -> tuple[str, str]:
    try:
        p = urlsplit(url)
        segs = [s for s in p.path.split("/") if s]
        return (p.hostname or "").lower(), (segs[0].lower() if segs else "")
    except Exception:
        return "", ""


def _radio_api_queries(name: str) -> list[str]:
    tokens = [t for t in re.sub(r"[!\-–—_.,/()']", " ", name).split() if t]
    cands: list[str] = []
    if name.strip():
        cands.append(name.strip())
    if len(tokens) >= 2:
        cands.append(" ".join(tokens[:2]))
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        cl = c.lower()
        if cl not in seen:
            seen.add(cl)
            out.append(c)
    return out


async def async_lookup_radio_logo(session: aiohttp.ClientSession, name: str, url: str) -> str:
    """Return a high-res station logo from radio.net, matched by stream URL. '' if none.

    Matching is done ONLY by stream URL identity (host + first path segment) —
    name matching is intentionally avoided because radio.net's search ranking
    produces confident-but-wrong matches for generic names.
    """
    host, key = _stream_key(url)
    if not host or not key:
        return ""

    for query in _radio_api_queries(name):
        try:
            async with session.get(
                _RADIO_API_SEARCH,
                params={"count": "60", "query": query},
                headers={"User-Agent": "bose_router_app/homeassistant"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
        except Exception as err:
            _LOGGER.debug("radio.net search failed for %r: %s", query, err)
            continue

        for station in data.get("playables", []):
            logo = str(station.get("logo300x300") or "").strip()
            if not logo:
                continue
            for stream in station.get("streams", []):
                if _stream_key(str(stream.get("url") or "")) == (host, key):
                    return logo
    return ""


# ---------------------------------------------------------------------------
# ICY stream metadata (current track / artist)
# ---------------------------------------------------------------------------

async def async_fetch_icy_meta(session: aiohttp.ClientSession, url: str) -> dict[str, str]:
    """Fetch live ICY metadata from an internet radio stream.

    Returns {"stream_title": str, "icy_name": str}. Returns {} on any error.
    """
    url = url.strip()
    if not url:
        return {}

    try:
        async with session.get(
            url,
            headers={"Icy-MetaData": "1", "User-Agent": "bose_router_app/homeassistant"},
            timeout=aiohttp.ClientTimeout(total=5, connect=3),
        ) as resp:
            if resp.status not in (200, 206):
                return {}

            icy_name = resp.headers.get("icy-name", "").strip()
            metaint_str = resp.headers.get("icy-metaint", "0")
            metaint = int(metaint_str) if metaint_str.isdigit() else 0

            if not metaint:
                return {"stream_title": "", "icy_name": icy_name}

            await resp.content.readexactly(metaint)
            length_byte = await resp.content.readexactly(1)
            meta_len = struct.unpack("B", length_byte)[0] * 16

            stream_title = ""
            if meta_len:
                meta_bytes = await resp.content.readexactly(meta_len)
                try:
                    raw = meta_bytes.decode("utf-8").rstrip("\x00")
                except UnicodeDecodeError:
                    raw = meta_bytes.decode("latin-1").rstrip("\x00")
                m = re.search(r"StreamTitle='([^']*)'", raw)
                if m:
                    stream_title = m.group(1).strip()

            return {"stream_title": stream_title, "icy_name": icy_name}

    except Exception as err:
        _LOGGER.debug("ICY fetch failed for %s: %s", url, err)
        return {}


def parse_icy_stream_title(stream_title: str) -> tuple[str, str]:
    """Parse 'Artist - Title' into (artist, title). Returns ('', stream_title) if no separator."""
    if " - " in stream_title:
        artist, _, title = stream_title.partition(" - ")
        return artist.strip(), title.strip()
    return "", stream_title


async def async_lookup_track_art(session: aiohttp.ClientSession, *, artist: str, title: str) -> str:
    """Best-effort cover art lookup for a parsed Artist/Title pair."""
    artist = str(artist or "").strip()
    title = str(title or "").strip()
    if not title:
        return ""

    cache_key = (artist.casefold(), title.casefold())
    if cache_key in _track_art_cache:
        return _track_art_cache[cache_key]

    query = f"{artist} {title}".strip()
    artwork = ""
    try:
        async with session.get(
            "https://itunes.apple.com/search",
            params={"term": query, "entity": "song", "limit": "5"},
            headers={"User-Agent": "bose_router_app/homeassistant"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                # iTunes sends "Content-Type: text/javascript", not
                # "application/json" — aiohttp's default strict content-type
                # check rejects that and raises unless disabled here
                # (confirmed live in bose_preset_router v0.7.21).
                data = await resp.json(content_type=None)
                artwork = _pick_itunes_artwork(data.get("results", []), artist=artist, title=title)
    except Exception as err:
        _LOGGER.debug("Track art lookup failed for %r / %r: %s", artist, title, err)

    _track_art_cache[cache_key] = artwork
    return artwork


def _pick_itunes_artwork(results: list[dict[str, object]], *, artist: str, title: str) -> str:
    expected_artist = _normalize_track_value(artist)
    expected_title = _normalize_track_value(title)

    for result in results:
        candidate_title = _normalize_track_value(str(result.get("trackName") or ""))
        candidate_artist = _normalize_track_value(str(result.get("artistName") or ""))
        if candidate_title != expected_title:
            continue
        if expected_artist and candidate_artist and candidate_artist != expected_artist:
            continue
        artwork = str(
            result.get("artworkUrl512") or result.get("artworkUrl100") or result.get("artworkUrl60") or ""
        ).strip()
        if artwork:
            return artwork.replace("100x100bb", "512x512bb").replace("60x60bb", "512x512bb")

    return ""


def _normalize_track_value(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", value.lower())).strip()
