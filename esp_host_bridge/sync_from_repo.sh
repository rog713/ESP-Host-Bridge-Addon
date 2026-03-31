#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
SRC_DIR="$ROOT_DIR"
DST_DIR="$ROOT_DIR/homeassistant_addon/esp_host_bridge/app"

rm -rf "$DST_DIR"
mkdir -p "$DST_DIR"

cp "$SRC_DIR/pyproject.toml" "$DST_DIR/pyproject.toml"
cp "$SRC_DIR/README.md" "$DST_DIR/README.md"
rsync -a \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$SRC_DIR/esp_host_bridge/" "$DST_DIR/esp_host_bridge/"

echo "Synced esp_host_bridge package into Home Assistant add-on context."
