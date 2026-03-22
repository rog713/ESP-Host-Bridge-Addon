# ESP Host Bridge

ESP Host Bridge runs the Host Bridge Web UI inside Home Assistant for the ESP dashboard firmware in this repository.

## What it does

- runs `host_metrics.py webui`
- stores Web UI config in `/data/config.json`
- syncs add-on configuration into `/data/config.json` on startup
- serves the Web UI on port `8654`
- exposes the Web UI through Home Assistant Ingress

## When to use it

Use this add-on only when Home Assistant is on the same machine as the ESP USB device and the host resources you want to monitor.

Poor fit:

- Home Assistant is on one machine and the ESP or monitored host is on another

## Permissions

This add-on intentionally asks for broad host access:

- `full_access: true`
- `host_network: true`
- `host_uts: true`
- `hassio_api: true`
- `homeassistant_api: true`
- `hassio_role: manager`
- `udev: true`
- `uart: true`
- `apparmor: false`

Why:

- serial access needs host device visibility
- add-ons, integrations, activity, and host power use Supervisor/Core APIs
- disk and SMART access need real block-device visibility when available

## Install

1. Copy `host_metrics/homeassistant_addon/esp_host_bridge/` to `/addons/esp_host_bridge/` on the Home Assistant host.
2. Restart Home Assistant or reload the app store.
3. Install `ESP Host Bridge`.
4. Start the add-on and open the Web UI.
5. Configure the serial port and any polling or sensor options you want.

## Behavior

### Configuration

The add-on now has two entry points that stay aligned:

- the Home Assistant add-on Configuration tab writes `/data/options.json`
- the launcher syncs those values into `/data/config.json` before starting the Web UI

You can still adjust settings from the Web UI. The add-on Configuration tab is now a first-class way to set stable defaults.

### Serial

The add-on reads the ESP over `/dev` on the Home Assistant host. The exact device path depends on your system.

### Add-ons

Add-on data comes from the Supervisor API. No host Docker socket is used.

### Integrations

Integration data comes from Home Assistant Core entity-registry data over the Core WebSocket API. It is a read-only overview.

### Activity

Recent activity comes from the Home Assistant logbook API.

### Host power

Shutdown and restart use the Supervisor host API:

- `POST /host/shutdown`
- `POST /host/reboot`

## Persistence

Host Bridge settings are stored in `/data/config.json`.

Home Assistant add-on options are stored in `/data/options.json` and merged into `/data/config.json` during startup.
