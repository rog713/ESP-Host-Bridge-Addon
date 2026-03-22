# Changelog

## 2026.03.22.1
- Added Home Assistant Ingress and panel metadata for a more native add-on UI.
- Added stable add-on `options` and `schema` entries for the Configuration tab.
- Syncs add-on Configuration tab values into `/data/config.json` at startup.
- Kept the existing host-access model and runtime behavior intact.

## 2026.03.20.1
- Initial Home Assistant add-on scaffold for ESP Host Bridge.
- Bakes Python dependencies into the image at build time.
- Persists the Host Bridge Web UI config at `/data/config.json`.
- Enables host-level access needed for serial, network, disk, and Docker-backed telemetry.
