"""Entry point. Reads Supervisor App options from /data/options.json (the
standard mechanism per developers.home-assistant.io/docs/apps/configuration),
falling back to environment variables for local (non-Supervisor) testing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import aiohttp

from bose_client import BoseSoundTouchClient
from server import BoseRouterServer
from zeroconf_advertise import ZeroconfAdvertiser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_LOGGER = logging.getLogger(__name__)

APP_VERSION = "0.1.0"
OPTIONS_PATH = Path("/data/options.json")


def _load_options() -> dict:
    if OPTIONS_PATH.exists():
        return json.loads(OPTIONS_PATH.read_text())
    return {
        "bose_ip": os.environ.get("BOSE_IP", ""),
        "ws_port": int(os.environ.get("WS_PORT", "8765")),
    }


async def async_main() -> None:
    options = _load_options()
    bose_ip = str(options.get("bose_ip") or "").strip()
    ws_port = int(options.get("ws_port") or 8765)

    if not bose_ip:
        raise SystemExit("bose_ip option is required for Phase 1 (single device)")

    server_id = f"bose-router-{uuid.uuid5(uuid.NAMESPACE_DNS, bose_ip).hex[:8]}"

    async with aiohttp.ClientSession() as session:
        bose_client = BoseSoundTouchClient(session, host=bose_ip)
        server = BoseRouterServer(bose_client=bose_client)
        advertiser = ZeroconfAdvertiser(server_id=server_id, server_version=APP_VERSION, port=ws_port)

        await advertiser.async_start()
        try:
            await server.serve_forever(port=ws_port)
        finally:
            await advertiser.async_stop()


if __name__ == "__main__":
    asyncio.run(async_main())
