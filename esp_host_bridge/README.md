# ESP Host Bridge Add-on

`esp_host_bridge/` is the Home Assistant add-on payload.

## Contents

- `config.yaml`: add-on manifest
- `app/`: vendored `esp_host_bridge` runtime package
- `start_addon.py`: startup wrapper
- `run.sh`: container entrypoint
- `sync_from_repo.sh`: refreshes `app/` from the maintained repo root
