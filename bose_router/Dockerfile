ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM $BUILD_FROM

# Phase 3 adds pyatv (AirPlay/RAOP). Its dependency chain (cryptography,
# srptools) needs a C/Rust toolchain to build on Alpine (musl) if no
# prebuilt wheel matches — kept as regular (non-virtual) packages for now,
# image-size optimization is not a Phase 3 concern. Later phases add further
# system packages here (e.g. libchromaprint1, ffmpeg) — the whole point of
# moving off the HA custom-component architecture is that this line becomes
# possible at all.
RUN apk add --no-cache \
    python3 py3-pip \
    gcc musl-dev libffi-dev openssl-dev cargo rust

# Phase 5 adds AcoustID/Chromaprint audio fingerprinting — the feature that
# originally motivated moving off the HA custom-component architecture
# (chromaprint is a native library; a custom component has no way to install
# system packages at all). chromaprint-libs provides libchromaprint.so.1,
# which pyacoustid's ctypes binding (chromaprint.py) looks for by name;
# chromaprint also ships the fpcalc CLI as a fallback path pyacoustid uses
# if the ctypes binding isn't available; ffmpeg is audioread's decode
# backend for turning the downloaded stream snippet into raw PCM.
RUN apk add --no-cache \
    chromaprint chromaprint-libs ffmpeg

COPY app /app
COPY rootfs /

RUN pip install --no-cache-dir --break-system-packages \
    aiohttp==3.10.* \
    websockets==13.* \
    zeroconf==0.146.* \
    pyatv==0.18.* \
    pyacoustid==1.3.*

WORKDIR /app

RUN chmod a+x /etc/services.d/bose_router/run
