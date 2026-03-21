# Changelog

## 2026.03.21.13
- Reverted advanced UI selectors (device/entity) to restore add-on visibility in Home Assistant. Standard text fields are used for now to ensure compatibility.

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
