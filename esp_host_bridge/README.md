# ESP Host Bridge Add-on

`esp_host_bridge/` is the Home Assistant add-on payload that Home Assistant installs from this repository.

## What is here

- `config.yaml`: add-on manifest and Configuration-tab schema
- `app/`: Host Bridge runtime and Web UI
- `start_addon.py`: add-on launcher that syncs Home Assistant options into `/data/config.json`
- `run.sh`: container entrypoint

## Current behavior

- exposes the Web UI through Home Assistant Ingress
- keeps a direct Web UI endpoint on port `8654`
- stores runtime settings in `/data/config.json`
- merges add-on options from `/data/options.json` into `/data/config.json` on startup
- supports direct Web UI password protection

## Maintainer note

If you are updating this repository from the monorepo, sync the current runtime files into this directory before publishing.
