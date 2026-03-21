# Changelog

## 2026.03.21.04
- Fixed Active Interface and Disk incorrectly showing local devices when Home Assistant Proxy is active.

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
