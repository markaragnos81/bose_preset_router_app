# Bose Preset Router — App

Standalone Home Assistant App (Supervisor Add-on) that controls Bose
SoundTouch speakers, plus a thin HA client integration (`bose_router_client`,
installed via HACS) that surfaces them as `media_player` entities. Fully
replaces the earlier [bose_preset_router](https://github.com/markaragnos81/bose_preset_router)
custom component, which this project has superseded and retired.

## Why a separate App instead of a custom component

A Home Assistant custom component runs inside HA Core's own Python process
and event loop. That has real limits this project ran into directly:

- **No system-package access.** Audio-fingerprinting (AcoustID/Chromaprint)
  needs the native `libchromaprint` library — impossible to install from
  inside a custom component, only possible because this App runs in its own
  Docker container.
- **Shared event loop with HA.** AirPlay streaming (`pyatv`) makes blocking
  calls internally; a custom component needs thread-isolation workarounds to
  avoid stalling HA's own event loop. This App's own dedicated event loop
  needs none of that.
- **Every code change needs a full HA restart** to test, vs. just restarting
  the App.

Follows the same pattern Music Assistant itself uses: a standalone server
(own container, own OS control) + a thin HA integration that talks to it
over WebSocket, discovering it via Zeroconf/mDNS.

## What it does

- Full Bose SoundTouch control: presets, volume, mute, play/pause/stop,
  next/previous track, power on/standby, source selection.
- Preset playback via AirPlay (`pyatv`/RAOP) rather than native UPnP —
  more reliable on this hardware (see below).
- **Physical preset button presses work too** — the App listens to each
  speaker's own real-time notification socket and takes over as soon as a
  button is pressed, before the speaker's own (unreliable) native playback
  attempt can fail.
- Real track/artist/album-art metadata via ICY stream title parsing
  (listens for several seconds per poll, not just a single snapshot — a
  one-shot read frequently lands on a jingle/ident instead of the song),
  Radio Browser and radio.net station lookups, and iTunes cover art.
- AcoustID/Chromaprint audio fingerprinting as a last-resort fallback for
  stations that never send real song titles at all.
- Preset maintenance from the App's own Configuration tab: global default
  presets applied to every speaker, with optional per-speaker overrides.
  Station names auto-resolve from the stream if left blank.
- Multi-room zone commands are implemented but unverified on this
  hardware — `/zone` returns 404 on every SoundTouch unit tested so far,
  likely a firmware limitation rather than something the App can fix.
- AirPlay playback resumes automatically after an App restart.

## Why AirPlay, not native UPnP, for playback

Two separate reliability issues were found live on this hardware, both
avoided by routing all playback through AirPlay instead:

1. The native `PRESET_n` remote key can "wedge" the UPnP renderer — the
   speaker accepts the command (HTTP 200) but never actually streams
   audio, and only a physical power cycle clears it.
2. Driving playback via pure UPnP AVTransport (`SetAVTransportURI`+`Play`)
   avoids the wedging issue but got stuck in
   `CurrentTransportState=PAUSED_PLAYBACK` on another unit across repeated
   retries.

AirPlay was the one path that played reliably in every live test across
this project, so it's what both preset selection and physical-button
handling use.

## Repository layout

```
bose_router/                          # the App (Supervisor Add-on)
  config.yaml                         # App manifest — devices, presets, log level, etc.
  Dockerfile
  translations/{en,de}.yaml           # human-friendly labels for the Configuration tab
  app/                                # the App's own Python source
custom_components/
  bose_router_client/                 # the thin HA client (install via HACS)
```

## Installing

1. **The App**: Settings → Add-ons → Add-on Store → ⋮ → Repositories → add
   `https://github.com/markaragnos81/bose_preset_router_app`. Install
   "Bose Preset Router", configure your speakers (and, optionally, default
   presets) in its Configuration tab, then start it.
2. **The HA client**: add this same repository URL as a custom repository
   in HACS, install "Bose Preset Router (App Client)". It discovers the
   running App automatically via Zeroconf.

## Local development (App only)

```bash
docker build -t bose-router:dev ./bose_router
docker run --rm --network host \
  -e BOSE_DEVICES="Büro:192.168.20.139,Wohnzimmer:192.168.20.20" \
  -e WS_PORT=8765 \
  bose-router:dev
```
