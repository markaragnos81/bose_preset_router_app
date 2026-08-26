"""Minimal media_player entity proving the full round trip: HA entity ->
App (WebSocket) -> real Bose hardware, for both reads (coordinator-polled
snapshot) and writes (volume control). Feature-minimal by design — this is
Phase 2's proof that control works end-to-end, not a production-parity
entity (that comes once bose_preset_router's full feature set is ported).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_COORDINATORS, DATA_WS_CLIENT, DOMAIN
from .coordinator import BoseRouterDeviceCoordinator
from .ws_client import BoseRouterAppClient


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client: BoseRouterAppClient = entry_data[DATA_WS_CLIENT]
    coordinators: dict[str, BoseRouterDeviceCoordinator] = entry_data[DATA_COORDINATORS]

    async_add_entities(
        BoseRouterMediaPlayer(coordinator, client=client)
        for coordinator in coordinators.values()
    )


class BoseRouterMediaPlayer(CoordinatorEntity[BoseRouterDeviceCoordinator], MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
    )

    def __init__(self, coordinator: BoseRouterDeviceCoordinator, *, client: BoseRouterAppClient) -> None:
        super().__init__(coordinator)
        self._client = client
        info = (coordinator.data or {}).get("info", {})
        self._attr_unique_id = f"bose_router_client_{coordinator.device_ip}"
        self._attr_name = info.get("name") or coordinator.device_ip

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def state(self) -> MediaPlayerState | str | None:
        now_playing = self._data.get("now_playing", {})
        source = str(now_playing.get("source", "")).upper()
        play_status = str(now_playing.get("play_status", "")).upper()
        if source in {"STANDBY", ""}:
            return MediaPlayerState.OFF
        if play_status == "PLAY_STATE":
            return MediaPlayerState.PLAYING
        if play_status == "PAUSE_STATE":
            return MediaPlayerState.PAUSED
        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        actual = self._data.get("volume", {}).get("actual")
        return None if actual is None else max(0.0, min(1.0, float(actual) / 100))

    @property
    def media_title(self) -> str | None:
        now_playing = self._data.get("now_playing", {})
        return now_playing.get("station_name") or now_playing.get("item_name") or None

    async def async_set_volume_level(self, volume: float) -> None:
        await self._client.async_send(
            "set_volume",
            device=self.coordinator.device_ip,
            args={"level": round(volume * 100)},
        )
        await self.coordinator.async_request_refresh()

    async def async_media_play(self) -> None:
        await self._client.async_send("play", device=self.coordinator.device_ip)
        await self.coordinator.async_request_refresh()

    async def async_media_pause(self) -> None:
        await self._client.async_send("pause", device=self.coordinator.device_ip)
        await self.coordinator.async_request_refresh()

    async def async_media_stop(self) -> None:
        await self._client.async_send("stop", device=self.coordinator.device_ip)
        await self.coordinator.async_request_refresh()
