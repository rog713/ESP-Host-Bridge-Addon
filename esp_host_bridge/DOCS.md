# ESP Host Bridge

ESP Host Bridge runs the Host Bridge Web UI inside Home Assistant for an ESP display connected over USB.

## Best fit

Use this add-on when Home Assistant, the ESP device, and the host resources you want to monitor are all on the same machine.

## How it works

- add-on options are stored in `/data/options.json`
- runtime settings are stored in `/data/config.json`
- add-on options are synced into the runtime config on startup
- the Web UI is available through Home Assistant Ingress
- direct Web UI access can be protected with a password

## Data sources

- local host telemetry for CPU, memory, uptime, network, disk, and sensors when available
- Home Assistant Supervisor API for add-ons and host power control
- Home Assistant Core WebSocket API for integrations
- Home Assistant logbook API for recent activity
