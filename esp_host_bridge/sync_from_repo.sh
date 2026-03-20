#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
SRC_DIR="$ROOT_DIR/host_metrics"
DST_DIR="$ROOT_DIR/host_metrics/homeassistant_repo/esp_host_bridge/app"

mkdir -p "$DST_DIR"

cp "$SRC_DIR/host_metrics.py" "$DST_DIR/host_metrics.py"
cp "$SRC_DIR/host_metrics_ui_assets.py" "$DST_DIR/host_metrics_ui_assets.py"
cp "$SRC_DIR/host_metrics_webui_templates.py" "$DST_DIR/host_metrics_webui_templates.py"
cp "$SRC_DIR/host_ui.js" "$DST_DIR/host_ui.js"
cp "$SRC_DIR/host_ui.css" "$DST_DIR/host_ui.css"
cp "$SRC_DIR/requirements.txt" "$DST_DIR/requirements.txt"

echo "Synced Host Bridge runtime files into the Home Assistant app repository context."
