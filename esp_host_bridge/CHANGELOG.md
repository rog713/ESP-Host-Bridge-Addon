## 2026.03.31.6

- Fixed add-on version reporting so the Web UI uses the add-on version instead of the vendored core package version.
- Added static asset cache busting for Home Assistant ingress so updated CSS and JS load after add-on upgrades.

## 2026.03.31.5

- Fixed Home Assistant ingress asset and API URLs so the add-on Web UI styles and scripts load under ingress correctly.

## 2026.03.31.4

- Rebuilt the Home Assistant add-on from the refactored `esp_host_bridge` core.
- Switched the add-on payload to vendor the maintained package layout instead of the old `host_metrics.py` snapshot.

## 2026.03.31.3

- Moved the add-on source into the maintained `ESP-Host-Bridge` repo layout.
- Vendor the current `esp_host_bridge` package into the add-on build context instead of the old `host_metrics.py` snapshot.

## 2026.03.24.1

- Added a Host Bridge version pill to the Web UI.
- Published the current bridge status UI updates.

# Changelog

## 2026.03.23.2

- Prefer the configured serial device path over transient `/dev/ttyACM*` nodes.

## 2026.03.23.1

- Added a display sleep status indicator to the Web UI.

## 2026.03.22.9

- Refined the ESP preview layout and centering in the add-on Web UI.

## 2026.03.22.8

- Pause USB telemetry output while the display is asleep.
- Resume output and send a fresh update when the display wakes.

## 2026.03.22.7

- Updated the ESP preview layout and spacing.

## 2026.03.22.6

- Removed the Home Assistant Configuration form.
- Settings are now managed only through the Web UI.

## 2026.03.22.5
- Added basic password protection for direct Web UI access.
- Trusts Home Assistant ingress and keeps `/api/status` open for add-on health checks.
- Stores the direct Web UI password as a hash in the runtime config and generates a persistent session secret automatically.

## 2026.03.22.4
- Disabled `full_access` to test whether the current add-on can run under the narrower Home Assistant permission model.
- Kept `host_network`, `host_uts`, `udev`, `uart`, and AppArmor enabled so the hardening delta stays isolated.

## 2026.03.22.3
- Re-enabled the default Home Assistant AppArmor profile for the add-on.
- Kept the existing host-network runtime model unchanged.

## 2026.03.22.2
- Fixed Home Assistant Ingress path handling in the Web UI.
- Removed absolute `/api/...` and `/static/...` assumptions from the add-on frontend.
- Fixed add-on launcher validation mode so it no longer writes to `/data` during dry-run checks.

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
