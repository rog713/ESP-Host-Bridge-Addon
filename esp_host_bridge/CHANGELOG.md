# Changelog

## 2026.03.20.1
- Initial Home Assistant add-on scaffold for ESP Host Bridge.
- Bakes Python dependencies into the image at build time.
- Persists the Host Bridge Web UI config at `/data/config.json`.
- Enables host-level access needed for serial, network, disk, and Docker-backed telemetry.
