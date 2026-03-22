# ESP Host Bridge

ESP Host Bridge runs the Host Bridge Web UI inside Home Assistant for the ESP dashboard firmware in this repository.

## What it does

- runs `host_metrics.py webui`
- stores Web UI settings in `/data/config.json`
- syncs add-on Configuration-tab values from `/data/options.json` into `/data/config.json` on startup
- serves the Web UI on port `8654`
- exposes the Web UI through Home Assistant Ingress
- can protect direct Web UI access with a password

## When to use it

Use this add-on when Home Assistant is on the same machine as:

- the ESP USB device
- the host resources you want to monitor

Poor fit:

- Home Assistant is on one machine and the ESP or monitored host is on another

## Permissions

Current add-on permissions:

- `host_network: true`
- `host_uts: true`
- `hassio_api: true`
- `homeassistant_api: true`
- `hassio_role: manager`
- `udev: true`
- `uart: true`
- `full_access: false`
- `apparmor: true`

Why:

- serial access needs host device visibility
- add-ons, integrations, activity, and host power use Supervisor/Core APIs
- local host telemetry still uses host-visible network, procfs, thermal, and disk paths where available

## Install

1. Add this repository to Home Assistant as a custom add-on repository.
2. Install `ESP Host Bridge`.
3. Start the add-on and open the Web UI.
4. Configure the serial port and any telemetry or polling options you want.

## Behavior

### Configuration

The add-on keeps two config entry points aligned:

- Home Assistant add-on options in `/data/options.json`
- Host Bridge runtime config in `/data/config.json`

The launcher syncs add-on options into the runtime config before the Web UI starts.

### Serial

The add-on reads the ESP over a host-visible serial device path, typically under `/dev/serial/by-id/` when available.

### Metrics

The add-on still uses local host telemetry for:

- CPU
- memory
- uptime
- network RX/TX
- disk usage
- disk IO
- temperature and fan sensors when available

### Add-ons

Add-on data comes from the Home Assistant Supervisor API.

### Integrations

Integration data comes from Home Assistant Core entity-registry data over the Core WebSocket API. It is a read-only overview.

### Activity

Recent activity comes from the Home Assistant logbook API.

### Host power

Shutdown and restart use the Supervisor host API:

- `POST /host/shutdown`
- `POST /host/reboot`

### Direct Web UI protection

Home Assistant Ingress is trusted and uses Home Assistant auth.

Direct access to the add-on Web UI can be protected with a password from the Web UI setup page. The password is stored as a hash in `/data/config.json`.

## Persistence

- runtime settings: `/data/config.json`
- add-on options: `/data/options.json`
