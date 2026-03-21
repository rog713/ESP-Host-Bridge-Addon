# ESP Host Bridge

ESP Host Bridge runs the Host Bridge Web UI inside Home Assistant for the ESP dashboard firmware in this repository.

## What it does

- runs `host_metrics.py webui`
- stores Web UI config in `/data/config.json`
- serves the Web UI on port `8654`

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
5. Configure the serial port, network interface, and any add-on, integration, or activity polling you want.

## Behavior

### Serial

The add-on reads the ESP over `/dev` on the Home Assistant host. The exact device path depends on your system and must be selected in the Add-on Configuration tab.

### Pure Home Assistant Proxy

The bridge acts strictly as a **Proxy**. It does NOT poll your host hardware directly (no `psutil`, no direct `/proc` reads). This makes it highly secure and compatible with restricted environments.

- **Metrics**: Relies exclusively on Home Assistant entities.
- **Sensors Required**: You must install the **System Monitor** integration in Home Assistant to provide the basic metrics (CPU, Memory, Disk, Network, Temp).
- **Custom Sensors**: If you want GPU metrics or Fan RPM, you must create or install custom sensors in Home Assistant and map their Entity IDs to the bridge.

### Configuration (The Two Places)

Because the add-on acts as a bridge, its configuration is split into two logical areas:

1. **Add-on Configuration Tab (Home Assistant UI)**
   - Used *only* for the hardware USB mapping.
   - Set the **Serial Port** here so the Docker container can access the ESP device.

2. **Web UI (Ingress)**
   - Used for all software and metric mapping.
   - Open the Web UI and go to the **Setup** tab.
   - **Auto-Discovery**: Click the "Discover Entities" button to automatically map your System Monitor sensors.
   - Manually enter any custom `sensor.xxx` entities (like `sensor.gpu_temperature`).
   - Configure polling intervals, baud rate, and power control settings.

### Add-ons

Add-on status data is pulled directly from the Supervisor API.

### Integrations

Integration data comes from Home Assistant Core entity-registry data over the Core WebSocket API. It provides a read-only overview.

### Activity

Recent system activity comes from the Home Assistant logbook API.

### Host power

Shutdown and restart use the Supervisor host API:

- `POST /host/shutdown`
- `POST /host/reboot`

## Persistence

Host Bridge settings configured in the Web UI are stored persistently in `/data/config.json`.
