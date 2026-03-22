# ESP Host Bridge Add-on Repository

This repository contains the Home Assistant add-on for `ESP Host Bridge`.

## Install

1. Add `https://github.com/rog713/ESP-Host-Bridge-Addon` as a custom Home Assistant add-on repository.
2. Install `ESP Host Bridge`.
3. Start the add-on.
4. Open the Web UI through Home Assistant Ingress or the direct add-on URL.
5. Select the serial port and save your settings.

## What it does

- sends host telemetry to an ESP display over USB CDC
- shows Home Assistant add-ons, integrations, and recent activity in the Web UI
- reads local host metrics such as CPU, memory, network, and disk

## Best fit

Use this add-on when Home Assistant, the ESP device, and the host resources you want to monitor are all on the same machine.

If Home Assistant is running on a different machine than the ESP device or the monitored host, use the regular Host Bridge install instead.
## Reset direct Web UI password

If you forget the direct Web UI password, open the add-on through Home Assistant Ingress, go to `Direct Web UI Security`, and either disable protection or set a new password, then save and restart the add-on.
