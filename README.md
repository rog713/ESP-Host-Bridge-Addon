# ESP Host Bridge Home Assistant App Repository

Standalone Home Assistant app repository for `ESP Host Bridge`.

Home Assistant expects `repository.yaml` at the Git repository root for third-party app repositories. This copy is arranged for that model:

- `repository.yaml`
- `esp_host_bridge/`

## Current App

- Version: `2026.03.21.32`
- **Required**: **System Monitor** integration (for HA mode telemetry).
- Home Assistant app mode replaces Docker and VM views with:
  - `Add-ons`
  - `Integrations`
- Host serial access is enabled for ESP USB CDC devices.

## Install from Home Assistant

1. Go to `Settings -> Apps -> App store`.
2. Open the top-right menu.
3. Add `https://github.com/rog713/ESP-Host-Bridge`.
4. Install `ESP Host Bridge`.

## Scope

This app is intentionally host-local. It only makes sense when Home Assistant runs on the same machine that owns the serial device and the workloads you want to monitor.
