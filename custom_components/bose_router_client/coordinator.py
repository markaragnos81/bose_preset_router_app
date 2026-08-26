from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_POLL_INTERVAL_SECONDS
from .ws_client import BoseRouterAppClient, BoseRouterAppError

_LOGGER = logging.getLogger(__name__)


class BoseRouterDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls one Bose device's full snapshot through the App over WebSocket."""

    def __init__(self, hass: HomeAssistant, *, client: BoseRouterAppClient, device_ip: str) -> None:
        self.client = client
        self.device_ip = device_ip
        super().__init__(
            hass,
            _LOGGER,
            name=f"bose_router_client_{device_ip}",
            update_interval=timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.async_send("snapshot", device=self.device_ip)
        except BoseRouterAppError as err:
            raise UpdateFailed(f"App reported an error for {self.device_ip}: {err}") from err
        except OSError as err:
            raise UpdateFailed(f"Could not reach App for {self.device_ip}: {err}") from err
