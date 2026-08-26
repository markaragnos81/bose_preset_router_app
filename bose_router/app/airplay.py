"""AirPlay (RAOP) playback via pyatv — ported from bose_preset_router's
airplay.py, with the thread-isolation hack removed.

That hack existed only because pyatv's stream_file() makes a blocking
time.sleep() call internally, which used to run on Home Assistant's own
shared event loop — stalling HA itself. This App has its own dedicated
process and event loop that nothing else competes with, so the blocking
call only ever stalls this one stream's own task, exactly like it would in
any other single-purpose asyncio service. This is the concrete payoff the
whole App rewrite was started for (see README "Why this exists").

AirPlayDiscovery similarly drops the "reuse Home Assistant's shared
zeroconf instance" dance — the App owns its own zeroconf instance outright,
so it can just keep a persistent AsyncServiceBrowser against it directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable

import pyatv
from pyatv.const import Protocol
from pyatv.interface import AppleTV, BaseConfig, MediaMetadata
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

_LOGGER = logging.getLogger(__name__)

# See postlund/pyatv#2889 (filed from the bose_preset_router project): pyatv's
# HTTP/Icecast RAOP source hardcodes a 64KB read-ahead buffer (~2.6s at
# 192kbps), not exposed via any public stream_file() parameter. Widened here
# at import time — pyatv reads these as module-level globals at call time, so
# patching before any stream starts is effective for every future stream.
_RAOP_BUFFER_MULTIPLIER = 4


def _patch_raop_buffer_size() -> None:
    try:
        from pyatv.protocols.raop import audio_source as _raop_audio_source

        original_buffer = _raop_audio_source.BUFFER_SIZE
        original_headroom = _raop_audio_source.HEADROOM_SIZE
        _raop_audio_source.BUFFER_SIZE = original_buffer * _RAOP_BUFFER_MULTIPLIER
        _raop_audio_source.HEADROOM_SIZE = original_headroom * _RAOP_BUFFER_MULTIPLIER
        _LOGGER.info(
            "AirPlay: widened pyatv RAOP read-ahead buffer %d KB -> %d KB",
            original_buffer // 1024, _raop_audio_source.BUFFER_SIZE // 1024,
        )
    except Exception as err:
        _LOGGER.debug("AirPlay: could not widen pyatv RAOP buffer (skipping): %s", err)


_patch_raop_buffer_size()

DEFAULT_SCAN_INTERVAL_SECONDS = 20.0
DEFAULT_CACHE_MAX_AGE_SECONDS = 45.0
DEFAULT_SCAN_TIMEOUT_SECONDS = 6
FALLBACK_RESCAN_ATTEMPTS = 3
FALLBACK_RESCAN_DELAY_SECONDS = 2.0
RAOP_SERVICE_TYPE = "_raop._tcp.local."
RESUME_STORAGE_PATH = Path("/data/airplay_resume.json")


def _noop_service_state_change(zeroconf, service_type, name, state_change) -> None:
    """No-op handler for AsyncServiceBrowser — its only job is to keep the
    zeroconf cache populated for pyatv.scan(aiozc=...) to read from.
    AsyncServiceBrowser requires at least one handler to construct.
    """


class AirPlayDiscovery:
    """Shared, periodic RAOP discovery cache for the whole App process."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[BaseConfig, float]] = {}
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._aiozc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None

    async def async_start(self) -> None:
        self._stop_event.clear()
        self._aiozc = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(
            self._aiozc.zeroconf, [RAOP_SERVICE_TYPE], handlers=[_noop_service_state_change]
        )
        self._task = asyncio.create_task(self._scan_loop(), name="airplay_discovery")

    async def async_stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None
        if self._aiozc:
            await self._aiozc.async_close()
            self._aiozc = None

    async def _scan_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._async_scan_once()
            except Exception as err:
                _LOGGER.warning("AirPlay discovery scan failed: %s", err, exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=DEFAULT_SCAN_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _async_scan_once(self) -> None:
        loop = asyncio.get_running_loop()
        start = time.monotonic()
        results = await pyatv.scan(
            loop, timeout=DEFAULT_SCAN_TIMEOUT_SECONDS, protocol={Protocol.RAOP}, aiozc=self._aiozc
        )
        elapsed = time.monotonic() - start
        _LOGGER.info(
            "AirPlay scan finished in %.2fs, found %d device(s): %s",
            elapsed, len(results), [str(c.address) for c in results],
        )
        now = time.monotonic()
        for cfg in results:
            self._cache[str(cfg.address)] = (cfg, now)

    async def async_get_config(
        self, bose_ip: str, *, max_age_seconds: float = DEFAULT_CACHE_MAX_AGE_SECONDS
    ) -> BaseConfig | None:
        cached = self._cache.get(bose_ip)
        if cached and (time.monotonic() - cached[1]) < max_age_seconds:
            return cached[0]

        for attempt in range(1, FALLBACK_RESCAN_ATTEMPTS + 1):
            try:
                await self._async_scan_once()
            except Exception as err:
                _LOGGER.warning(
                    "AirPlay discovery fallback scan failed for %s (attempt %d/%d): %s",
                    bose_ip, attempt, FALLBACK_RESCAN_ATTEMPTS, err, exc_info=True,
                )
            cached = self._cache.get(bose_ip)
            if cached:
                return cached[0]
            if attempt < FALLBACK_RESCAN_ATTEMPTS:
                await asyncio.sleep(FALLBACK_RESCAN_DELAY_SECONDS)
        return None


class AirPlayPlayer:
    """Owns the AirPlay/RAOP connection + streaming task for one Bose device.

    Runs directly on the App's own event loop — no thread isolation needed
    (see module docstring). pyatv's stream_file() blocking sleep only stalls
    this one stream's task, which is fine: nothing else on this App's loop
    needs sub-second responsiveness the way Home Assistant's shared loop did.
    """

    def __init__(self, bose_ip: str, discovery: AirPlayDiscovery) -> None:
        self.bose_ip = bose_ip
        self._discovery = discovery
        self._on_ended: Callable[[], None] | None = None
        self._atv: AppleTV | None = None
        self._stream_task: asyncio.Task | None = None

    @property
    def is_playing(self) -> bool:
        return self._stream_task is not None and not self._stream_task.done()

    def set_on_ended(self, callback: Callable[[], None] | None) -> None:
        self._on_ended = callback

    async def play(
        self, url: str, *, title: str = "", artist: str = "", album: str = "", volume_percent: float | None = None
    ) -> bool:
        await self.stop()

        config = await self._discovery.async_get_config(self.bose_ip)
        if config is None:
            _LOGGER.warning("AirPlay: no discovered RAOP target for %s", self.bose_ip)
            return False

        try:
            self._atv = await pyatv.connect(config, asyncio.get_running_loop())
        except Exception as err:
            _LOGGER.warning("AirPlay: connect failed for %s: %s", self.bose_ip, err)
            self._atv = None
            return False

        if volume_percent is not None:
            try:
                await self._atv.audio.set_volume(volume_percent)
            except Exception as err:
                _LOGGER.debug("AirPlay: could not pre-set volume for %s: %s", self.bose_ip, err)

        metadata = MediaMetadata(title=title or "Stream", artist=artist, album=album or "AirPlay")

        async def _run_stream() -> None:
            try:
                await self._atv.stream.stream_file(url, metadata=metadata)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("AirPlay: stream_file error for %s: %s", self.bose_ip, err)

        def _on_task_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            if self._on_ended is not None:
                self._on_ended()

        self._stream_task = asyncio.create_task(_run_stream(), name=f"airplay_stream_{self.bose_ip}")
        self._stream_task.add_done_callback(_on_task_done)
        return True

    async def stop(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            await asyncio.gather(self._stream_task, return_exceptions=True)
            self._stream_task = None
        if self._atv is not None:
            try:
                self._atv.close()
            except Exception:
                pass
            self._atv = None


class AirPlayResumeStore:
    """Persists which preset was last playing via AirPlay per device, across
    App restarts — a plain JSON file under /data (Supervisor's persistent
    volume for this App), replacing bose_preset_router's use of Home
    Assistant's helpers.storage.Store.
    """

    def __init__(self, path: Path = RESUME_STORAGE_PATH) -> None:
        self._path = path
        self._data: dict[str, int] = {}

    async def async_load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except Exception as err:
                _LOGGER.warning("Could not load AirPlay resume state: %s", err)
                self._data = {}

    def get(self, bose_ip: str) -> int | None:
        return self._data.get(bose_ip)

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data))

    async def async_set(self, bose_ip: str, preset: int) -> None:
        self._data[bose_ip] = preset
        self._save()

    async def async_clear(self, bose_ip: str) -> None:
        if bose_ip in self._data:
            del self._data[bose_ip]
            self._save()
