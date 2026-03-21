# ESP Host Bridge Home Assistant App Repository

Standalone Home Assistant app repository for `ESP Host Bridge`.

Home Assistant expects `repository.yaml` at the Git repository root for third-party app repositories. This copy is arranged for that model:

- `repository.yaml`
- `esp_host_bridge/`

---

# ESP Host Bridge Add-on

USB CDC bridge and host telemetry Web UI for ESP display devices.

## Current App

- Version: `2026.03.21.47`
- **Required**: **[System Monitor](https://www.home-assistant.io/integrations/systemmonitor/)** integration (for HA mode telemetry). This is highly recommended for local monitoring of the HA Server.
- Home Assistant app mode replaces Docker and VM views with:
  - `Add-ons`
  - `Integrations`
- Host serial access is enabled for ESP USB CDC devices.

## Features

- **USB Bridge**: Forwards host metrics to ESPHome-based display devices over Serial (USB CDC).
- **Web UI**: Real-time dashboard for monitoring host health, add-ons, and integrations.
- **Pure Home Assistant Proxy**: The bridge acts strictly as a proxy, gathering all metrics safely and securely from Home Assistant entities. It does NOT poll local host hardware directly.
- **Auto-Discovery**: Automatically finds and configures Home Assistant System Monitor entities.

## Prerequisites & Sensors

Because this add-on does not poll local hardware, it relies on Home Assistant to provide the data. 
You must configure the **[System Monitor](https://www.home-assistant.io/integrations/systemmonitor/)** integration in Home Assistant to provide the basic host metrics (CPU, RAM, Disk, Temp, Network, Uptime).

If you want to display **GPU Metrics** or **Fan RPM**, you will need to add those specific sensors to Home Assistant (e.g., via HACS integrations or command-line sensors) and then map them in the Web UI.

## Configuration (Two Places)

There are two places where configuration is managed:

1. **Add-on Configuration Tab (Home Assistant)**
   - Used *only* for basic hardware mapping.
   - **Serial Port Selector:** Pick the USB device where your ESP is connected.
   - **Agent Configuration:** Set Baud Rate, Polling Intervals, and toggle Power Control, addons_polling_enabled, integrations_polling_enabled, activity_polling_enabled

2. **Web UI (Ingress) - Primary Configuration**
   - Click "Open Web UI" to access the main configuration interface.
   - **Agent Configuration:** Set Baud Rate, Polling Intervals, and toggle Power Control.
   - **Home Assistant Proxy:** This is where you map your `sensor.xxx` entities to the bridge. You can use the "Discover Entities" button to automatically fill in standard System Monitor sensors.

## Install from Home Assistant

1. Go to `Settings -> Apps -> App store`.
2. Open the top-right menu.
3. Add `https://github.com/rog713/ESP-Host-Bridge` (or your fork).
4. Install `ESP Host Bridge`.
5. Go to the **Configuration** tab and select your **Serial Port**.
6. Start the add-on and open the **Web UI**.
7. Go to the **Setup** tab in the Web UI to map your sensors.

## Scope

This app is intentionally host-local. It only makes sense when Home Assistant runs on the same machine that owns the serial device and the workloads you want to monitor.
