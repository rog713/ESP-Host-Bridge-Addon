# Changelog

## 2026.03.21.22
- Optimized sensor discovery to prioritize entities starting with `sensor.system_monitor_` for more accurate auto-configuration.

## 2026.03.21.21
- Improved sensor auto-discovery with support for German localized names (e.g., `prozessornutzung`).
- Enhanced internal logging for Home Assistant requests to diagnose discovery and value issues.
- Refined Web UI logic to more reliably hide dashboard cards and ESP tabs for missing/unconfigured metrics.
- Fixed auto-completion in the Web UI to show friendly names alongside entity IDs.

## 2026.03.21.20
- Fixed add-on visibility by using strictly supported schema types (`str` instead of `entity`).
- Enabled native Serial Port dropdown using the `device` selector.
- Improved Debug mode: leaving the Serial Port empty now automatically enables hardware-free logging.
- Corrected baud rate list and default value handling.

## 2026.03.21.19
- Simplified configuration schema to guarantee add-on visibility while keeping native device/entity selectors.
- Improved Debug mode: leaving the Serial Port empty now automatically enables hardware-free logging.

## 2026.03.21.18
- Added support for "NONE" or "DEBUG" as the Serial Port to allow testing telemetry without a physical ESP device.
- Corrected `baud` default type in `config.yaml`.

## 2026.03.21.17
- Added auto-completion for Home Assistant sensors inside the Web UI (Setup tab).
- Re-enabled native Home Assistant sensor pickers in the Add-on Configuration tab with improved strictness.
- Added detailed logging for Home Assistant API requests to debug discovery issues.

## 2026.03.21.16
- Corrected `baud` default value and list format for the configuration UI.
- Improved `serial_port` selector to ensure it populates available host devices in the Add-on Configuration tab.

## 2026.03.21.15
- Refined configuration schema to restore add-on visibility.
- Enabled native `device` selector for Serial Port (dropdown).
- Used standard optional strings for entities to ensure maximum compatibility.
- Strictly followed "Truly optional" rules for the schema (no defaults for optional fields).

## 2026.03.21.14
- Successfully implemented advanced UI selectors for Serial Port (device dropdown) and Home Assistant entities (sensor picker).
- Switched Baud Rate to a dropdown list for better usability.
- Added minimum Home Assistant version requirement (2024.1.0).

## 2026.03.21.13
- Reverted advanced UI selectors (device/entity) to restore add-on visibility in Home Assistant. Standard text fields are used for now to ensure compatibility.
...
## 2026.03.21.12
- Added native Home Assistant serial device selector to the Add-on Configuration tab for easier port selection.
...
## 2026.03.21.11
- Added native Home Assistant entity selectors to the Add-on Configuration tab for easier setup and auto-completion.
...
## 2026.03.21.10
- Fixed Home Assistant Proxy entities not being passed to the agent process, which caused metrics to stay in "Waiting..." status.
...
## 2026.03.21.09
- Moved all configuration settings to the standard Home Assistant Add-on "Configuration" tab.
- Added automatic synchronization between Home Assistant options and the Web UI configuration.
- Added hints in the Web UI about managing settings via the Add-on tab.

## 2026.03.21.08
- Unconfigured Home Assistant sensors are now hidden from the dashboard and ESP preview tabs.

## 2026.03.21.07
- Removed local hardware sensor fallbacks when in Home Assistant mode. All metrics now rely exclusively on the System Monitor integration (Green Mode).
...
## 2026.03.21.06
- Improved Home Assistant entity discovery with better error handling and logging.

## 2026.03.21.05
- Fixed Home Assistant Proxy entities not persisting after Save + Restart.
- Added "Discover Entities" button to auto-detect System Monitor sensors (including German localized names).
- Improved Home Assistant API integration.

## 2026.03.21.04
- Fixed Active Interface and Disk incorrectly showing local devices when Home Assistant Proxy is active.
...
## 2026.03.21.03
- Fixed `NameError: name 'st' is not defined` that caused the Web UI to crash.

## 2026.03.21.02
- Fixed redundant Telemetry UI showing failed local sensors when in Home Assistant Proxy (Green Mode).
- Added missing Fan Speed Entity field to the Home Assistant Proxy configuration.
- Corrected Disk Write speed placeholder in the UI.

## 2026.03.20.1
- Initial Home Assistant add-on scaffold for ESP Host Bridge.
- Bakes Python dependencies into the image at build time.
- Persists the Host Bridge Web UI config at `/data/config.json`.
- Enables host-level access needed for serial, network, disk, and Docker-backed telemetry.
