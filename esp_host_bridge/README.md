# ESP Host Bridge Add-on

USB CDC bridge and host telemetry Web UI for ESP display devices.

## Features

- **USB Bridge**: Forwards host metrics to ESPHome-based display devices over Serial (USB CDC).
- **Web UI**: Real-time dashboard for monitoring host health, add-ons, and integrations.
- **Home Assistant Proxy (Green Mode)**: Securely pulls metrics from Home Assistant sensors instead of direct host hardware access.
- **Auto-Discovery**: Automatically finds and configures Home Assistant System Monitor entities.

## Prerequisites (HA Mode)

When running inside Home Assistant, this add-on requires the **System Monitor** integration to be configured. The add-on pulls metrics (CPU, RAM, Disk, etc.) from these entities to provide telemetry to your ESP device without needing full host privilege.

## Installation

1. Add this repository to your Home Assistant Add-on Store.
2. Install **ESP Host Bridge**.
3. Plug in your ESP device and select the correct **Serial Port** in the configuration.
4. Start the add-on and open the **Web UI** to configure your sensors.

---

*If you are maintaining this repository from the monorepo, use `sync_from_repo.sh` before publishing.*
