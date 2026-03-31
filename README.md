# ESP Host Bridge Add-on Repository

This directory is the maintained source for the Home Assistant add-on export.

## Install

1. Add the published `ESP Host Bridge` add-on repository to Home Assistant.
2. Install `ESP Host Bridge`.
3. Start the add-on.
4. Open the Web UI through Home Assistant Ingress or the direct add-on URL.
5. Open the Web UI and configure your settings there.

## What it does

- sends host telemetry to an ESP display over USB CDC
- shows Home Assistant add-ons, integrations, and recent activity in the Web UI
- reads local host metrics such as CPU, memory, network, and disk
- keeps configuration in the Web UI instead of the Home Assistant Configuration page

## Source Layout

- `repository.yaml`: Home Assistant repository metadata
- `esp_host_bridge/`: add-on payload
- `esp_host_bridge/app/`: vendored `esp_host_bridge` package used to build the add-on image

## Best fit

Use this add-on when Home Assistant, the ESP device, and the host resources you want to monitor are all on the same machine.

If Home Assistant is running on a different machine than the ESP device or the monitored host, use the regular Host Bridge install instead.

## Reset direct Web UI password

If you forget the direct Web UI password, open the add-on through Home Assistant Ingress, go to `Direct Web UI Security`, and either disable protection or set a new password, then save and restart the add-on.

## Credits

- [LordGuenni](https://github.com/LordGuenni) for packaging and Home Assistant add-on structure improvements that helped shape this add-on.
