"""Listens to each Bose device's own real-time notification WebSocket
(port 8080, "gabbo" subprotocol) for physical preset-button presses.

Necessary because pressing a physical preset button makes the device try
to resolve and stream its own natively-stored preset URL directly - which
fails ("Dienst nicht verfügbar" / service unavailable) for presets whose
URL isn't something the speaker can reach on its own (confirmed live: the
private stream*.lan hostnames aren't resolvable by the speaker itself).
Catching the nowSelectionUpdated notification lets the app immediately
override that native attempt with a working AirPlay-based play instead.
Ported from production bose_preset_router's router.py _device_loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable

import websockets

_LOGGER = logging.getLogger(__name__)

_NOTIFY_PORT = 8080
_PRESET_RE = re.compile(r'<preset id="(\d+)">')
_RECONNECT_DELAY_SECONDS = 3


class BoseNotificationListener:
    """Keeps a persistent connection to one device's notification socket."""

    def __init__(self, device_ip: str, on_preset_pressed: Callable[[int], Awaitable[None]]) -> None:
        self._device_ip = device_ip
        self._on_preset_pressed = on_preset_pressed
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def async_start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"bose_notify_{self._device_ip}")

    async def async_stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        url = f"ws://{self._device_ip}:{_NOTIFY_PORT}/"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url, subprotocols=["gabbo"]) as ws:
                    _LOGGER.info("Connected to Bose notification socket for %s", self._device_ip)
                    async for message in ws:
                        if not isinstance(message, str):
                            continue
                        if "nowSelectionUpdated" not in message or "<preset id=" not in message:
                            continue
                        match = _PRESET_RE.search(message)
                        if not match:
                            continue
                        preset_id = int(match.group(1))
                        if not 1 <= preset_id <= 6:
                            continue
                        _LOGGER.info("Physical preset %d pressed on %s", preset_id, self._device_ip)
                        try:
                            await self._on_preset_pressed(preset_id)
                        except Exception as err:
                            _LOGGER.warning(
                                "Failed to handle physical preset press on %s: %s", self._device_ip, err
                            )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.debug("Bose notification socket error for %s: %s", self._device_ip, err)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=_RECONNECT_DELAY_SECONDS)
                except asyncio.TimeoutError:
                    pass
