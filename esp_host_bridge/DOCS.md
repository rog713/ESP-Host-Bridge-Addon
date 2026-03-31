# ESP Host Bridge

ESP Host Bridge runs the Host Bridge Web UI inside Home Assistant for an ESP display connected over USB.

## Best fit

Use this add-on when Home Assistant, the ESP device, and the host resources you want to monitor are all on the same machine.

## How it works

- runtime settings are stored in `/data/config.json`
- settings are managed in the Web UI
- the Web UI is available through Home Assistant Ingress
- direct Web UI access can be protected with a password
- the add-on vendors the maintained `esp_host_bridge` package into `/opt/esp-host-bridge/app`

## Data sources

- local host telemetry for CPU, memory, uptime, network, disk, and sensors when available
- Home Assistant Supervisor API for add-ons and host power control
- Home Assistant Core WebSocket API for integrations
- Home Assistant logbook API for recent activity

## Reset direct Web UI password

If you forget the direct Web UI password, open the add-on through Home Assistant Ingress, go to `Direct Web UI Security`, and either disable protection or set a new password, then save and restart the add-on.
