# Bose Preset Router — App

Standalone Home Assistant App (Supervisor Add-on) companion to
[bose_preset_router](https://github.com/markaragnos81/bose_preset_router).

## Why this exists

`bose_preset_router` runs as a Home Assistant custom component — inside HA
Core's own Python process and event loop. That has real limits:

- **No system-package access.** [Music Assistant](https://github.com/music-assistant/server)
  identifies songs on radio stations that don't send real ICY metadata (e.g.
  Radio21, RadioBob) via its `acoustid_lookup` provider — AcoustID/Chromaprint
  audio fingerprinting. That needs the native `libchromaprint1` library,
  which MA installs via `apt-get` in its own Docker image. A custom
  component has no way to install system packages, only pure-Python `pip`
  requirements — AcoustID is structurally impossible there.
- **Shared event loop with HA.** AirPlay streaming (`pyatv`) makes blocking
  calls internally, requiring workarounds (a dedicated thread per stream)
  to avoid stalling HA's own event loop.
- **Every code change needs a full HA restart** to test.

This app follows the same pattern Music Assistant itself uses: a standalone
server (own container, own OS control) + a thin HA integration that talks to
it over WebSocket, discovering it via Zeroconf/mDNS
(`homeassistant/components/music_assistant/config_flow.py` was used as the
reference for the discovery pattern).

## Status: Phase 1 (proof of concept)

Only proves the plumbing — Zeroconf advertisement + WebSocket `now_playing`
query for a single, hardcoded Bose device. No AirPlay, no presets, no
AcoustID yet. See the project plan for the full phased roadmap.

`bose_preset_router` (the existing custom component) is unaffected and
remains the production integration until this app reaches feature parity
and is proven in live use.

## Local development

```bash
docker build -t bose-router:dev ./bose_router
docker run --rm --network host \
  -e BOSE_IP=192.168.20.139 -e WS_PORT=8765 \
  bose-router:dev
```

## Installing as a Home Assistant App

Settings → Add-ons → Add-on Store → ⋮ → Repositories → add this repo's URL.
