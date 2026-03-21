ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM ${BUILD_FROM}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk add --no-cache \
        iproute2 \
        lm-sensors \
        nvme-cli \
        pciutils \
        py3-flask \
        py3-pyserial \
        py3-websockets \
        py3-yaml \
        procps \
        smartmontools \
        usbutils

WORKDIR /opt/esp-host-bridge

COPY app/ /opt/esp-host-bridge/app/
COPY start_addon.py /opt/esp-host-bridge/start_addon.py
COPY run.sh /run.sh

RUN chmod +x /run.sh

CMD ["/run.sh"]
