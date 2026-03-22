# ESP Host Bridge Add-on Repository

This repository is a standalone Home Assistant add-on repository for `ESP Host Bridge`.

Home Assistant expects third-party add-on repositories to expose:

- `repository.yaml`
- `esp_host_bridge/`

This repository is arranged for that layout.

## Current Add-on

- Version: `2026.03.22.5`
- Ingress-enabled Web UI
- Home Assistant configuration schema in the add-on Configuration tab
- USB CDC bridge for ESP devices
- Home Assistant mode workload pages:
  - `Add-ons`
  - `Integrations`
  - `Recent Activity`
- Direct Web UI password protection is available from the Web UI itself

## Install

1. Add `https://github.com/rog713/ESP-Host-Bridge-Addon` as a custom Home Assistant add-on repository.
2. Install `ESP Host Bridge`.
3. Start the add-on.
4. Open the Web UI through Home Assistant Ingress or the direct add-on URL.
5. Configure the serial port and any telemetry options you want.

## Scope

This add-on is meant for setups where Home Assistant runs on the same machine that owns:

- the ESP USB device
- the local host resources you want to monitor

If Home Assistant is running on a different machine than the ESP or the monitored host, this add-on is the wrong fit.

## Notes

- Add-on data comes from the Home Assistant Supervisor API.
- Integration data comes from the Home Assistant Core entity registry over WebSocket.
- Recent activity comes from the Home Assistant logbook API.
- The add-on still uses local host telemetry for metrics like CPU, memory, network, and disk.
