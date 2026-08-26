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

COPY app /app
COPY rootfs /

RUN pip install --no-cache-dir --break-system-packages \
    aiohttp==3.10.* \
    websockets==13.* \
    zeroconf==0.146.* \
    pyatv==0.18.*

WORKDIR /app

RUN chmod a+x /etc/services.d/bose_router/run
