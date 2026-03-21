# ESP Host Bridge Add-on

USB CDC bridge and host telemetry Web UI for ESP display devices.

## Features

- **USB Bridge**: Forwards host metrics to ESPHome-based display devices over Serial (USB CDC).
- **Web UI**: Real-time dashboard for monitoring host health, add-ons, and integrations.
- **Pure Home Assistant Proxy**: The bridge acts strictly as a proxy, gathering all metrics safely and securely from Home Assistant entities. It does NOT poll local host hardware directly.
- **Auto-Discovery**: Automatically finds and configures Home Assistant System Monitor entities.

## Prerequisites & Sensors

Because this add-on does not poll local hardware, it relies on Home Assistant to provide the data. 
You must configure the **System Monitor** integration in Home Assistant to provide the basic host metrics (CPU, RAM, Disk, Temp, Network, Uptime).

If you want to display **GPU Metrics** or **Fan RPM**, you will need to add those specific sensors to Home Assistant (e.g., via HACS integrations or command-line sensors) and then map them in the Web UI.

## Configuration (The Two Places)

There are two places where configuration is managed:

1. **Add-on Configuration Tab (Home Assistant)**
   - Used *only* for basic hardware mapping.
   - **Serial Port Selector:** Pick the USB device where your ESP is connected.

2. **Web UI (Ingress) - Primary Configuration**
   - Click "Open Web UI" to access the main configuration interface.
   - **Agent Configuration:** Set Baud Rate, Polling Intervals, and toggle Power Control.
   - **Home Assistant Proxy:** This is where you map your `sensor.xxx` entities to the bridge. You can use the "Discover Entities" button to automatically fill in standard System Monitor sensors.

## Installation

1. Add this repository to your Home Assistant Add-on Store.
2. Install **ESP Host Bridge**.
3. Go to the **Configuration** tab and select your **Serial Port**.
4. Start the add-on and open the **Web UI**.
5. Go to the **Setup** tab in the Web UI to map your sensors.

---

*If you are maintaining this repository from the monorepo, use `sync_from_repo.sh` before publishing.*
