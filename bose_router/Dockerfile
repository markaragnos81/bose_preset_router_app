ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM $BUILD_FROM

# Phase 1: pure-Python deps only. Later phases add system packages here
# (e.g. libchromaprint1, ffmpeg) — the whole point of moving off the HA
# custom-component architecture is that this line becomes possible at all.
RUN apk add --no-cache python3 py3-pip

COPY app /app
COPY rootfs /

RUN pip install --no-cache-dir --break-system-packages \
    aiohttp==3.10.* \
    websockets==13.* \
    zeroconf==0.146.*

WORKDIR /app

RUN chmod a+x /etc/services.d/bose_router/run
