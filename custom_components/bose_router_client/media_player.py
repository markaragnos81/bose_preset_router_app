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

    entities = {
        ip: BoseRouterMediaPlayer(coordinator, client=client)
        for ip, coordinator in coordinators.items()
    }
    # Each entity needs to resolve OTHER devices' zone membership (Bose deviceID
    # -> IP -> entity_id) for group_members/join/unjoin — give them all a
    # reference to their siblings rather than routing that through hass.data
    # lookups on every call.
    for entity in entities.values():
        entity.set_siblings(entities)

    async_add_entities(entities.values())


class BoseRouterMediaPlayer(CoordinatorEntity[BoseRouterDeviceCoordinator], MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.GROUPING
    )

    def __init__(self, coordinator: BoseRouterDeviceCoordinator, *, client: BoseRouterAppClient) -> None:
        super().__init__(coordinator)
        self._client = client
        self._siblings: dict[str, BoseRouterMediaPlayer] = {}
        info = (coordinator.data or {}).get("info", {})
        self._attr_unique_id = f"bose_router_client_{coordinator.device_ip}"
        self._attr_name = info.get("name") or coordinator.device_ip

    def set_siblings(self, siblings: dict[str, BoseRouterMediaPlayer]) -> None:
        self._siblings = siblings

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def _own_device_id(self) -> str:
        return str(self._data.get("info", {}).get("device_id") or "")

    def _ip_for_device_id(self, device_id: str) -> str | None:
        for ip, sibling in self._siblings.items():
            if sibling._own_device_id == device_id:  # noqa: SLF001
                return ip
        return None

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

    @property
    def group_members(self) -> list[str] | None:
        zone = self._data.get("zone", {})
        master_id = str(zone.get("master_id") or "")
        member_ips = [m.get("ip_address") for m in zone.get("members") or [] if m.get("ip_address")]
        if not master_id and not member_ips:
            return None

        ips: list[str] = []
        master_ip = self._ip_for_device_id(master_id)
        if master_ip:
            ips.append(master_ip)
        ips.extend(ip for ip in member_ips if ip != master_ip)

        entity_ids = [
            self._siblings[ip].entity_id
            for ip in ips
            if ip in self._siblings and self._siblings[ip].entity_id
        ]
        return entity_ids or None

    async def async_join_players(self, group_members: list[str]) -> None:
        """Make this player the zone master and add the given players as slaves."""
        member_ips = [
            ip
            for entity_id in group_members
            for ip, sibling in self._siblings.items()
            if sibling.entity_id == entity_id
        ]
        if not member_ips:
            return
        await self._client.async_send(
            "add_zone_slaves",
            device=self.coordinator.device_ip,
            args={"master_ip": self.coordinator.device_ip, "member_ips": member_ips},
        )
        await self.coordinator.async_request_refresh()

    async def async_unjoin_player(self) -> None:
        """Remove this player from its current zone."""
        zone = self._data.get("zone", {})
        master_id = str(zone.get("master_id") or "")
        if not master_id or master_id == self._own_device_id:
            return  # standalone, or we're the master — unjoin only makes sense for a slave
        master_ip = self._ip_for_device_id(master_id)
        if not master_ip:
            return
        await self._client.async_send(
            "remove_zone_slaves",
            device=master_ip,
            args={"master_ip": master_ip, "member_ips": [self.coordinator.device_ip]},
        )
        await self.coordinator.async_request_refresh()
