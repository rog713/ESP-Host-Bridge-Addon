#!/usr/bin/env python3
from __future__ import annotations

import argparse
import atexit
import copy
import difflib
import html
import http.client
import json
import logging
import os
import platform
import re
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import asyncio
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple
from urllib.parse import quote_plus

# removed psutil

try:
    import serial  # type: ignore
    from serial import SerialException  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception:
    serial = None
    SerialException = Exception
    list_ports = None

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

SERIAL_RETRY_SECONDS = 2
RX_BUFFER_MAX_BYTES = 4096
RX_BUFFER_KEEP_BYTES = 1024
DISK_TEMP_REFRESH_SECONDS = 15.0
DISK_USAGE_REFRESH_SECONDS = 10.0
SLOW_SENSOR_REFRESH_SECONDS = 5.0
DOCKER_WARN_INTERVAL_SECONDS = 30.0
VIRSH_WARN_INTERVAL_SECONDS = 30.0
ACTIVITY_WARN_INTERVAL_SECONDS = 30.0
MAX_LOG_LINES = 800
METRIC_HISTORY_POINTS = 90
WEBUI_DEFAULT_PORT = 8654
MDI_FONT_CSS_URL = "https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css"
MDI_CODEPOINT_CACHE_PATH = Path(__file__).resolve().parent / ".host_metrics_mdi_codepoints.json"
ESP_BOOT_LINE_RE = re.compile(r"\bESP=BOOT\b(?:,ID=([0-9A-Fa-f]+))?(?:,REASON=([A-Z0-9_]+))?")
GENERIC_HOME_ASSISTANT_ACRONYMS = {
    "api",
    "cpu",
    "gpu",
    "ha",
    "id",
    "ip",
    "js",
    "mac",
    "mqtt",
    "ram",
    "ssh",
    "tcp",
    "udp",
    "ui",
    "usb",
    "vm",
    "vpn",
    "ws",
    "wss",
    "zha",
}

_mdi_codepoint_map_lock = threading.Lock()
_mdi_codepoint_map_cache: Optional[Dict[str, int]] = None
_mdi_codepoint_map_cache_err: Optional[str] = None


@dataclass
class RuntimeState:
    cpu_prev_total: Optional[int] = None
    cpu_prev_idle: Optional[int] = None
    active_iface: Optional[str] = None
    prev_rx: Optional[float] = None
    prev_tx: Optional[float] = None
    prev_t: Optional[float] = None
    last_addons_warn_ts: float = 0.0
    last_virsh_warn_ts: float = 0.0
    disk_temp_c: float = 0.0
    disk_temp_available: bool = False
    last_disk_temp_ts: float = 0.0
    disk_usage_pct: float = 0.0
    last_disk_usage_ts: float = 0.0
    fan_rpm: float = 0.0
    fan_available: bool = False
    gpu_temp_c: float = 0.0
    gpu_util_pct: float = 0.0
    gpu_mem_pct: float = 0.0
    gpu_available: bool = False
    last_slow_sensor_ts: float = 0.0
    active_disk: Optional[str] = None
    prev_disk_read_b: Optional[float] = None
    prev_disk_write_b: Optional[float] = None
    rx_buf: str = ""
    tx_frame_index: int = 0
    cached_addons: list[dict[str, Any]] = field(default_factory=list)
    cached_addon_counts: Dict[str, int] = field(
        default_factory=lambda: {"running": 0, "stopped": 0, "unhealthy": 0}
    )
    last_addons_refresh_ts: float = 0.0
    cached_integrations: list[dict[str, Any]] = field(default_factory=list)
    cached_integration_counts: Dict[str, int] = field(
        default_factory=lambda: {"running": 0, "stopped": 0, "paused": 0, "other": 0}
    )
    last_integrations_refresh_ts: float = 0.0
    cached_activity: list[dict[str, Any]] = field(default_factory=list)
    last_activity_refresh_ts: float = 0.0
    last_activity_warn_ts: float = 0.0
    host_name_sent: bool = False
    ha_token_present: bool = False
    ha_addons_api_ok: Optional[bool] = None
    ha_integrations_api_ok: Optional[bool] = None
    ha_activity_api_ok: Optional[bool] = None
    # HA Host Info Cache
    ha_host_info: Dict[str, Any] = field(default_factory=dict)
    last_ha_host_info_ts: float = 0.0


def safe_float(v: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: Optional[int] = 0) -> Optional[int]:
    try:
        return int(float(v))
    except Exception:
        return default


def _read_first_line(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readline().strip()


def resolve_host_name() -> str:
    # If we are in HA mode and have a token, try to get the actual host name via API
    # instead of the container ID.
    if is_home_assistant_app_mode() and SUPERVISOR_TOKEN:
        try:
            # We must use a direct request here because the regular proxy functions might not be defined yet
            # or could cause circular dependencies if moved.
            url = SUPERVISOR_HTTP_URL + "/host/info"
            req = urllib.request.Request(url, method="GET")
            req.add_header("Authorization", f"Bearer {SUPERVISOR_TOKEN}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                # API returns {"result": "ok", "data": {"hostname": "...", ...}}
                hn = data.get("data", {}).get("hostname")
                if hn:
                    return str(hn).strip()
        except Exception:
            pass

    for candidate in (socket.gethostname(), platform.node(), os.environ.get("HOSTNAME", "")):
        name = str(candidate or "").strip()
        if name:
            return name
    return ""


def compact_host_name(value: str, limit: int = 63) -> str:
    cleaned = str(value or "").replace("\r", "").replace("\n", "").replace(",", "_").strip()
    return cleaned[:limit]


def resolve_supervisor_token() -> str:
    token = str(os.environ.get("SUPERVISOR_TOKEN", "") or "").strip()
    if token:
        return token
    try:
        path = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        pass
    return ""


SUPERVISOR_TOKEN = resolve_supervisor_token()
SUPERVISOR_HTTP_URL = str(os.environ.get("ESP_HOST_BRIDGE_SUPERVISOR_HTTP", "http://supervisor") or "http://supervisor").rstrip("/")
SUPERVISOR_WS_URL = str(os.environ.get("ESP_HOST_BRIDGE_SUPERVISOR_WS", "ws://supervisor/core/websocket") or "ws://supervisor/core/websocket").rstrip("/")
HOME_ASSISTANT_PLATFORM_MODE = str(os.environ.get("ESP_HOST_BRIDGE_PLATFORM_MODE", "") or "").strip().lower()
HOME_ASSISTANT_SELF_SLUG = str(os.environ.get("ESP_HOST_BRIDGE_SELF_SLUG", "esp_host_bridge") or "esp_host_bridge").strip()


def is_home_assistant_app_mode() -> bool:
    if HOME_ASSISTANT_PLATFORM_MODE == "homeassistant":
        return True
    return bool(SUPERVISOR_TOKEN)


HOST_NAME = resolve_host_name()
HOST_NAME_USB = compact_host_name(HOST_NAME)


def _humanize_home_assistant_slug(value: Any) -> str:
    slug = str(value or "").strip().lower()
    if not slug:
        return ""
    parts = [p for p in re.split(r"[_\-]+", slug) if p]
    if not parts:
        return slug
    return " ".join(
        part.upper() if part in GENERIC_HOME_ASSISTANT_ACRONYMS else part.capitalize()
        for part in parts
    )


def compact_addons(docker_data: list[dict[str, Any]], max_items: int = 10) -> str:
    out: list[str] = []
    for c in docker_data[:max_items]:
        if not isinstance(c, dict):
            continue
        raw_name = c.get("name") or c.get("Names") or "container"
        if isinstance(raw_name, list):
            name = str(raw_name[0] if raw_name else "container")
        else:
            name = str(raw_name)
        name = name.lstrip("/").replace(",", "_").replace(";", "_")
        if len(name) > 24:
            name = name[:24]
        status_raw = str(c.get("status") or c.get("State") or "").lower()
        state = "up" if any(x in status_raw for x in ["running", "up", "healthy"]) else "down"
        out.append(f"{name}|{state}")
    return ";".join(out)


def _sanitize_compact_token(v: Any, fallback: str = "") -> str:
    s = str(v or fallback).strip()
    if not s:
        s = fallback
    return s.replace(",", "_").replace(";", "_").replace("|", "_")


def classify_integration_state(state_raw: Any) -> tuple[str, str]:
    s = str(state_raw or "").strip().lower()
    if not s:
        return "stopped", "Stopped"
    if any(x in s for x in ("running", "idle", "in shutdown", "shutdown", "no state")):
        return "running", "Running"
    if any(x in s for x in ("paused", "pmsuspended", "suspended", "blocked")):
        return "paused", "Paused"
    if any(x in s for x in ("shut off", "shutoff", "crashed")):
        return "stopped", "Stopped"
    return "other", s.title()


def compact_integrations(vm_data: list[dict[str, Any]], max_items: int = 10) -> str:
    out: list[str] = []
    for vm in vm_data[:max_items]:
        if not isinstance(vm, dict):
            continue
        name = _sanitize_compact_token(vm.get("name"), "vm")
        if len(name) > 24:
            name = name[:24]
        state_key, state_label = classify_integration_state(vm.get("state"))
        preserved_state_label = _sanitize_compact_token(vm.get("state_label"), "")
        if preserved_state_label:
            state_label = preserved_state_label
        vcpus = max(0, safe_int(vm.get("vcpus"), 0) or 0)
        mem_mib = max(0, safe_int(vm.get("max_mem_mib"), 0) or 0)
        out.append(
            f"{name}|{_sanitize_compact_token(state_key, 'stopped')}|"
            f"{vcpus}|{mem_mib}|{_sanitize_compact_token(state_label, 'Stopped')}"
        )
    return ";".join(out) if out else "-"


def _compact_activity_age(seconds: Optional[float]) -> str:
    value = max(0, int(round(safe_float(seconds, 0.0) or 0.0)))
    if value < 90:
        return f"{value}s"
    if value < 5400:
        return f"{max(1, round(value / 60))}m"
    if value < 172800:
        return f"{max(1, round(value / 3600))}h"
    return f"{max(1, round(value / 86400))}d"


ACTIVITY_SOURCE_LABELS = {
    "automation": "auto",
    "binary_sensor": "binary",
    "button": "button",
    "camera": "camera",
    "climate": "climate",
    "cover": "cover",
    "device_tracker": "tracker",
    "event": "event",
    "fan": "fan",
    "input_boolean": "boolean",
    "input_number": "number",
    "input_select": "select",
    "light": "light",
    "lock": "lock",
    "media_player": "media",
    "number": "number",
    "person": "person",
    "remote": "remote",
    "scene": "scene",
    "script": "script",
    "select": "select",
    "sensor": "sensor",
    "sun": "sun",
    "switch": "switch",
    "update": "update",
    "vacuum": "vacuum",
    "weather": "weather",
}


def _compact_activity_source(domain: Any, entity_id: Any) -> str:
    source = str(domain or "").strip().lower()
    entity = str(entity_id or "").strip().lower()
    if not source and "." in entity:
        source = entity.split(".", 1)[0]
    if not source:
        return ""
    label = ACTIVITY_SOURCE_LABELS.get(source, source.replace("_", " "))
    label = _sanitize_compact_token(label, "")
    if len(label) > 10:
        label = label[:10]
    return label


def _compact_activity_tail(entity_id: Any) -> str:
    tail = str(entity_id or "").strip()
    if "." in tail:
        tail = tail.split(".", 1)[1]
    tail = _sanitize_compact_token(tail, "")
    if len(tail) > 18:
        tail = tail[:18]
    return tail


def compact_activity_entries(rows: list[dict[str, Any]], max_items: int = 5) -> str:
    out: list[str] = []
    now = time.time()
    for row in rows[:max_items]:
        if not isinstance(row, dict):
            continue
        name = _sanitize_compact_token(row.get("name"), "Activity")
        message = _sanitize_compact_token(row.get("message"), "updated")
        source = _sanitize_compact_token(
            row.get("source"),
            _compact_activity_source(row.get("domain"), row.get("entity_id")),
        )
        tail = _sanitize_compact_token(
            row.get("entity_tail"),
            _compact_activity_tail(row.get("entity_id")),
        )
        when_ts = safe_float(row.get("when_ts"), None)
        age = _compact_activity_age((now - when_ts) if when_ts is not None and when_ts > 0 else None)
        if len(name) > 22:
            name = name[:22]
        if len(message) > 16:
            message = message[:16]
        if len(age) > 5:
            age = age[:5]
        if len(source) > 10:
            source = source[:10]
        if len(tail) > 18:
            tail = tail[:18]
        out.append(f"{name}|{message}|{age}|{source}|{tail}")
    return ";".join(out) if out else "-"


def _supervisor_request_json(path: str, timeout: float, method: str = "GET", payload: Any = None) -> Any:
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")
    url = SUPERVISOR_HTTP_URL + path
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method.upper())
    req.add_header("Authorization", f"Bearer {SUPERVISOR_TOKEN}")
    req.add_header("Content-Type", "application/json")
    logging.info("HA Proxy Request: %s %s", method.upper(), url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            raw = resp.read()
            logging.info("HA Proxy Response: %s (bytes=%d)", status, len(raw))
    except Exception as e:
        logging.error("HA Proxy Error: %s", e)
        raise
    if not raw:
        return {}
    decoded = json.loads(raw.decode("utf-8", errors="ignore"))
    if isinstance(decoded, dict) and "data" in decoded:
        return decoded.get("data")
    return decoded


def get_home_assistant_state(entity_id: str, timeout: float = 2.0) -> Tuple[Optional[str], Dict[str, Any]]:
    """Fetch the 'state' string and attributes for a specific Home Assistant entity."""
    if not SUPERVISOR_TOKEN or not entity_id:
        return None, {}
    try:
        # The Supervisor API provides a proxy to the HA Core API at /core/api/states/<entity_id>
        res = _supervisor_request_json(f"/core/api/states/{entity_id}", timeout=timeout)
        if isinstance(res, dict) and "state" in res:
            val = str(res["state"])
            attrs = res.get("attributes", {})
            logging.info("HA State [%s] = %s (unit=%s)", entity_id, val, attrs.get("unit_of_measurement"))
            return val, attrs
    except Exception as e:
        logging.warning("Failed to fetch HA state for %s: %s", entity_id, e)
    return None, {}


def get_home_assistant_metric_converted(entity_id: str, timeout: float, target_kb: bool = False) -> Optional[float]:
    """Fetch an HA state and convert it to a numeric value, handling units like MB/s -> kbps or kB/s."""
    state, attrs = get_home_assistant_state(entity_id, timeout=timeout)
    if state is None:
        return None
    val = safe_float(state, None)
    if val is None:
        return None
    
    uom = str(attrs.get("unit_of_measurement", "")).lower()
    
    # Target kbps (bits) for network, kB/s (bytes) for disk
    if target_kb: # Convert to kbps (bits)
        if "mib/s" in uom or "mb/s" in uom:
            return val * 1024.0 * 8.0
        if "kib/s" in uom or "kb/s" in uom:
            return val * 8.0
        if "b/s" in uom:
            return (val * 8.0) / 1000.0
    else: # Convert to kB/s (bytes)
        if "mib/s" in uom or "mb/s" in uom:
            return val * 1024.0
        if "kib/s" in uom or "kb/s" in uom:
            return val
        if "b/s" in uom:
            return val / 1024.0
            
    return val


def get_home_assistant_all_states(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Fetch all states from the Home Assistant Core API via the Supervisor proxy."""
    if not SUPERVISOR_TOKEN:
        return []
    try:
        res = _supervisor_request_json("/core/api/states", timeout=timeout)
        if isinstance(res, list):
            return res
    except Exception:
        pass
    return []


def discover_ha_proxy_entities(timeout: float = 5.0) -> Dict[str, str]:
    """Search all HA entities for likely candidates for System Monitor metrics."""
    states = get_home_assistant_all_states(timeout=timeout)
    found = {}

    def _match(eid: str, matches: list[str]) -> bool:
        return any(m in eid.lower() for m in matches)

    # First pass: look specifically for sensor.system_monitor_* (highly likely matches)
    for item in states:
        eid = item.get("entity_id", "")
        if not eid.startswith("sensor.system_monitor_"):
            continue
        attrs = item.get("attributes", {})
        uom = attrs.get("unit_of_measurement", "")
        dev_class = attrs.get("device_class", "")

        # CPU Usage
        if not found.get("ha_entity_cpu") and uom == "%" and _match(eid, ["prozessornutzung", "processor_use", "cpu_usage"]):
            found["ha_entity_cpu"] = eid
        # Memory Usage
        if not found.get("ha_entity_mem") and uom == "%" and _match(eid, ["arbeitsspeicherauslastung", "memory_use_percent", "ram_usage"]):
            found["ha_entity_mem"] = eid
        # Temperature
        if not found.get("ha_entity_temp") and uom in ["°C", "°F"] and _match(eid, ["prozessortemperatur", "processor_temperature", "cpu_temp"]):
            found["ha_entity_temp"] = eid
        # Disk Usage
        if not found.get("ha_entity_disk_pct") and uom == "%" and _match(eid, ["massenspeicher_auslastung", "disk_usage", "disk_use"]):
             found["ha_entity_disk_pct"] = eid
        # Network RX
        if not found.get("ha_entity_net_rx") and (uom in ["B/s", "kB/s", "MB/s", "KiB/s", "MiB/s"] or dev_class == "data_rate") and _match(eid, ["eingehender_netzwerkdurchsatz", "network_throughput_in", "rx_speed"]):
             found["ha_entity_net_rx"] = eid
        # Network TX
        if not found.get("ha_entity_net_tx") and (uom in ["B/s", "kB/s", "MB/s", "KiB/s", "MiB/s"] or dev_class == "data_rate") and _match(eid, ["ausgehender_netzwerkdurchsatz", "network_throughput_out", "tx_speed"]):
             found["ha_entity_net_tx"] = eid
        # Uptime
        if not found.get("ha_entity_uptime") and (dev_class in ["timestamp", "duration"] or _match(eid, ["letzter_systemstart", "last_boot", "uptime"])):
             found["ha_entity_uptime"] = eid

    # Second pass: general match for any remaining fields
    for item in states:
        eid = item.get("entity_id", "")
        if not eid.startswith("sensor."):
            continue
        attrs = item.get("attributes", {})
        uom = attrs.get("unit_of_measurement", "")
        dev_class = attrs.get("device_class", "")

        # CPU Usage
        if not found.get("ha_entity_cpu") and uom == "%" and _match(eid, ["processor_use", "cpu_usage", "cpu_util", "cpu_percent", "prozessornutzung"]):
            found["ha_entity_cpu"] = eid
        # Memory Usage
        if not found.get("ha_entity_mem") and uom == "%" and _match(eid, ["memory_use_percent", "ram_usage", "memory_usage", "arbeitsspeicherauslastung"]):
            found["ha_entity_mem"] = eid
        # Temperature
        if not found.get("ha_entity_temp") and uom in ["°C", "°F"] and _match(eid, ["processor_temperature", "cpu_temp", "thermal", "prozessortemperatur"]):
            if "disk" not in eid.lower() and "gpu" not in eid.lower():
                 found["ha_entity_temp"] = eid
        # Disk Usage
        if not found.get("ha_entity_disk_pct") and uom == "%" and _match(eid, ["disk_usage", "disk_use", "storage_usage", "massenspeicher_auslastung"]):
             found["ha_entity_disk_pct"] = eid
        # Network RX
        if not found.get("ha_entity_net_rx") and (uom in ["B/s", "kB/s", "MB/s", "GiB/s", "KiB/s", "MiB/s"] or dev_class == "data_rate") and _match(eid, ["network_throughput_in", "network_in", "rx_speed", "eingehender_netzwerkdurchsatz"]):
             found["ha_entity_net_rx"] = eid
        # Network TX
        if not found.get("ha_entity_net_tx") and (uom in ["B/s", "kB/s", "MB/s", "GiB/s", "KiB/s", "MiB/s"] or dev_class == "data_rate") and _match(eid, ["network_throughput_out", "network_out", "tx_speed", "ausgehender_netzwerkdurchsatz"]):
             found["ha_entity_net_tx"] = eid
        # Uptime
        if not found.get("ha_entity_uptime") and (dev_class in ["timestamp", "duration"] or _match(eid, ["last_boot", "uptime", "letzter_systemstart"])):
             found["ha_entity_uptime"] = eid
        # Disk Read
        if not found.get("ha_entity_disk_read") and (uom in ["B/s", "kB/s", "MB/s"] or dev_class == "data_rate") and _match(eid, ["disk_read_speed", "disk_read"]):
             found["ha_entity_disk_read"] = eid
        # Disk Write
        if not found.get("ha_entity_disk_write") and (uom in ["B/s", "kB/s", "MB/s"] or dev_class == "data_rate") and _match(eid, ["disk_write_speed", "disk_write"]):
             found["ha_entity_disk_write"] = eid

    return found


def get_home_assistant_addons(timeout: float) -> list[dict[str, Any]]:
    payload = _supervisor_request_json("/addons", timeout=timeout)
    rows = payload.get("addons") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    addons: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if slug and slug == HOME_ASSISTANT_SELF_SLUG:
            continue
        state = str(item.get("state") or "").strip().lower()
        update_available = bool(item.get("update_available"))
        available = bool(item.get("available", True))
        label = str(item.get("name") or slug or "App").strip()
        state_text = "running" if state == "started" else "stopped"
        if update_available or not available:
            state_text = f"{state_text} issue"
        addons.append(
            {
                "name": label,
                "slug": slug,
                "state": state_text,
                "status": state_text,
                "update_available": update_available,
                "available": available,
            }
        )
    addons.sort(key=lambda row: (0 if "running" in str(row.get("state", "")).lower() else 1, str(row.get("name") or "").lower()))
    return addons


async def _fetch_home_assistant_integrations_async(timeout: float) -> list[dict[str, Any]]:
    try:
        import websockets  # type: ignore
    except Exception as e:
        raise RuntimeError("websockets package is unavailable") from e

    async with websockets.connect(SUPERVISOR_WS_URL, open_timeout=timeout, close_timeout=timeout, max_size=8 * 1024 * 1024) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if not isinstance(hello, dict) or hello.get("type") != "auth_required":
            raise RuntimeError("unexpected Home Assistant websocket greeting")
        await ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR_TOKEN}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant websocket auth failed")
        await ws.send(json.dumps({"id": 1, "type": "config/entity_registry/list_for_display"}))
        result = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if not isinstance(result, dict) or not result.get("success"):
            raise RuntimeError("entity registry query failed")
        payload = result.get("result")
        if not isinstance(payload, dict):
            return []
        entities = payload.get("entities")
        if not isinstance(entities, list):
            return []
        counts: Dict[str, int] = {}
        for row in entities:
            if not isinstance(row, dict):
                continue
            platform_slug = str(row.get("pl") or "").strip().lower()
            if not platform_slug:
                continue
            counts[platform_slug] = counts.get(platform_slug, 0) + 1
        items: list[dict[str, Any]] = []
        for slug, entity_count in counts.items():
            entity_label = f"{entity_count} entity" if entity_count == 1 else f"{entity_count} entities"
            items.append(
                {
                    "name": _humanize_home_assistant_slug(slug),
                    "state": "running",
                    "vcpus": 0,
                    "max_mem_mib": 0,
                    "state_label": entity_label,
                    "entity_count": entity_count,
                    "platform": slug,
                }
            )
        items.sort(key=lambda row: (-safe_int(row.get("entity_count"), 0), str(row.get("name") or "").lower()))
        return items


def get_home_assistant_integrations(timeout: float) -> list[dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")
    items = asyncio.run(_fetch_home_assistant_integrations_async(timeout))
    normalized: list[dict[str, Any]] = []
    for item in items:
        state_label = str(item.get("state_label") or "Loaded")
        normalized.append(
            {
                "name": str(item.get("name") or "Integration"),
                "state": "running",
                "vcpus": 0,
                "max_mem_mib": 0,
                "state_label": state_label,
                "entity_count": safe_int(item.get("entity_count"), 0) or 0,
            }
        )
    return normalized


def _parse_home_assistant_timestamp(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_home_assistant_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def get_home_assistant_logbook_entries(
    timeout: float,
    *,
    limit: int = 12,
    lookback_minutes: int = 180,
) -> list[dict[str, Any]]:
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is not available")
    safe_limit = max(1, min(25, safe_int(limit, 12) or 12))
    safe_lookback = max(5, min(1440, safe_int(lookback_minutes, 180) or 180))
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(minutes=safe_lookback)
    start_token = urllib.parse.quote(_format_home_assistant_timestamp(start_dt), safe="")
    query = urllib.parse.urlencode({"end_time": _format_home_assistant_timestamp(end_dt)})
    payload = _supervisor_request_json(f"/core/api/logbook/{start_token}?{query}", timeout=timeout)
    rows = payload if isinstance(payload, list) else []
    entries: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        domain = str(item.get("domain") or "").strip().lower()
        entity_id = str(item.get("entity_id") or "").strip()
        message = str(item.get("message") or "").strip()
        when_raw = str(item.get("when") or "").strip()
        when_dt = _parse_home_assistant_timestamp(when_raw)
        when_ts = float(when_dt.timestamp()) if when_dt else 0.0
        if not name:
            if entity_id:
                name = _humanize_home_assistant_slug(entity_id.split(".", 1)[-1])
            elif domain:
                name = _humanize_home_assistant_slug(domain)
            else:
                name = "Activity"
        if not message:
            message = "updated"
        summary = f"{name} {message}".strip()
        entries.append(
            {
                "name": name,
                "message": message,
                "summary": summary,
                "entity_id": entity_id,
                "entity_tail": _compact_activity_tail(entity_id),
                "domain": domain,
                "source": _compact_activity_source(domain, entity_id),
                "when": when_raw,
                "when_ts": when_ts,
            }
        )
    entries.sort(key=lambda row: (float(row.get("when_ts") or 0.0), str(row.get("summary") or "")), reverse=True)
    return entries[:safe_limit]


def normalize_addon_data(v: Any) -> list[dict[str, Any]]:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for key in ("containers", "docker", "items"):
            candidate = v.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def integration_summary_counts(vm_data: list[dict[str, Any]]) -> Dict[str, int]:
    counts = {"running": 0, "stopped": 0, "paused": 0, "other": 0}
    for vm in vm_data:
        if not isinstance(vm, dict):
            continue
        key, _label = classify_integration_state(vm.get("state"))
        if key not in counts:
            key = "other"
        counts[key] += 1
    return counts


def _read_temp_millic(path: str) -> Optional[float]:
    try:
        v = float(_read_first_line(path))
    except Exception:
        return None
    if v > 1000.0:
        return v / 1000.0
    if -50.0 <= v <= 150.0:
        return v
    return None


def get_cpu_temp_c(sensor_hint: Optional[str] = None) -> Optional[float]:
    hint = (sensor_hint or "").strip().lower()

    # 1. Try /sys/class/thermal (Linux fallback)
    try:
        thermal_dir = "/sys/class/thermal"
        if os.path.isdir(thermal_dir):
            zones = sorted([p for p in os.listdir(thermal_dir) if p.startswith("thermal_zone")])

            # If hint is a direct path or zone name
            if hint and (hint.startswith('/sys/') or hint.startswith('thermal_zone')):
                path = hint if hint.startswith('/') else os.path.join(thermal_dir, hint)
                temp = _read_temp_millic(os.path.join(path, 'temp'))
                if temp is not None:
                    return temp

            # Try to find a processor related typed zone
            for tz in zones:
                tpath = os.path.join(thermal_dir, tz, 'type')
                vpath = os.path.join(thermal_dir, tz, 'temp')
                try:
                    ttype = _read_first_line(tpath).lower()
                except Exception:
                    ttype = ""
                temp = _read_temp_millic(vpath)
                if temp is None:
                    continue
                if any(x in ttype for x in ("cpu", "pkg", "package", "x86_pkg", "soc", "composite")):
                    return temp

            # Final fallback: first available zone
            for tz in zones:
                temp = _read_temp_millic(os.path.join(thermal_dir, tz, 'temp'))
                if temp is not None:
                    return temp
    except Exception:
        pass

    return None



def get_fan_rpm(sensor_hint: Optional[str] = None) -> Optional[float]:
    hint = (sensor_hint or "").strip().lower()
    try:
        for hw in sorted(os.listdir('/sys/class/hwmon')):
            base = f'/sys/class/hwmon/{hw}'
            if hint.startswith(base.lower() + '/fan') and hint.endswith('_input'):
                v = safe_float(_read_first_line(hint), None)
                if v is not None and v >= 0:
                    return float(v)
            for name in sorted(os.listdir(base)):
                if not re.match(r'fan\d+_input$', name):
                    continue
                v = safe_float(_read_first_line(f'{base}/{name}'), None)
                if v is not None and v >= 0:
                    return float(v)
    except Exception:
        pass
    return None


def get_disk_usage_pct(disk_hint: Optional[str] = None, active_disk: Optional[str] = None) -> float:
    try:
        st = os.statvfs('/')
        total = float(st.f_blocks) * float(st.f_frsize)

        avail = float(st.f_bavail) * float(st.f_frsize)
        used = max(0.0, total - avail)
        if total > 0:
            return (used * 100.0) / total
    except Exception:
        pass
    return 0.0


def addon_summary_counts(docker_data: list[dict[str, Any]]) -> Dict[str, int]:
    counts = {"running": 0, "stopped": 0, "unhealthy": 0}
    for c in docker_data:
        if not isinstance(c, dict):
            continue
        state_raw = str(c.get('State') or c.get('state') or '').lower()
        status_raw = str(c.get('Status') or c.get('status') or '').lower()
        combined = f'{state_raw} {status_raw}'
        is_running = ('running' in combined) or (' up ' in f' {combined} ')
        if is_running:
            counts['running'] += 1
        else:
            counts['stopped'] += 1
        if 'unhealthy' in combined:
            counts['unhealthy'] += 1
    return counts


def get_gpu_metrics(timeout: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {"temp_c": 0.0, "util_pct": 0.0, "mem_pct": 0.0, "available": False}
    try:
        p = subprocess.run(
            [
                'nvidia-smi',
                '--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total',
                '--format=csv,noheader,nounits',
            ],
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout)),
            check=False,
        )
        if p.returncode == 0 and p.stdout:
            temps: list[float] = []
            utils: list[float] = []
            mem_pcts: list[float] = []
            for line in p.stdout.splitlines():
                parts = [x.strip() for x in line.split(',')]
                if len(parts) < 4:
                    continue
                t = safe_float(parts[0], None)
                u = safe_float(parts[1], None)
                mu = safe_float(parts[2], None)
                mt = safe_float(parts[3], None)
                if t is not None and -20.0 <= t <= 150.0:
                    temps.append(float(t))
                if u is not None and 0.0 <= u <= 100.0:
                    utils.append(float(u))
                if mu is not None and mt and mt > 0:
                    mem_pcts.append(max(0.0, min(100.0, (float(mu) * 100.0) / float(mt))))
            if temps:
                out['temp_c'] = max(temps)
            if utils:
                out['util_pct'] = max(utils)
            if mem_pcts:
                out['mem_pct'] = max(mem_pcts)
            if temps or utils or mem_pcts:
                out['available'] = True
    except Exception:
        pass
    return out


def _extract_temp_from_text(text: str) -> Optional[float]:
    for line in text.splitlines():
        ll = line.lower()
        if "temperature" not in ll and "composite" not in ll:
            continue
        nums = re.findall(r"-?\d+(?:\.\d+)?", line)
        for n in nums:
            v = safe_float(n, None)
            if v is None:
                continue
            if -20.0 <= v <= 150.0:
                return float(v)
    return None


def _normalize_disk_name(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.startswith("/dev/"):
        s = s[5:]
    if s.startswith("nvme") and "p" in s:
        s = re.sub(r"p\d+$", "", s)
    s = re.sub(r"\d+$", "", s)
    return s


def _disk_candidates(device_hint: Optional[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            out.append(path)

    hint_name = _normalize_disk_name(device_hint)
    if hint_name:
        add(f"/dev/{hint_name}")
    for d in ["/dev/nvme0", "/dev/nvme0n1", "/dev/sda"]:
        add(d)
    return out


def get_disk_temp_c(timeout: float, disk_device: Optional[str] = None) -> Optional[float]:
    hint_name = _normalize_disk_name(disk_device)
    for dev in _disk_candidates(disk_device):
        for cmd in (["nvme", "smart-log", dev], ["smartctl", "-A", dev]):
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            except Exception:
                continue
            text = (p.stdout or "") + "\n" + (p.stderr or "")
            t = _extract_temp_from_text(text)
            if t is not None:
                return t
    return None


def _read_diskstats() -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    try:
        with open("/proc/diskstats", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return out

    for line in lines:
        cols = line.split()
        if len(cols) < 14:
            continue
        name = cols[2]
        if re.search(r"\d+$", name) and not name.startswith("nvme"):
            continue
        if name.startswith(("loop", "ram", "dm-", "sr", "zram", "md")):
            continue
        if name.startswith("nvme") and re.search(r"p\d+$", name):
            continue
        try:
            sectors_read = float(cols[5])
            sectors_written = float(cols[9])
        except Exception:
            continue
        out[name] = (sectors_read * 512.0, sectors_written * 512.0)
    return out


def get_disk_bytes_local(disk_hint: Optional[str] = None, last_disk: Optional[str] = None) -> tuple[float, float, Optional[str]]:
    stats = _read_diskstats()
    if not stats:
        return 0.0, 0.0, None

    hint_name = _normalize_disk_name(disk_hint)
    if hint_name:
        if hint_name in stats:
            rb, wb = stats[hint_name]
            return rb, wb, hint_name
        for name in stats:
            if name.startswith(hint_name):
                rb, wb = stats[name]
                return rb, wb, name

    if last_disk and last_disk in stats:
        rb, wb = stats[last_disk]
        return rb, wb, last_disk

    for name in sorted(stats.keys()):
        if name.startswith(("nvme", "sd", "vd", "xvd", "mmcblk", "disk")):
            rb, wb = stats[name]
            return rb, wb, name

    name = next(iter(stats.keys()))
    rb, wb = stats[name]
    return rb, wb, name


def get_docker_containers_from_engine(socket_path: str, timeout: float) -> Any:
    class UnixHTTPConnection(http.client.HTTPConnection):
        def __init__(self, unix_socket_path: str, timeout_s: float):
            super().__init__("localhost", timeout=timeout_s)
            self.unix_socket_path = unix_socket_path

        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.unix_socket_path)

    conn = UnixHTTPConnection(socket_path, timeout)
    try:
        conn.request("GET", "/containers/json?all=1")
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Docker API HTTP {resp.status}: {body[:200]!r}")
        return json.loads(body.decode("utf-8", errors="ignore"))
    finally:
        conn.close()


def _run_command_capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=max(1.0, float(timeout)),
        check=False,
    )


def _virsh_cmd(virsh_binary: str, virsh_uri: Optional[str], *parts: str) -> list[str]:
    argv = [virsh_binary or "virsh"]
    if virsh_uri:
        argv.extend(["-c", virsh_uri])
    argv.extend(parts)
    return argv


def _virsh_uri_candidates(virsh_uri: Optional[str]) -> list[Optional[str]]:
    if virsh_uri:
        return [virsh_uri]
    out: list[Optional[str]] = []
    seen: set[str] = set()
    for candidate in (None, "qemu:///system", "qemu:///session"):
        key = candidate or ""
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _parse_virsh_mem_mib(v: Any) -> int:
    text = str(v or "").strip()
    if not text:
        return 0
    nums = re.findall(r"-?\d+(?:\.\d+)?", text)
    if not nums:
        return 0
    value = safe_float(nums[0], 0.0) or 0.0
    ll = text.lower()
    if "gib" in ll or "gb" in ll:
        return max(0, int(round(value * 1024.0)))
    if "mib" in ll or "mb" in ll:
        return max(0, int(round(value)))
    if "kib" in ll or "kb" in ll:
        return max(0, int(round(value / 1024.0)))
    return max(0, int(round(value / 1024.0)))


def _parse_virsh_dominfo(text: str) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        k = key.strip().lower()
        v = value.strip()
        info[k] = v
    name = str(info.get("name") or "").strip()
    state = str(info.get("state") or "").strip()
    vcpus = max(0, safe_int(info.get("cpu(s)"), 0) or 0)
    max_mem_mib = _parse_virsh_mem_mib(info.get("max memory"))
    used_mem_mib = _parse_virsh_mem_mib(info.get("used memory"))
    autostart = str(info.get("autostart") or "").strip().lower() in {"enable", "enabled", "yes"}
    persistent = str(info.get("persistent") or "").strip().lower() in {"yes", "true"}
    dom_id = str(info.get("id") or "-").strip()
    return {
        "name": name,
        "state": state,
        "vcpus": vcpus,
        "max_mem_mib": max_mem_mib,
        "used_mem_mib": used_mem_mib,
        "autostart": autostart,
        "persistent": persistent,
        "id": dom_id,
    }


def get_virtual_machines_from_virsh(virsh_binary: str, virsh_uri: Optional[str], timeout: float) -> list[dict[str, Any]]:
    names: list[str] = []
    chosen_uri = virsh_uri
    errors: list[str] = []
    had_empty_success = False
    for candidate_uri in _virsh_uri_candidates(virsh_uri):
        base = _virsh_cmd(virsh_binary, candidate_uri, "list", "--all", "--name")
        p = _run_command_capture(base, timeout)
        if p.returncode != 0:
            errors.append((p.stderr or p.stdout or f"virsh list failed ({p.returncode})").strip())
            continue
        names = [line.strip() for line in (p.stdout or "").splitlines() if line.strip()]
        chosen_uri = candidate_uri
        if names:
            break
        had_empty_success = True
        if virsh_uri:
            return []
    else:
        if had_empty_success:
            return []
        raise RuntimeError("; ".join([e for e in errors if e][:3]) or "virsh list failed")

    if not names:
        return []

    out: list[dict[str, Any]] = []
    for name in names:
        dominfo_cmd = _virsh_cmd(virsh_binary, chosen_uri, "dominfo", name)
        try:
            info_p = _run_command_capture(dominfo_cmd, timeout)
        except Exception as e:
            logging.warning("virsh dominfo failed for %s (%s)", name, e)
            out.append({"name": name, "state": "unknown", "vcpus": 0, "max_mem_mib": 0, "used_mem_mib": 0})
            continue
        if info_p.returncode != 0:
            logging.warning(
                "virsh dominfo failed for %s (rc=%s: %s)",
                name,
                info_p.returncode,
                (info_p.stderr or info_p.stdout or "").strip()[:160],
            )
            out.append({"name": name, "state": "unknown", "vcpus": 0, "max_mem_mib": 0, "used_mem_mib": 0})
            continue
        item = _parse_virsh_dominfo(info_p.stdout or "")
        if not item.get("name"):
            item["name"] = name
        out.append(item)
    return out


def get_available_ports() -> list[str]:
    if list_ports is None:
        return []
    try:
        return sorted(p.device for p in list_ports.comports())
    except Exception:
        return []


def list_serial_port_choices() -> list[str]:
    choices: list[str] = []
    seen: set[str] = set()

    def _add(path: Optional[str]) -> None:
        if not path:
            return
        p = str(path).strip()
        if not p or p in seen:
            return
        seen.add(p)
        choices.append(p)

    # Prefer stable Linux/Unraid symlinks first when present.
    by_id_dir = Path('/dev/serial/by-id')
    try:
        if by_id_dir.is_dir():
            for item in sorted(by_id_dir.iterdir(), key=lambda x: x.name.lower()):
                _add(str(item))
    except Exception:
        pass

    for port in get_available_ports():
        _add(port)

    return choices


def test_serial_open(port: Optional[str], baud: int) -> tuple[bool, str]:
    if serial is None:
        return False, 'pyserial is not installed. Install with: pip install pyserial'

    p = (port or '').strip()
    if not p:
        return False, 'serial port is required'

    try:
        baud_i = int(baud)
    except Exception:
        return False, 'invalid baud rate'
    if baud_i <= 0:
        return False, 'invalid baud rate'

    try:
        s = serial.Serial(p, baud_i, timeout=1, write_timeout=2)
        try:
            s.dtr = False
            s.rts = False
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
        return True, f'opened {p} @ {baud_i}'
    except Exception as e:
        return False, f'failed to open {p}: {e}'


def list_network_interface_choices() -> list[str]:
    try:
        stats = _parse_proc_net_dev()
    except Exception:
        stats = {}
    names = [str(k) for k in stats.keys()]
    names = sorted(set(names), key=lambda x: (x.lower() in {"lo", "loopback", "lo0"}, x.lower()))
    return names


def list_disk_device_choices() -> list[str]:
    choices: list[str] = []
    seen: set[str] = set()

    def _add(v: Optional[str]) -> None:
        if not v:
            return
        x = str(v).strip()
        if not x or x in seen:
            return
        seen.add(x)
        choices.append(x)

    try:
        for name in sorted(os.listdir('/sys/block')):
            if name.startswith(('loop', 'ram', 'zram', 'dm-')):
                continue
            _add(f'/dev/{name}')
    except Exception:
        pass

    return choices


def list_cpu_temp_sensor_choices() -> list[str]:
    choices: list[str] = []
    seen: set[str] = set()

    def _add(v: Optional[str]) -> None:
        if not v:
            return
        x = str(v).strip()
        if not x or x in seen:
            return
        seen.add(x)
        choices.append(x)

    try:
        for tz in sorted([p for p in os.listdir('/sys/class/thermal') if p.startswith('thermal_zone')]):
            tpath = f'/sys/class/thermal/{tz}/type'
            try:
                ttype = _read_first_line(tpath).strip().lower()
            except Exception:
                ttype = ''
            if any(x in ttype for x in ('cpu', 'core', 'pkg', 'package', 'soc')):
                _add(f'/sys/class/thermal/{tz}')
    except Exception:
        pass

    return choices


def list_fan_sensor_choices() -> list[str]:
    choices: list[str] = []
    seen: set[str] = set()

    def _add(v: Optional[str]) -> None:
        if not v:
            return
        x = str(v).strip()
        if not x or x in seen:
            return
        seen.add(x)
        choices.append(x)

    try:
        for hw in sorted(os.listdir('/sys/class/hwmon')):
            base = f'/sys/class/hwmon/{hw}'
            for name in sorted(os.listdir(base)):
                if re.match(r'fan\d+_input$', name):
                    _add(f'{base}/{name}')
    except Exception:
        pass

    return choices


def detect_hardware_choices() -> dict[str, Any]:
    return {
        'serial_ports': list_serial_port_choices(),
        'network_ifaces': list_network_interface_choices(),
        'disk_devices': list_disk_device_choices(),
        'cpu_temp_sensors': list_cpu_temp_sensor_choices(),
        'fan_sensors': list_fan_sensor_choices(),
    }


def _safe_realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def pick_serial_port(requested: Optional[str], last_port: Optional[str] = None) -> Optional[str]:
    available = get_available_ports()
    if requested:
        req = requested.strip()
        if not req:
            requested = None
        else:
            req_abs = req if req.startswith("/dev/") or re.match(r"^[A-Za-z]+\d+$", req) else f"/dev/{req}"
            req_real = _safe_realpath(req_abs)
            for p in available:
                p_real = _safe_realpath(p)
                if req == p or req_abs == p or req_real == p_real:
                    return p
            if os.path.exists(req_abs):
                for p in available:
                    if _safe_realpath(p) == req_real:
                        return p
            logging.warning("serial port not found: %s", requested)
            if available:
                logging.warning("available ports:")
                for p in available:
                    logging.warning("  - %s", p)
            else:
                logging.warning("no serial ports detected.")
            return None

    if last_port and last_port in available:
        return last_port

    for p in available:
        if p.startswith("/dev/ttyACM"):
            return p
    for p in available:
        if p.startswith("/dev/ttyUSB"):
            return p
    for p in ("/dev/ttyAMA0", "/dev/serial0", "/dev/ttyS0"):
        if p in available:
            return p
    for p in available:
        if p.startswith("/dev/cu.usbmodem"):
            return p
    for p in available:
        if p.startswith("/dev/cu.usb"):
            return p
    for p in available:
        if p.startswith("/dev/tty.usb"):
            return p
    for p in available:
        if p.upper().startswith("COM"):
            return p
    return available[0] if available else None


def open_serial(requested_port: Optional[str], baud: int, last_port: Optional[str] = None):
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install with: pip install pyserial")
    while True:
        s, serial_port = try_open_serial_once(requested_port, baud, last_port=last_port)
        if s is not None:
            return s, serial_port
        time.sleep(SERIAL_RETRY_SECONDS)


def try_open_serial_once(requested_port: Optional[str], baud: int, last_port: Optional[str] = None):
    if serial is None:
        raise RuntimeError("pyserial is not installed. Install with: pip install pyserial")
    serial_port = pick_serial_port(requested_port, last_port=last_port)
    if serial_port is None:
        logging.warning("no serial port available, retrying in %ss", SERIAL_RETRY_SECONDS)
        return None, last_port
    try:
        s = serial.Serial(serial_port, baud, timeout=1, write_timeout=2)
        try:
            s.dtr = False
            s.rts = False
        except Exception:
            pass
        logging.info("serial connected: %s @ %s", serial_port, baud)
        return s, serial_port
    except Exception as e:
        logging.warning("serial open failed on %s (%s), retrying in %ss", serial_port, e, SERIAL_RETRY_SECONDS)
        return None, last_port


def detect_host_power_command_defaults() -> Dict[str, str]:
    if is_home_assistant_app_mode():
        return {
            "os": "homeassistant",
            "shutdown_cmd": "Supervisor API /host/shutdown",
            "restart_cmd": "Supervisor API /host/reboot",
        }
    system = platform.system().lower()
    if system == "linux":
        return {
            "os": system,
            "shutdown_cmd": "systemctl poweroff",
            "restart_cmd": "systemctl reboot",
        }
    if system == "darwin":
        return {
            "os": system,
            "shutdown_cmd": "/sbin/shutdown -h now",
            "restart_cmd": "/sbin/shutdown -r now",
        }
    if system == "windows":
        return {
            "os": system,
            "shutdown_cmd": "shutdown /s /t 0",
            "restart_cmd": "shutdown /r /t 0",
        }
    return {"os": system or "unknown", "shutdown_cmd": "", "restart_cmd": ""}




def resolve_host_command_argv(
    cmd: str,
    use_sudo: bool = False,
    shutdown_cmd: Optional[str] = None,
    restart_cmd: Optional[str] = None,
) -> tuple[Optional[list[str]], Optional[str]]:
    cmd_s = (cmd or "").strip()
    cmd_l = cmd_s.lower()
    system = platform.system().lower()

    custom_cmd = ""
    if cmd_l in ("shutdown",):
        custom_cmd = (shutdown_cmd or "").strip()
    elif cmd_l in ("restart", "reboot"):
        custom_cmd = (restart_cmd or "").strip()

    argv: Optional[list[str]] = None
    if custom_cmd:
        try:
            argv = shlex.split(custom_cmd, posix=(os.name != "nt"))
        except Exception as e:
            return None, f"invalid custom host command for CMD={cmd_s} ({e})"
        if not argv:
            return None, f"custom host command is empty for CMD={cmd_s}"
    else:
        if system == "linux":
            if cmd_l in ("shutdown",):
                argv = ["/usr/bin/systemctl", "poweroff"]
            elif cmd_l in ("restart", "reboot"):
                argv = ["/usr/bin/systemctl", "reboot"]
        elif system == "darwin":
            if cmd_l in ("shutdown",):
                argv = ["/sbin/shutdown", "-h", "now"]
            elif cmd_l in ("restart", "reboot"):
                argv = ["/sbin/shutdown", "-r", "now"]
        elif system == "windows":
            if cmd_l in ("shutdown",):
                argv = ["shutdown", "/s", "/t", "0"]
            elif cmd_l in ("restart", "reboot"):
                argv = ["shutdown", "/r", "/t", "0"]

    if argv is None:
        return None, f"unsupported or unknown CMD={cmd_s}"

    if use_sudo and system in {"linux", "darwin"} and argv and argv[0] != "sudo":
        argv = ["sudo"] + argv
    return argv, None


def execute_host_command(
    cmd: str,
    use_sudo: bool = False,
    shutdown_cmd: Optional[str] = None,
    restart_cmd: Optional[str] = None,
) -> None:
    cmd_s = (cmd or "").strip()
    argv, err = resolve_host_command_argv(
        cmd_s,
        use_sudo=use_sudo,
        shutdown_cmd=shutdown_cmd,
        restart_cmd=restart_cmd,
    )
    if argv is None:
        logging.warning(err or "ignoring unsupported or unknown CMD=%s", cmd_s)
        return
    logging.info("executing host command: %s", " ".join(shlex.quote(x) for x in argv))
    subprocess.run(argv, check=False)


def resolve_home_assistant_host_power_target(cmd: str) -> tuple[Optional[str], Optional[str]]:
    cmd_s = (cmd or "").strip()
    cmd_l = cmd_s.lower()
    if cmd_l == "shutdown":
        return "/host/shutdown", None
    if cmd_l in ("restart", "reboot"):
        return "/host/reboot", None
    return None, f"unsupported or unknown CMD={cmd_s}"


def execute_home_assistant_host_power_command(cmd: str, timeout: float) -> bool:
    cmd_s = (cmd or "").strip()
    path, err = resolve_home_assistant_host_power_target(cmd_s)
    if not path:
        logging.warning(err or "ignoring unsupported or unknown CMD=%s", cmd_s)
        return False
    try:
        _supervisor_request_json(path, timeout=timeout, method="POST", payload={})
        logging.info("home assistant host power command requested: %s via %s", cmd_s.lower(), path)
    except Exception as e:
        logging.warning("home assistant host power command failed for %s (%s)", cmd_s, e)
    return True


def execute_docker_command(cmd: str, socket_path: str, timeout: float) -> bool:
    cmd_s = (cmd or "").strip()
    cmd_l = cmd_s.lower()
    if cmd_l.startswith("docker_start:"):
        action = "start"
        target = cmd_s.split(":", 1)[1].strip()
    elif cmd_l.startswith("docker_stop:"):
        action = "stop"
        target = cmd_s.split(":", 1)[1].strip()
    else:
        return False

    if not target:
        logging.warning("ignoring docker command with empty target (CMD=%s)", cmd_s)
        return True

    class UnixHTTPConnection(http.client.HTTPConnection):
        def __init__(self, unix_socket_path: str, timeout_s: float):
            super().__init__("localhost", timeout=timeout_s)
            self.unix_socket_path = unix_socket_path

        def connect(self) -> None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.unix_socket_path)

    encoded = urllib.parse.quote(target, safe="")
    path = f"/containers/{encoded}/{action}" + ("?t=10" if action == "stop" else "")
    try:
        conn = UnixHTTPConnection(socket_path, timeout)
        conn.request("POST", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status in (204, 304):
            logging.info("docker %s requested for %s (HTTP %s)", action, target, resp.status)
        else:
            logging.warning(
                "docker %s failed for %s via %s (HTTP %s: %r)",
                action,
                target,
                socket_path,
                resp.status,
                body[:200],
            )
    except Exception as e:
        logging.warning("docker %s failed for %s via %s (%s)", action, target, socket_path, e)
    return True


def execute_home_assistant_addon_command(cmd: str, timeout: float) -> bool:
    cmd_s = (cmd or "").strip()
    cmd_l = cmd_s.lower()
    if cmd_l.startswith("docker_start:"):
        action = "start"
        target = cmd_s.split(":", 1)[1].strip()
    elif cmd_l.startswith("docker_stop:"):
        action = "stop"
        target = cmd_s.split(":", 1)[1].strip()
    else:
        return False
    if not target:
        logging.warning("ignoring add-on command with empty target (CMD=%s)", cmd_s)
        return True
    addons = get_home_assistant_addons(timeout)
    target_l = target.lower()
    match = next(
        (
            row for row in addons
            if str(row.get("name") or "") == target
            or str(row.get("slug") or "") == target
            or str(row.get("name") or "").lower().startswith(target_l)
            or str(row.get("slug") or "").lower().startswith(target_l)
        ),
        None,
    )
    if not match:
        logging.warning("home assistant add-on command target not found (%s)", target)
        return True
    slug = str(match.get("slug") or "").strip()
    if not slug:
        logging.warning("home assistant add-on slug missing for %s", target)
        return True
    try:
        _supervisor_request_json(f"/addons/{urllib.parse.quote(slug, safe='')}/{action}", timeout=timeout, method="POST", payload={})
        logging.info("home assistant add-on %s requested for %s", action, target)
    except Exception as e:
        logging.warning("home assistant add-on %s failed for %s (%s)", action, target, e)
    return True


def execute_virsh_command(cmd: str, virsh_binary: str, virsh_uri: Optional[str], timeout: float) -> bool:
    cmd_s = (cmd or "").strip()
    cmd_l = cmd_s.lower()
    if cmd_l.startswith("vm_start:"):
        action = "start"
        target = cmd_s.split(":", 1)[1].strip()
        parts = ("start", target)
    elif cmd_l.startswith("vm_force_stop:"):
        action = "destroy"
        target = cmd_s.split(":", 1)[1].strip()
        parts = ("destroy", target)
    elif cmd_l.startswith("vm_stop:"):
        action = "shutdown"
        target = cmd_s.split(":", 1)[1].strip()
        parts = ("shutdown", target)
    elif cmd_l.startswith("vm_restart:"):
        action = "reboot"
        target = cmd_s.split(":", 1)[1].strip()
        parts = ("reboot", target)
    else:
        return False

    if not target:
        logging.warning("ignoring VM command with empty target (CMD=%s)", cmd_s)
        return True

    errors: list[str] = []
    for candidate_uri in _virsh_uri_candidates(virsh_uri):
        argv = _virsh_cmd(virsh_binary, candidate_uri, *parts)
        try:
            p = _run_command_capture(argv, timeout)
            if p.returncode == 0:
                logging.info("vm %s requested for %s", action, target)
                return True
            errors.append((p.stderr or p.stdout or "").strip()[:200])
        except Exception as e:
            errors.append(str(e))
    logging.warning(
        "vm %s failed for %s (%s)",
        action,
        target,
        "; ".join([e for e in errors if e][:3]) or "unknown error",
    )
    return True


def command_to_power_state(cmd: str) -> Optional[str]:
    cmd_l = (cmd or "").strip().lower()
    if cmd_l == "shutdown":
        return "SHUTTING_DOWN"
    if cmd_l in ("restart", "reboot"):
        return "RESTARTING"
    return None


def process_usb_commands(
    ser: Any,
    rx_buf: str,
    allow_host_cmds: bool = False,
    homeassistant_mode: bool = False,
    host_cmd_use_sudo: bool = False,
    docker_socket: str = "/var/run/docker.sock",
    virsh_binary: str = "virsh",
    virsh_uri: Optional[str] = None,
    timeout: float = 2.0,
    shutdown_cmd: Optional[str] = None,
    restart_cmd: Optional[str] = None,
) -> str:
    try:
        n = ser.in_waiting
    except Exception:
        n = 0
    if n <= 0:
        return rx_buf

    raw = ser.read(n)
    if not raw:
        return rx_buf

    rx_buf += raw.decode("utf-8", errors="ignore")
    while True:
        nl = rx_buf.find("\n")
        if nl < 0:
            break
        line = rx_buf[:nl].strip("\r").strip()
        rx_buf = rx_buf[nl + 1 :]
        if not line:
            continue
        logging.info("usb_rx: %s", line)
        if not line.startswith("CMD="):
            continue
        cmd = line.split("=", 1)[1].strip()
        if allow_host_cmds:
            if homeassistant_mode and execute_home_assistant_addon_command(cmd, timeout):
                continue
            if execute_docker_command(cmd, docker_socket, timeout):
                continue
            if execute_virsh_command(cmd, virsh_binary, virsh_uri, timeout):
                continue
            power_state = command_to_power_state(cmd)
            if power_state:
                try:
                    ack = f"POWER={power_state}\n"
                    ser.write(ack.encode("utf-8", errors="ignore"))
                    ser.flush()
                    logging.info("usb_tx: %s", ack.strip())
                    time.sleep(0.15)
                except Exception as e:
                    logging.warning("failed to send power state to device (%s)", e)
            if homeassistant_mode and power_state:
                execute_home_assistant_host_power_command(cmd, timeout)
                continue
            execute_host_command(
                cmd,
                use_sudo=host_cmd_use_sudo,
                shutdown_cmd=shutdown_cmd,
                restart_cmd=restart_cmd,
            )
        else:
            logging.info("host command received but disabled (CMD=%s)", cmd)

    if len(rx_buf) > RX_BUFFER_MAX_BYTES:
        rx_buf = rx_buf[-RX_BUFFER_KEEP_BYTES:]
    return rx_buf


def build_status_line(args: argparse.Namespace, state: RuntimeState) -> str:
    now = time.time()
    homeassistant_mode = is_home_assistant_app_mode()
    state.ha_token_present = bool(SUPERVISOR_TOKEN)

    # 1. Fetch HA Host Info periodically (every 60s)
    if homeassistant_mode and SUPERVISOR_TOKEN and (now - state.last_ha_host_info_ts) > 60.0:
        try:
            res = _supervisor_request_json("/host/info", timeout=args.timeout)
            if isinstance(res, dict):
                state.ha_host_info = res
                state.last_ha_host_info_ts = now
        except Exception:
            pass

    # 2. Fetch metrics (Home Assistant Proxy Mode)
    # If HA entities are provided, we pull from them to allow "Green" security rating (no full_access needed)
    def _ha_get(key):
        eid = getattr(args, key, "")
        if homeassistant_mode and eid and eid.strip():
            return get_home_assistant_state(eid, timeout=args.timeout)
        return None, {}

    def _ha_get_conv(key, target_kb):
        eid = getattr(args, key, "")
        if homeassistant_mode and eid and eid.strip():
            return get_home_assistant_metric_converted(eid, timeout=args.timeout, target_kb=target_kb)
        return None

    ha_cpu, _ = _ha_get('ha_entity_cpu')
    ha_mem, _ = _ha_get('ha_entity_mem')
    ha_temp, _ = _ha_get('ha_entity_temp')
    ha_disk_pct, _ = _ha_get('ha_entity_disk_pct')
    ha_fan, _ = _ha_get('ha_entity_fan')
    ha_disk_temp, _ = _ha_get('ha_entity_disk_temp')
    ha_uptime, _ = _ha_get('ha_entity_uptime')
    ha_gpu_util, _ = _ha_get('ha_entity_gpu_util')
    ha_gpu_temp, _ = _ha_get('ha_entity_gpu_temp')
    ha_gpu_vram, _ = _ha_get('ha_entity_gpu_vram')

    # Fallbacks from host/info
    ha_info = state.ha_host_info
    if homeassistant_mode and ha_info:
        # Use boot_timestamp if entity is missing
        if ha_uptime is None and "boot_timestamp" in ha_info:
            try:
                # boot_timestamp is usually in microseconds
                ha_uptime = float(ha_info["boot_timestamp"]) / 1000000.0
            except Exception:
                pass
        
        # Use disk stats if entity is missing
        if ha_disk_pct is None and "disk_total" in ha_info and "disk_used" in ha_info:
            try:
                total = float(ha_info["disk_total"])
                used = float(ha_info["disk_used"])
                if total > 0:
                    ha_disk_pct = str(round((used / total) * 100.0, 1))
            except Exception:
                pass

    # Throughput metrics need unit conversion
    ha_net_rx_kbps = _ha_get_conv('ha_entity_net_rx', target_kb=True)
    ha_net_tx_kbps = _ha_get_conv('ha_entity_net_tx', target_kb=True)
    ha_disk_read_kbs = _ha_get_conv('ha_entity_disk_read', target_kb=False)
    ha_disk_write_kbs = _ha_get_conv('ha_entity_disk_write', target_kb=False)

    # CPU
    cpu_available = True
    if ha_cpu is not None:
        cpu_pct = safe_float(ha_cpu, 0.0)
    else:
        cpu_pct = 0.0
        cpu_available = False

    # MEM
    mem_available = True
    if ha_mem is not None:
        mem_pct = safe_float(ha_mem, 0.0)
    else:
        mem_pct = 0.0
        mem_available = False

    # TEMP
    if ha_temp is not None:
        cpu_temp_sample = safe_float(ha_temp, None)
    elif homeassistant_mode:
        cpu_temp_sample = None
    else:
        cpu_temp_sample = get_cpu_temp_c(getattr(args, 'cpu_temp_sensor', None))

    uptime_available = True
    uptime_s = 0.0
    if ha_uptime is not None:
        # Check if it's a numeric timestamp (boot_timestamp from host/info or float string)
        # or an ISO timestamp (sensor.last_boot)
        try:
            from datetime import datetime
            ha_uptime_str = str(ha_uptime)
            if "T" in ha_uptime_str:
                boot_dt = datetime.fromisoformat(ha_uptime_str.replace("Z", "+00:00"))
                uptime_s = max(0.0, time.time() - boot_dt.timestamp())
            else:
                ts = float(ha_uptime)
                if ts > 1000000000: # It's a boot timestamp
                    uptime_s = max(0.0, time.time() - ts)
                else: # It's an uptime duration in seconds
                    uptime_s = ts
        except Exception:
            uptime_s = safe_float(ha_uptime, uptime_s)
    else:
        uptime_available = False

    cpu_temp_available = cpu_temp_sample is not None
    cpu_temp = float(cpu_temp_sample or 0.0)
    if (now - state.last_disk_temp_ts) >= DISK_TEMP_REFRESH_SECONDS:
        if ha_disk_temp is not None:
            disk_temp_sample = safe_float(ha_disk_temp, None)
        elif homeassistant_mode:
            disk_temp_sample = None
        else:
            disk_temp_sample = get_disk_temp_c(args.timeout, args.disk_temp_device or args.disk_device)
        state.disk_temp_c = float(disk_temp_sample or 0.0)
        state.disk_temp_available = disk_temp_sample is not None
        state.last_disk_temp_ts = now
    disk_temp_available = bool(getattr(state, "disk_temp_available", False))
    disk_usage_available = True
    if (now - state.last_disk_usage_ts) >= DISK_USAGE_REFRESH_SECONDS:
        if ha_disk_pct is not None:
            state.disk_usage_pct = safe_float(ha_disk_pct, 0.0)
        elif homeassistant_mode:
            state.disk_usage_pct = 0.0
            disk_usage_available = False
        else:
            state.disk_usage_pct = get_disk_usage_pct(args.disk_device, state.active_disk)
        state.last_disk_usage_ts = now
    elif homeassistant_mode and not ha_disk_pct:
        disk_usage_available = False
    gpu_enabled = not bool(getattr(args, "disable_gpu_polling", False))
    if (now - state.last_slow_sensor_ts) >= SLOW_SENSOR_REFRESH_SECONDS:
        if ha_fan is not None:
            fan_rpm_sample = safe_float(ha_fan, 0.0)
        elif homeassistant_mode:
            fan_rpm_sample = None
        else:
            fan_rpm_sample = get_fan_rpm(getattr(args, 'fan_sensor', None))
        state.fan_rpm = float(fan_rpm_sample or 0.0)
        state.fan_available = fan_rpm_sample is not None
        if gpu_enabled:
            if homeassistant_mode:
                state.gpu_util_pct = safe_float(ha_gpu_util, 0.0)
                state.gpu_temp_c = safe_float(ha_gpu_temp, 0.0)
                state.gpu_mem_pct = safe_float(ha_gpu_vram, 0.0)
                state.gpu_available = any(x is not None for x in [ha_gpu_util, ha_gpu_temp, ha_gpu_vram])
            else:
                gpu = get_gpu_metrics(args.timeout)
                state.gpu_temp_c = float(gpu.get('temp_c', 0.0) or 0.0)
                state.gpu_util_pct = float(gpu.get('util_pct', 0.0) or 0.0)
                state.gpu_mem_pct = float(gpu.get('mem_pct', 0.0) or 0.0)
                state.gpu_available = bool(gpu.get('available', False))
        else:
            state.gpu_temp_c = 0.0
            state.gpu_util_pct = 0.0
            state.gpu_mem_pct = 0.0
            state.gpu_available = False
        state.last_slow_sensor_ts = now
    fan_available = bool(getattr(state, "fan_available", False))
    gpu_available = bool(getattr(state, "gpu_available", False))

    addons_enabled = not bool(getattr(args, "disable_docker_polling", False))
    addons_interval = max(0.0, float(getattr(args, "docker_interval", 2.0) or 0.0))
    if addons_enabled and addons_interval > 0.0 and (not state.last_addons_refresh_ts or (now - state.last_addons_refresh_ts) >= addons_interval):
        try:
            if homeassistant_mode:
                addons = get_home_assistant_addons(timeout=args.timeout)
            else:
                addons = get_docker_containers_from_engine(args.docker_socket, timeout=args.timeout)
            state.ha_addons_api_ok = True if homeassistant_mode else None
        except Exception as e:
            addons = []
            state.ha_addons_api_ok = False if homeassistant_mode else None
            if (now - state.last_addons_warn_ts) >= DOCKER_WARN_INTERVAL_SECONDS:
                if homeassistant_mode:
                    logging.warning("Home Assistant add-on API unavailable; continuing without add-on data (%s)", e)
                else:
                    logging.warning(
                        "Docker API unavailable via %s; continuing without docker data (%s)",
                        args.docker_socket,
                        e,
                    )
                state.last_addons_warn_ts = now
        addons = normalize_addon_data(addons)
        state.cached_addons = addons
        state.cached_addon_counts = addon_summary_counts(addons)
        state.last_addons_refresh_ts = now
    if addons_enabled:
        addons = list(state.cached_addons)
        addon_counts = dict(state.cached_addon_counts)
    else:
        addons = []
        addon_counts = {"running": 0, "stopped": 0, "unhealthy": 0}
        if homeassistant_mode:
            state.ha_addons_api_ok = None

    integrations_enabled = not bool(getattr(args, "disable_vm_polling", False))
    integrations_interval = max(0.0, float(getattr(args, "vm_interval", 5.0) or 0.0))
    if integrations_enabled and integrations_interval > 0.0 and (not state.last_integrations_refresh_ts or (now - state.last_integrations_refresh_ts) >= integrations_interval):
        try:
            if homeassistant_mode:
                integrations = get_home_assistant_integrations(timeout=args.timeout)
            else:
                integrations = get_virtual_machines_from_virsh(args.virsh_binary, args.virsh_uri, timeout=args.timeout)
            state.ha_integrations_api_ok = True if homeassistant_mode else None
        except Exception as e:
            integrations = []
            state.ha_integrations_api_ok = False if homeassistant_mode else None
            if (now - state.last_virsh_warn_ts) >= VIRSH_WARN_INTERVAL_SECONDS:
                if homeassistant_mode:
                    logging.warning("Home Assistant integration registry unavailable; continuing without integration data (%s)", e)
                else:
                    logging.warning(
                        "virsh unavailable via %s%s; continuing without VM data (%s)",
                        args.virsh_binary,
                        f" -c {args.virsh_uri}" if args.virsh_uri else "",
                        e,
                    )
                state.last_virsh_warn_ts = now
        state.cached_integrations = integrations
        state.cached_integration_counts = integration_summary_counts(integrations)
        state.last_integrations_refresh_ts = now
    if integrations_enabled:
        integrations = list(state.cached_integrations)
        integration_counts = dict(state.cached_integration_counts)
    else:
        integrations = []
        integration_counts = {"running": 0, "stopped": 0, "paused": 0, "other": 0}
        if homeassistant_mode:
            state.ha_integrations_api_ok = None

    activity_enabled = homeassistant_mode and not bool(getattr(args, "disable_activity_polling", False))
    activity_interval = max(0.0, float(getattr(args, "activity_interval", 10.0) or 0.0))
    activity_limit = max(1, min(25, safe_int(getattr(args, "activity_limit", 12), 12) or 12))
    activity_lookback_minutes = max(5, min(1440, safe_int(getattr(args, "activity_lookback_minutes", 180), 180) or 180))
    if activity_enabled and activity_interval > 0.0 and (not state.last_activity_refresh_ts or (now - state.last_activity_refresh_ts) >= activity_interval):
        try:
            activity_rows = get_home_assistant_logbook_entries(
                timeout=args.timeout,
                limit=activity_limit,
                lookback_minutes=activity_lookback_minutes,
            )
            state.ha_activity_api_ok = True
        except Exception as e:
            activity_rows = []
            state.ha_activity_api_ok = False
            if (now - state.last_activity_warn_ts) >= ACTIVITY_WARN_INTERVAL_SECONDS:
                logging.warning("Home Assistant logbook API unavailable; continuing without activity data (%s)", e)
                state.last_activity_warn_ts = now
        state.cached_activity = activity_rows
        state.last_activity_refresh_ts = now
    if activity_enabled:
        activity_rows = list(state.cached_activity)
    else:
        activity_rows = []
        state.ha_activity_api_ok = None

    if homeassistant_mode:
        rx_bytes, tx_bytes = 0, 0
        state.active_iface = "HA Proxy"
    else:
        # get_net_bytes_local is removed, so we fallback to 0 in standalone mode for now until we fully remove it
        rx_bytes, tx_bytes, state.active_iface = 0, 0, None

    rx_kbps = 0.0
    tx_kbps = 0.0
    dt = 0.0
    if state.prev_t is not None and now > state.prev_t:
        dt = now - state.prev_t
        if not homeassistant_mode:
            if state.prev_rx is not None and rx_bytes >= state.prev_rx:
                rx_kbps = ((rx_bytes - state.prev_rx) * 8.0) / 1000.0 / dt
            if state.prev_tx is not None and tx_bytes >= state.prev_tx:
                tx_kbps = ((tx_bytes - state.prev_tx) * 8.0) / 1000.0 / dt

    net_available = True
    if ha_net_rx_kbps is not None:
        rx_kbps = ha_net_rx_kbps
        state.active_iface = "HA Proxy"
    elif homeassistant_mode:
        net_available = False
    if ha_net_tx_kbps is not None:
        tx_kbps = ha_net_tx_kbps
        state.active_iface = "HA Proxy"
        net_available = True

    if homeassistant_mode:
        disk_read_b, disk_write_b = 0, 0
        state.active_disk = "HA Proxy"
    else:
        disk_read_b, disk_write_b, state.active_disk = get_disk_bytes_local(args.disk_device, state.active_disk)

    disk_io_available = True
    disk_r_kbs = 0.0
    disk_w_kbs = 0.0
    if dt > 0.0 and not homeassistant_mode:
        if state.prev_disk_read_b is not None and disk_read_b >= state.prev_disk_read_b:
            disk_r_kbs = (disk_read_b - state.prev_disk_read_b) / 1024.0 / dt
        if state.prev_disk_write_b is not None and disk_write_b >= state.prev_disk_write_b:
            disk_w_kbs = (disk_write_b - state.prev_disk_write_b) / 1024.0 / dt
    elif homeassistant_mode:
        disk_io_available = False

    # Overwrite with HA Proxy if enabled.
    if ha_disk_read_kbs is not None:
        disk_r_kbs = ha_disk_read_kbs
        state.active_disk = "HA Proxy"
        disk_io_available = True
    if ha_disk_write_kbs is not None:
        disk_w_kbs = ha_disk_write_kbs
        state.active_disk = "HA Proxy"
        disk_io_available = True
    state.prev_disk_read_b, state.prev_disk_write_b = disk_read_b, disk_write_b
    state.prev_rx, state.prev_tx, state.prev_t = rx_bytes, tx_bytes, now

    addons_compact = compact_addons(addons)
    integrations_compact = compact_integrations(integrations)
    activity_compact = compact_activity_entries(activity_rows, max_items=5)
    ha_addons_api = -1 if state.ha_addons_api_ok is None else (1 if state.ha_addons_api_ok else 0)
    ha_integrations_api = -1 if state.ha_integrations_api_ok is None else (1 if state.ha_integrations_api_ok else 0)
    ha_activity_api = -1 if state.ha_activity_api_ok is None else (1 if state.ha_activity_api_ok else 0)

    frame = state.tx_frame_index % 6
    state.tx_frame_index = (state.tx_frame_index + 1) % 6

    # If no hardware sensor data is available (e.g. VM), do not report the value.
    # Orientation: Home Assistant System Monitor logic.
    cpu_val = f"CPU={cpu_pct:.1f}," if cpu_available else ""
    mem_val = f"MEM={mem_pct:.1f}," if mem_available else ""
    uptime_val = f"UP={int(uptime_s)}," if uptime_available else ""
    net_rx_val = f"RX={rx_kbps:.0f}," if net_available else ""
    net_tx_val = f"TX={tx_kbps:.0f}," if net_available else ""
    cpu_temp_val = f"TEMP={cpu_temp:.1f}," if cpu_temp_available else ""
    disk_temp_val = f"DISK={state.disk_temp_c:.1f}," if disk_temp_available else ""
    disk_usage_val = f"DISKPCT={state.disk_usage_pct:.1f}," if disk_usage_available else ""
    disk_read_val = f"DISKR={disk_r_kbs:.0f}," if disk_io_available else ""
    disk_write_val = f"DISKW={disk_w_kbs:.0f}," if disk_io_available else ""
    fan_val = f"FAN={state.fan_rpm:.0f}," if fan_available else ""

    # Rotate compact frames to avoid overflowing the ESP USB CDC RX buffer.
    if frame == 0:
        return (
            f"{cpu_val}"
            f"{cpu_temp_val}"
            f"{mem_val}"
            f"{uptime_val}"
            f"{net_rx_val}"
            f"{net_tx_val}"
            f"IFACE={state.active_iface or ''},"
            f"TEMPAV={1 if cpu_temp_available else 0},"
            f"HAMODE={1 if homeassistant_mode else 0},"
            f"HATOKEN={1 if state.ha_token_present else 0},"
            f"HAADDONSAPI={ha_addons_api},"
            f"HAINTEGRATIONSAPI={ha_integrations_api},"
            f"ACTEN={1 if activity_enabled else 0},"
            f"ACTAPI={ha_activity_api},"
            f"GPUEN={1 if gpu_enabled else 0},"
            f"ADDONSEN={1 if addons_enabled else 0},"
            f"INTEGRATIONSEN={1 if integrations_enabled else 0},"
            f"POWER=RUNNING\n"
        )
    if frame == 1:
        return (
            f"{disk_temp_val}"
            f"{disk_usage_val}"
            f"{disk_read_val}"
            f"{disk_write_val}"
            f"{fan_val}"
            f"DISKTAV={1 if disk_temp_available else 0},"
            f"FANAV={1 if fan_available else 0},"
            f"POWER=RUNNING\n"
        )
    if frame == 2:
        return (
            f"GPUT={state.gpu_temp_c:.1f},"
            f"GPUU={state.gpu_util_pct:.0f},"
            f"GPUVM={state.gpu_mem_pct:.0f},"
            f"GPUAV={1 if gpu_available else 0},"
            f"POWER=RUNNING\n"
        )
    if frame == 3:
        return (
            f"ADDONSRUN={int(addon_counts.get('running', 0))},"
            f"ADDONSSTOP={int(addon_counts.get('stopped', 0))},"
            f"ADDONSISSUE={int(addon_counts.get('unhealthy', 0))},"
            f"ADDONS={addons_compact},"
            f"POWER=RUNNING\n"
        )
    if frame == 4:
        return (
            f"INTEGRATIONSRUN={int(integration_counts.get('running', 0))},"
            f"INTEGRATIONSSTOP={int(integration_counts.get('stopped', 0))},"
            f"INTEGRATIONSPAUSE={int(integration_counts.get('paused', 0))},"
            f"INTEGRATIONSOTHER={int(integration_counts.get('other', 0))},"
            f"INTEGRATIONS={integrations_compact},"
            f"POWER=RUNNING\n"
        )
    return (
        f"ACTIVITY={activity_compact},"
        f"POWER=RUNNING\n"
    )


def agent_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="esp-host-bridge agent")
    ap.add_argument("--serial-port", default=None, help="Serial device path (auto-detect if omitted)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--iface", default=None, help="Network interface name, e.g. eth0")
    ap.add_argument("--docker-socket", default="/var/run/docker.sock", help="Docker Engine Unix socket path")
    ap.add_argument("--docker-interval", type=float, default=2.0, help="Docker refresh interval in seconds (0 disables polling)")
    ap.add_argument("--disable-docker-polling", action="store_true", help="Disable Docker polling entirely")
    ap.add_argument("--virsh-binary", default="virsh", help="virsh executable path")
    ap.add_argument("--virsh-uri", default=None, help="Optional virsh connection URI, e.g. qemu:///system")
    ap.add_argument("--vm-interval", type=float, default=5.0, help="VM refresh interval in seconds (0 disables polling)")
    ap.add_argument("--disable-vm-polling", action="store_true", help="Disable VM polling entirely")
    ap.add_argument("--activity-interval", type=float, default=10.0, help="Home Assistant activity refresh interval in seconds (0 disables polling)")
    ap.add_argument("--activity-limit", type=int, default=12, help="How many recent activity items to keep in the compact payload cache")
    ap.add_argument("--activity-lookback-minutes", type=int, default=180, help="How far back the Home Assistant logbook query should search")
    ap.add_argument("--disable-activity-polling", action="store_true", help="Disable Home Assistant activity polling entirely")
    ap.add_argument("--disable-gpu-polling", action="store_true", help="Disable GPU polling entirely")
    ap.add_argument("--disk-device", default=None, help="Disk device for throughput (e.g. /dev/nvme0n1 or sda)")
    ap.add_argument("--disk-temp-device", default=None, help="Disk device for temperature (e.g. /dev/nvme0n1)")
    ap.add_argument("--cpu-temp-sensor", default=None, help="Preferred CPU/core temperature sensor identifier")
    ap.add_argument("--fan-sensor", default=None, help="Preferred fan sensor identifier")
    ap.add_argument(
        "--allow-host-cmds",
        action="store_true",
        help="Execute host actions from USB CDC commands (shutdown/restart/docker_start/docker_stop/vm_start/vm_stop/vm_force_stop/vm_restart)",
    )
    ap.add_argument(
        "--host-cmd-use-sudo",
        action="store_true",
        help="Run host commands via sudo (requires sudoers rule)",
    )
    ap.add_argument("--shutdown-cmd", default=None, help="Custom host shutdown command")
    ap.add_argument("--restart-cmd", default=None, help="Custom host restart command")
    ap.add_argument("--ha-entity-cpu", default=None, help="HA Entity ID for CPU usage")
    ap.add_argument("--ha-entity-mem", default=None, help="HA Entity ID for Memory usage")
    ap.add_argument("--ha-entity-temp", default=None, help="HA Entity ID for CPU temperature")
    ap.add_argument("--ha-entity-disk-pct", default=None, help="HA Entity ID for Disk usage percent")
    ap.add_argument("--ha-entity-net-rx", default=None, help="HA Entity ID for Network RX")
    ap.add_argument("--ha-entity-net-tx", default=None, help="HA Entity ID for Network TX")
    ap.add_argument("--ha-entity-fan", default=None, help="HA Entity ID for Fan speed")
    ap.add_argument("--ha-entity-disk-temp", default=None, help="HA Entity ID for Disk temperature")
    ap.add_argument("--ha-entity-uptime", default=None, help="HA Entity ID for Uptime")
    ap.add_argument("--ha-entity-disk-read", default=None, help="HA Entity ID for Disk read speed")
    ap.add_argument("--ha-entity-disk-write", default=None, help="HA Entity ID for Disk write speed")
    ap.add_argument("--ha-entity-gpu-util", default=None, help="HA Entity ID for GPU utilization")
    ap.add_argument("--ha-entity-gpu-temp", default=None, help="HA Entity ID for GPU temperature")
    ap.add_argument("--ha-entity-gpu-vram", default=None, help="HA Entity ID for GPU VRAM usage")
    return ap


def run_agent(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    last_port = None
    state = RuntimeState()
    ser = None
    next_serial_retry_at = 0.0
    debug_mode = not args.serial_port or str(args.serial_port).upper() in ("NONE", "DEBUG")
    if debug_mode:
        logging.info("DEBUG MODE: Serial communication is disabled (Port is empty, NONE, or DEBUG)")

    try:
        while True:
            now = time.time()
            if ser is None and now >= next_serial_retry_at and not debug_mode:
                ser, last_port = try_open_serial_once(args.serial_port, args.baud, last_port=last_port)
                if ser is None:
                    next_serial_retry_at = now + SERIAL_RETRY_SECONDS
                else:
                    state.host_name_sent = False
            try:
                line = build_status_line(args, state)
                if debug_mode:
                    logging.info("[DEBUG] %s", line.strip())
                else:
                    logging.info("%s", line.strip())

                if ser is not None:
                    state.rx_buf = process_usb_commands(
                        ser,
                        state.rx_buf,
                        allow_host_cmds=args.allow_host_cmds,
                        homeassistant_mode=is_home_assistant_app_mode(),
                        host_cmd_use_sudo=args.host_cmd_use_sudo,
                        docker_socket=args.docker_socket,
                        virsh_binary=args.virsh_binary,
                        virsh_uri=args.virsh_uri,
                        timeout=args.timeout,
                        shutdown_cmd=args.shutdown_cmd,
                        restart_cmd=args.restart_cmd,
                    )
                    if not state.host_name_sent and HOST_NAME_USB:
                        ser.write(f"HOSTNAME={HOST_NAME_USB}\n".encode("utf-8", errors="ignore"))
                        state.host_name_sent = True
                    ser.write(line.encode("utf-8", errors="ignore"))
                    state.rx_buf = process_usb_commands(
                        ser,
                        state.rx_buf,
                        allow_host_cmds=args.allow_host_cmds,
                        homeassistant_mode=is_home_assistant_app_mode(),
                        host_cmd_use_sudo=args.host_cmd_use_sudo,
                        docker_socket=args.docker_socket,
                        virsh_binary=args.virsh_binary,
                        virsh_uri=args.virsh_uri,
                        timeout=args.timeout,
                        shutdown_cmd=args.shutdown_cmd,
                        restart_cmd=args.restart_cmd,
                    )
            except (SerialException, OSError) as e:
                logging.warning("serial write failed (%s), reconnecting...", e)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                state.rx_buf = ""
                next_serial_retry_at = time.time() + SERIAL_RETRY_SECONDS
            except Exception as e:
                logging.warning("%s", e)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logging.info("stopped by user (KeyboardInterrupt)")
        return 0
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
    return 0


# Web UI section

def default_webui_config_path() -> Path:
    env = os.environ.get("WEBUI_CONFIG", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().with_name("config.json")


def webui_default_cfg() -> Dict[str, Any]:
    return {
        "serial_port": "",
        "baud": 115200,
        "interval": 1.0,
        "timeout": 2.0,
        "iface": "",
        "addons_polling_enabled": True,
        "addons_interval": 2.0,
        "integrations_polling_enabled": True,
        "integrations_interval": 5.0,
        "activity_polling_enabled": True,
        "activity_interval": 10.0,
        "activity_limit": 12,
        "activity_lookback_minutes": 180,
        "gpu_polling_enabled": True,
        "disk_device": "",
        "disk_temp_device": "",
        "cpu_temp_sensor": "",
        "fan_sensor": "",
        "power_control_enabled": False,
        "ha_entity_cpu": "sensor.processor_use",
        "ha_entity_mem": "sensor.memory_use_percent",
        "ha_entity_temp": "sensor.processor_temperature",
        "ha_entity_disk_pct": "",
        "ha_entity_net_rx": "",
        "ha_entity_net_tx": "",
        "ha_entity_fan": "",
        "ha_entity_disk_temp": "",
        "ha_entity_uptime": "sensor.last_boot",
        "ha_entity_disk_read": "",
        "ha_entity_disk_write": "",
        "ha_entity_gpu_util": "",
        "ha_entity_gpu_temp": "",
        "ha_entity_gpu_vram": "",
    }


def _get_root_path() -> str:
    from flask import request
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


def _redir(value: str, key: str = "msg"):
    from flask import redirect
    root = _get_root_path()
    return redirect(f"{root}/?{key}={quote_plus(value)}")


def _clean_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v).strip()


def _clean_int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _clean_float(v: Any, default: float) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _clean_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_cfg(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = webui_default_cfg()
    cfg["serial_port"] = _clean_str(raw.get("serial_port", cfg["serial_port"]), cfg["serial_port"])
    cfg["baud"] = _clean_int(raw.get("baud", cfg["baud"]), cfg["baud"])
    cfg["interval"] = _clean_float(raw.get("interval", cfg["interval"]), cfg["interval"])
    cfg["timeout"] = _clean_float(raw.get("timeout", cfg["timeout"]), cfg["timeout"])
    cfg["iface"] = _clean_str(raw.get("iface", cfg["iface"]), cfg["iface"])
    cfg["addons_polling_enabled"] = _clean_bool(
        raw.get("addons_polling_enabled", raw.get("docker_polling_enabled", cfg["addons_polling_enabled"])),
        cfg["addons_polling_enabled"],
    )
    cfg["addons_interval"] = _clean_float(
        raw.get("addons_interval", raw.get("docker_interval", cfg["addons_interval"])),
        cfg["addons_interval"],
    )
    cfg["integrations_polling_enabled"] = _clean_bool(
        raw.get("integrations_polling_enabled", raw.get("vm_polling_enabled", cfg["integrations_polling_enabled"])),
        cfg["integrations_polling_enabled"],
    )
    cfg["integrations_interval"] = _clean_float(
        raw.get("integrations_interval", raw.get("vm_interval", cfg["integrations_interval"])),
        cfg["integrations_interval"],
    )
    cfg["activity_polling_enabled"] = _clean_bool(
        raw.get("activity_polling_enabled", cfg["activity_polling_enabled"]),
        cfg["activity_polling_enabled"],
    )
    cfg["activity_interval"] = _clean_float(
        raw.get("activity_interval", cfg["activity_interval"]),
        cfg["activity_interval"],
    )
    cfg["activity_limit"] = max(1, min(25, _clean_int(raw.get("activity_limit", cfg["activity_limit"]), cfg["activity_limit"])))
    cfg["activity_lookback_minutes"] = max(
        5,
        min(1440, _clean_int(raw.get("activity_lookback_minutes", cfg["activity_lookback_minutes"]), cfg["activity_lookback_minutes"])),
    )
    cfg["gpu_polling_enabled"] = _clean_bool(raw.get("gpu_polling_enabled", cfg["gpu_polling_enabled"]), cfg["gpu_polling_enabled"])
    cfg["disk_device"] = _clean_str(raw.get("disk_device", cfg["disk_device"]), cfg["disk_device"])
    cfg["disk_temp_device"] = _clean_str(raw.get("disk_temp_device", cfg["disk_temp_device"]), cfg["disk_temp_device"])
    cfg["cpu_temp_sensor"] = _clean_str(raw.get("cpu_temp_sensor", cfg["cpu_temp_sensor"]), cfg["cpu_temp_sensor"])
    cfg["fan_sensor"] = _clean_str(raw.get("fan_sensor", cfg["fan_sensor"]), cfg["fan_sensor"])
    cfg["power_control_enabled"] = _clean_bool(
        raw.get("power_control_enabled", raw.get("allow_host_cmds", cfg["power_control_enabled"])),
        cfg["power_control_enabled"],
    )
    for ha_key in [
        "ha_entity_cpu",
        "ha_entity_mem",
        "ha_entity_temp",
        "ha_entity_disk_pct",
        "ha_entity_net_rx",
        "ha_entity_net_tx",
        "ha_entity_fan",
        "ha_entity_disk_temp",
        "ha_entity_uptime",
        "ha_entity_disk_read",
        "ha_entity_disk_write",
        "ha_entity_gpu_util",
        "ha_entity_gpu_temp",
        "ha_entity_gpu_vram",
    ]:
        cfg[ha_key] = _clean_str(raw.get(ha_key, cfg[ha_key]), cfg[ha_key])
    return cfg


def validate_cfg(cfg: Dict[str, Any]) -> tuple[bool, str]:
    if _clean_int(cfg.get("baud"), 0) <= 0:
        return False, "baud must be > 0"
    if _clean_float(cfg.get("interval"), 0.0) <= 0.0:
        return False, "interval must be > 0"
    if _clean_float(cfg.get("timeout"), 0.0) <= 0.0:
        return False, "timeout must be > 0"
    if _clean_float(cfg.get("addons_interval"), 0.0) < 0.0:
        return False, "addons_interval must be >= 0"
    if _clean_float(cfg.get("integrations_interval"), 0.0) < 0.0:
        return False, "integrations_interval must be >= 0"
    if _clean_float(cfg.get("activity_interval"), 0.0) < 0.0:
        return False, "activity_interval must be >= 0"
    if _clean_int(cfg.get("activity_limit"), 0) <= 0:
        return False, "activity_limit must be > 0"
    if _clean_int(cfg.get("activity_lookback_minutes"), 0) <= 0:
        return False, "activity_lookback_minutes must be > 0"
    return True, "ok"


def load_cfg(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return webui_default_cfg()
    if not isinstance(obj, dict):
        return webui_default_cfg()
    return normalize_cfg(obj)


def atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def cfg_to_agent_args(cfg: Dict[str, Any]) -> list[str]:
    argv = [
        "--baud",
        str(_clean_int(cfg.get("baud"), 115200)),
        "--interval",
        str(_clean_float(cfg.get("interval"), 1.0)),
        "--timeout",
        str(_clean_float(cfg.get("timeout"), 2.0)),
        "--docker-interval",
        str(_clean_float(cfg.get("addons_interval"), 2.0)),
        "--vm-interval",
        str(_clean_float(cfg.get("integrations_interval"), 5.0)),
        "--activity-interval",
        str(_clean_float(cfg.get("activity_interval"), 10.0)),
        "--activity-limit",
        str(_clean_int(cfg.get("activity_limit"), 12)),
        "--activity-lookback-minutes",
        str(_clean_int(cfg.get("activity_lookback_minutes"), 180)),
    ]
    for key, flag in [
        ("serial_port", "--serial-port"),
        ("iface", "--iface"),
        ("disk_device", "--disk-device"),
        ("disk_temp_device", "--disk-temp-device"),
        ("cpu_temp_sensor", "--cpu-temp-sensor"),
        ("fan_sensor", "--fan-sensor"),
        ("ha_entity_cpu", "--ha-entity-cpu"),
        ("ha_entity_mem", "--ha-entity-mem"),
        ("ha_entity_temp", "--ha-entity-temp"),
        ("ha_entity_disk_pct", "--ha-entity-disk-pct"),
        ("ha_entity_net_rx", "--ha-entity-net-rx"),
        ("ha_entity_net_tx", "--ha-entity-net-tx"),
        ("ha_entity_fan", "--ha-entity-fan"),
        ("ha_entity_disk_temp", "--ha-entity-disk-temp"),
        ("ha_entity_uptime", "--ha-entity-uptime"),
        ("ha_entity_disk_read", "--ha-entity-disk-read"),
        ("ha_entity_disk_write", "--ha-entity-disk-write"),
        ("ha_entity_gpu_util", "--ha-entity-gpu-util"),
        ("ha_entity_gpu_temp", "--ha-entity-gpu-temp"),
        ("ha_entity_gpu_vram", "--ha-entity-gpu-vram"),
    ]:
        val = _clean_str(cfg.get(key), "")
        if val:
            argv += [flag, val]
    if not _clean_bool(cfg.get("addons_polling_enabled"), True):
        argv += ["--disable-docker-polling"]
    if not _clean_bool(cfg.get("integrations_polling_enabled"), True):
        argv += ["--disable-vm-polling"]
    if not _clean_bool(cfg.get("activity_polling_enabled"), True):
        argv += ["--disable-activity-polling"]
    if not _clean_bool(cfg.get("gpu_polling_enabled"), True):
        argv += ["--disable-gpu-polling"]
    if _clean_bool(cfg.get("power_control_enabled"), False):
        argv += ["--allow-host-cmds"]
    return argv


class RunnerManager:
    def __init__(self, self_script: Path, python_bin: str) -> None:
        self.self_script = self_script
        self.python_bin = python_bin
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._logs: Deque[tuple[int, str]] = deque(maxlen=MAX_LOG_LINES)
        self._next_log_id = 1
        self._comm_logs: Deque[tuple[int, str]] = deque(maxlen=MAX_LOG_LINES)
        self._next_comm_log_id = 1
        self._started_at: Optional[float] = None
        self._last_exit: Optional[int] = None
        self._cmd: Optional[list[str]] = None
        self._last_metrics_line: str = ""
        self._last_metrics_at: Optional[float] = None
        self._last_metrics: Dict[str, str] = {}
        self._metric_history: Dict[str, Deque[tuple[float, float]]] = {}
        self._serial_connected: Optional[bool] = None
        self._serial_disconnect_count: int = 0
        self._last_serial_disconnect_at: Optional[float] = None
        self._last_serial_reconnect_at: Optional[float] = None
        self._last_comm_event_at: Optional[float] = None
        self._last_comm_event_text: str = ""
        self._esp_boot_count: int = 0
        self._last_esp_boot_at: Optional[float] = None
        self._last_esp_boot_id: str = ""
        self._last_esp_boot_reason: str = ""
        self._last_esp_boot_line: str = ""
        self._ha_activity: list[dict[str, Any]] = []
        self._ha_activity_enabled: bool = False
        self._ha_activity_api_ok: Optional[bool] = None
        self._ha_activity_last_refresh_at: float = 0.0
        self._ha_activity_last_warn_ts: float = 0.0
        self._ha_host_info: Dict[str, Any] = {}
        self._last_ha_host_info_ts: float = 0.0

    def _refresh_ha_host_info(self, timeout: float = 2.0) -> None:
        now = time.time()
        if is_home_assistant_app_mode() and SUPERVISOR_TOKEN and (not self._last_ha_host_info_ts or (now - self._last_ha_host_info_ts) > 60.0):
            try:
                res = _supervisor_request_json("/host/info", timeout=timeout)
                if isinstance(res, dict):
                    self._ha_host_info = res
                    self._last_ha_host_info_ts = now
            except Exception:
                pass

    @staticmethod
    def _is_comm_event_line(line: str) -> bool:
        ll = (line or "").lower()
        comm_markers = [
            "serial connected:",
            "serial write failed",
            "serial open failed",
            "serial port not found:",
            "no serial port available",
            "no serial ports detected",
            "available ports:",
            "failed to send power state to device",
            "usb_cdc",
            "esp=boot",
        ]
        if any(m in ll for m in comm_markers):
            return True
        # Include indented available-port list lines after "available ports:"
        if ll.startswith("warning:   - /dev/") or ll.startswith("  - /dev/"):
            return True
        return False

    def _update_comm_state_from_line(self, line: str) -> None:
        ll = (line or "").lower()
        now_ts = time.time()
        self._last_comm_event_at = now_ts
        self._last_comm_event_text = (line or "").strip()
        if "serial connected:" in ll:
            self._serial_connected = True
            self._last_serial_reconnect_at = now_ts
            return
        if "esp=boot" in ll:
            if self._serial_connected is not True:
                self._last_serial_reconnect_at = now_ts
            self._serial_connected = True
            return
        if "serial write failed" in ll or "serial open failed" in ll:
            self._serial_connected = False
            self._serial_disconnect_count += 1
            self._last_serial_disconnect_at = now_ts
            return
        if "serial port not found:" in ll or "no serial port available" in ll:
            self._serial_connected = False
            if self._last_serial_disconnect_at is None:
                self._last_serial_disconnect_at = now_ts

    def _try_capture_metrics(self, line: str) -> None:
        raw = (line or "").strip()
        if not raw:
            return
        # Agent logs are usually prefixed (e.g. "INFO: "), so find the first metrics token.
        m = re.search(r'\b[A-Z][A-Z0-9_]*=', raw)
        if not m:
            return
        payload = raw[m.start():]
        if ',' not in payload and 'POWER=' not in payload:
            return
        metrics: Dict[str, str] = {}
        for part in payload.split(','):
            if '=' not in part:
                continue
            k, v = part.split('=', 1)
            key = k.strip().upper()
            val = v.strip()
            if not key:
                continue
            metrics[key] = val
        if not metrics:
            return
        now_ts = time.time()
        with self._lock:
            merged = dict(self._last_metrics)
            merged.update(metrics)
            self._last_metrics_line = payload
            self._last_metrics_at = now_ts
            self._last_metrics = merged
            for k, v in metrics.items():
                try:
                    fv = float(v)
                except Exception:
                    continue
                hist = self._metric_history.get(k)
                if hist is None:
                    hist = deque(maxlen=METRIC_HISTORY_POINTS)
                    self._metric_history[k] = hist
                hist.append((now_ts, fv))

    def _try_capture_esp_boot(self, line: str) -> None:
        raw = (line or "").strip()
        if not raw:
            return
        match = ESP_BOOT_LINE_RE.search(raw)
        if not match:
            return
        boot_id = (match.group(1) or "").strip().upper()
        boot_reason = (match.group(2) or "").strip().upper()
        now_ts = time.time()
        with self._lock:
            if boot_id:
                if boot_id == self._last_esp_boot_id and self._last_esp_boot_at and (now_ts - self._last_esp_boot_at) < 30.0:
                    return
            elif raw == self._last_esp_boot_line and self._last_esp_boot_at and (now_ts - self._last_esp_boot_at) < 10.0:
                return
            self._esp_boot_count += 1
            self._last_esp_boot_at = now_ts
            self._last_esp_boot_id = boot_id
            self._last_esp_boot_reason = boot_reason
            self._last_esp_boot_line = raw

    def _append_log(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"
        self._try_capture_metrics(line)
        self._try_capture_esp_boot(line)
        with self._lock:
            self._logs.append((self._next_log_id, line))
            self._next_log_id += 1
            if self._is_comm_event_line(line):
                self._update_comm_state_from_line(line)
                self._comm_logs.append((self._next_comm_log_id, line))
                self._next_comm_log_id += 1

    def log_event(self, line: str) -> None:
        self._append_log(line)

    def logs_tail_text(self, limit: int = 140) -> str:
        with self._lock:
            tail = list(self._logs)[-limit:]
        return "".join([line for _, line in tail])

    def logs_all_text(self) -> str:
        with self._lock:
            rows = list(self._logs)
        return "".join([line for _, line in rows])

    def comm_logs_tail_text(self, limit: int = 140) -> str:
        with self._lock:
            tail = list(self._comm_logs)[-limit:]
        return "".join([line for _, line in tail])

    def comm_logs_all_text(self) -> str:
        with self._lock:
            rows = list(self._comm_logs)
        return "".join([line for _, line in rows])

    def logs_since(self, since: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [{"id": i, "text": line} for i, line in self._logs if i >= since]
            next_id = self._next_log_id
        return rows, next_id

    def comm_logs_since(self, since: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [{"id": i, "text": line} for i, line in self._comm_logs if i >= since]
            next_id = self._next_comm_log_id
        return rows, next_id

    def clear_logs(self) -> None:
        with self._lock:
            self._logs.clear()
            self._next_log_id = 1

    def clear_comm_logs(self) -> None:
        with self._lock:
            self._comm_logs.clear()
            self._next_comm_log_id = 1

    def refresh_home_assistant_activity(self, cfg: Dict[str, Any]) -> None:
        if not is_home_assistant_app_mode():
            with self._lock:
                self._ha_activity = []
                self._ha_activity_enabled = False
                self._ha_activity_api_ok = None
                self._ha_activity_last_refresh_at = 0.0
            return
        enabled = _clean_bool(cfg.get("activity_polling_enabled"), True)
        interval = max(0.0, _clean_float(cfg.get("activity_interval"), 10.0))
        limit = max(1, min(25, _clean_int(cfg.get("activity_limit"), 12)))
        lookback_minutes = max(5, min(1440, _clean_int(cfg.get("activity_lookback_minutes"), 180)))
        timeout = max(0.5, _clean_float(cfg.get("timeout"), 2.0))
        now = time.time()
        with self._lock:
            self._ha_activity_enabled = bool(enabled and interval > 0.0)
            if not self._ha_activity_enabled:
                self._ha_activity = []
                self._ha_activity_api_ok = None
                self._ha_activity_last_refresh_at = 0.0
                return
            if self._ha_activity_last_refresh_at and (now - self._ha_activity_last_refresh_at) < interval:
                return
        try:
            rows = get_home_assistant_logbook_entries(
                timeout=timeout,
                limit=limit,
                lookback_minutes=lookback_minutes,
            )
        except Exception as e:
            warn_now = False
            with self._lock:
                self._ha_activity_api_ok = False
                self._ha_activity_last_refresh_at = now
                if (now - self._ha_activity_last_warn_ts) >= ACTIVITY_WARN_INTERVAL_SECONDS:
                    self._ha_activity_last_warn_ts = now
                    warn_now = True
            if warn_now:
                logging.warning("Home Assistant logbook API unavailable; continuing without recent activity (%s)", e)
            return
        with self._lock:
            self._ha_activity = rows
            self._ha_activity_api_ok = True
            self._ha_activity_last_refresh_at = now

    def status(self, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if cfg is not None:
            self.refresh_home_assistant_activity(cfg)
        self._refresh_ha_host_info()
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            active_iface = self._last_metrics.get("IFACE") or None
            activity_rows = copy.deepcopy(self._ha_activity)
            latest_activity_age_s = None
            if activity_rows:
                latest_ts = safe_float(activity_rows[0].get("when_ts"), None)
                if latest_ts is not None:
                    latest_activity_age_s = max(0.0, time.time() - latest_ts)
            return {
                "host_name": self._ha_host_info.get("hostname", HOST_NAME) or None,
                "operating_system": self._ha_host_info.get("operating_system") or None,
                "platform_mode": "homeassistant" if is_home_assistant_app_mode() else "host",
                "ha_status": {
                    "token_present": bool(SUPERVISOR_TOKEN),
                    "addons_api_ok": None if "HAADDONSAPI" not in self._last_metrics else bool(safe_int(self._last_metrics.get("HAADDONSAPI"), 0)),
                    "integrations_api_ok": None if "HAINTEGRATIONSAPI" not in self._last_metrics else bool(safe_int(self._last_metrics.get("HAINTEGRATIONSAPI"), 0)),
                    "activity_polling_enabled": self._ha_activity_enabled,
                    "activity_api_ok": self._ha_activity_api_ok,
                    "activity_count": len(activity_rows),
                    "activity_latest_age_s": latest_activity_age_s,
                    "addons_running": safe_int(self._last_metrics.get("ADDONSRUN"), 0) or 0,
                    "addons_stopped": safe_int(self._last_metrics.get("ADDONSSTOP"), 0) or 0,
                    "addons_issue": safe_int(self._last_metrics.get("ADDONSISSUE"), 0) or 0,
                    "integrations_loaded": safe_int(self._last_metrics.get("INTEGRATIONSRUN"), 0) or 0,
                },
                "ha_activity": activity_rows,
                "running": running,
                "pid": self._proc.pid if running and self._proc else None,
                "started_at": self._started_at,
                "last_exit": self._last_exit,
                "cmd": self._cmd,
                "next_log_id": self._next_log_id,
                "next_comm_log_id": self._next_comm_log_id,
                "comm_status": {
                    "serial_connected": self._serial_connected,
                    "serial_disconnect_count": self._serial_disconnect_count,
                    "last_serial_disconnect_at": self._last_serial_disconnect_at,
                    "last_serial_reconnect_at": self._last_serial_reconnect_at,
                    "last_comm_event_at": self._last_comm_event_at,
                    "last_comm_event_age_s": (time.time() - self._last_comm_event_at) if self._last_comm_event_at else None,
                    "last_comm_event_text": self._last_comm_event_text,
                },
                "esp_status": {
                    "boot_count": self._esp_boot_count,
                    "last_boot_at": self._last_esp_boot_at,
                    "last_boot_age_s": (time.time() - self._last_esp_boot_at) if self._last_esp_boot_at else None,
                    "last_boot_id": self._last_esp_boot_id,
                    "last_boot_reason": self._last_esp_boot_reason,
                },
                "last_metrics_at": self._last_metrics_at,
                "last_metrics_age_s": (time.time() - self._last_metrics_at) if self._last_metrics_at else None,
                "last_metrics": dict(self._last_metrics),
                "last_metrics_line": self._last_metrics_line,
                "active_iface": active_iface,
                "metric_history": {k: [float(vv) for _, vv in rows] for k, rows in self._metric_history.items()},
            }

    def _on_process_exit(self, proc: subprocess.Popen[str]) -> None:
        rc = proc.wait()
        self._append_log(f"[agent exited rc={rc}]")
        with self._lock:
            if self._proc is proc:
                self._proc = None
            self._last_exit = rc

    def _start_reader(self, proc: subprocess.Popen[str]) -> None:
        def run() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._append_log(line)

        threading.Thread(target=run, name="agent-stdout", daemon=True).start()
        threading.Thread(target=self._on_process_exit, args=(proc,), name="agent-exit", daemon=True).start()

    def start(self, cfg: Dict[str, Any]) -> tuple[bool, str]:
        ok, msg = validate_cfg(cfg)
        if not ok:
            return False, msg
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False, "Process is already running"

        cmd = [self.python_bin, str(self.self_script), "agent"] + cfg_to_agent_args(cfg)
        self._append_log("[starting] " + " ".join(shlex.quote(x) for x in cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=(os.name != "nt"),
            )
        except Exception as e:
            self._append_log(f"[start failed] {e}")
            return False, f"Failed to start: {e}"

        with self._lock:
            self._proc = proc
            self._started_at = time.time()
            self._last_exit = None
            self._cmd = cmd
        self._start_reader(proc)
        return True, "Started"

    def stop(self, timeout: float = 5.0) -> tuple[bool, str]:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return False, "No running process"
        self._append_log("[stopping]")
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._append_log("[did not exit in time; killing]")
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=timeout)
        except Exception as e:
            return False, f"Failed to stop: {e}"
        with self._lock:
            if self._proc is proc:
                self._proc = None
        return True, "Stopped"

    def restart(self, cfg: Dict[str, Any]) -> tuple[bool, str]:
        self.stop()
        time.sleep(0.2)
        return self.start(cfg)

    def stop_noexcept(self) -> None:
        try:
            self.stop()
        except Exception:
            pass


def fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _render_mode_toggle_html() -> str:
    return (
        '<div class="mode-toggle">'
        '<button id="viewSetupBtn" class="secondary" type="button">Setup</button>'
        '<button id="viewMonitorBtn" class="secondary" type="button">Dashboard</button>'
        "</div>"
    )


def _render_topbar_subtitle() -> str:
    return "USB CDC telemetry and control bridge for ESPHome"


def page_html(title: str, body: str) -> str:
    root = _get_root_path()
    mode_toggle_html = _render_mode_toggle_html()
    topbar_subtitle = _render_topbar_subtitle()
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@mdi/font@7.4.47/css/materialdesignicons.min.css">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Arimo:wght@400;700&family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@48,400,0,0&display=swap">
  <link rel="stylesheet" href="https://fonts.googleapis.com/icon?family=Material+Icons">
  <link rel="stylesheet" href="{root}/static/host/host_ui.css">
</head>
<body>
  <div class="shell">
    <div class="topbar">
      <div>
        <div class="brandline">
          <span class="title-badge" aria-hidden="true"><span class="mdi mdi-chart-line"></span></span>
          <div>
            <h1>{html.escape(title)}</h1>
            <div class="subtitle">{html.escape(topbar_subtitle)}</div>
          </div>
        </div>
      </div>
      <div class="topbar-actions">
        {mode_toggle_html}
      </div>
    </div>
    <div class="wrap">{body}</div>
  </div>
  <script>
    window.__HOST_METRICS_INGRESS_PATH__ = {json.dumps(root)};
  </script>
</body>
</html>
"""


def cfg_from_form(form: Any) -> Dict[str, Any]:
    def _has_checkbox(name: str) -> bool:
        try:
            return name in form
        except Exception:
            return form.get(name) is not None

    return normalize_cfg(
        {
            "serial_port": form.get("serial_port"),
            "baud": form.get("baud"),
            "interval": form.get("interval"),
            "timeout": form.get("timeout"),
            "iface": form.get("iface"),
            "addons_polling_enabled": _has_checkbox("addons_polling_enabled"),
            "addons_interval": form.get("addons_interval"),
            "integrations_polling_enabled": _has_checkbox("integrations_polling_enabled"),
            "integrations_interval": form.get("integrations_interval"),
            "activity_polling_enabled": _has_checkbox("activity_polling_enabled"),
            "activity_interval": form.get("activity_interval"),
            "activity_limit": form.get("activity_limit"),
            "activity_lookback_minutes": form.get("activity_lookback_minutes"),
            "gpu_polling_enabled": _has_checkbox("gpu_polling_enabled"),
            "disk_device": form.get("disk_device"),
            "disk_temp_device": form.get("disk_temp_device"),
            "cpu_temp_sensor": form.get("cpu_temp_sensor"),
            "fan_sensor": form.get("fan_sensor"),
            "power_control_enabled": _has_checkbox("power_control_enabled"),
            "ha_entity_cpu": form.get("ha_entity_cpu"),
            "ha_entity_mem": form.get("ha_entity_mem"),
            "ha_entity_temp": form.get("ha_entity_temp"),
            "ha_entity_disk_pct": form.get("ha_entity_disk_pct"),
            "ha_entity_net_rx": form.get("ha_entity_net_rx"),
            "ha_entity_net_tx": form.get("ha_entity_net_tx"),
            "ha_entity_fan": form.get("ha_entity_fan"),
            "ha_entity_disk_temp": form.get("ha_entity_disk_temp"),
            "ha_entity_uptime": form.get("ha_entity_uptime"),
            "ha_entity_disk_read": form.get("ha_entity_disk_read"),
            "ha_entity_disk_write": form.get("ha_entity_disk_write"),
        }
    )


def _load_mdi_codepoint_map(force: bool = False) -> dict[str, int]:
    global _mdi_codepoint_map_cache, _mdi_codepoint_map_cache_err
    with _mdi_codepoint_map_lock:
        if _mdi_codepoint_map_cache is not None and not force:
            return _mdi_codepoint_map_cache
        if not force:
            try:
                if MDI_CODEPOINT_CACHE_PATH.exists():
                    raw = json.loads(MDI_CODEPOINT_CACHE_PATH.read_text(encoding="utf-8", errors="ignore"))
                    if isinstance(raw, dict):
                        cached: dict[str, int] = {}
                        for k, v in raw.items():
                            try:
                                name = str(k).strip().lower()
                                if not name.startswith("mdi-"):
                                    continue
                                cached[name] = int(v)
                            except Exception:
                                continue
                        if cached:
                            _mdi_codepoint_map_cache = cached
                            _mdi_codepoint_map_cache_err = None
                            return cached
            except Exception:
                pass
        req = urllib.request.Request(
            MDI_FONT_CSS_URL,
            headers={"User-Agent": "esp-host-bridge/1.0"},
        )
        try:
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - fixed HTTPS URL
                    css = resp.read().decode("utf-8", errors="ignore")
            except Exception as first_err:
                # Local networks with intercepting proxies can break cert validation.
                retry_unverified = isinstance(first_err, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(first_err)
                if not retry_unverified:
                    raise
                ctx = ssl._create_unverified_context()  # type: ignore[attr-defined]
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:  # nosec B310 - fixed HTTPS URL
                    css = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            _mdi_codepoint_map_cache_err = str(e)
            if _mdi_codepoint_map_cache is not None:
                return _mdi_codepoint_map_cache
            raise
        out: dict[str, int] = {}
        for name, cp_hex in re.findall(
            r'\.(mdi-[a-z0-9-]+)::?before\s*\{[^}]*content:\s*"\\([0-9A-Fa-f]+)"',
            css,
            flags=re.IGNORECASE,
        ):
            try:
                out[name.lower()] = int(cp_hex, 16)
            except Exception:
                continue
        if not out:
            raise RuntimeError("Failed to parse MDI CSS codepoint map")
        _mdi_codepoint_map_cache = out
        _mdi_codepoint_map_cache_err = None
        try:
            MDI_CODEPOINT_CACHE_PATH.write_text(
                json.dumps(out, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            pass
        return out


def mdi_lookup_glyph(name: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    raw = str(name or "").strip().lower()
    if not raw:
        return None, None, "Missing icon name"
    if raw.startswith("mdi "):
        raw = raw.replace(" ", "-", 1)
    if not raw.startswith("mdi-"):
        raw = "mdi-" + raw
    try:
        cmap = _load_mdi_codepoint_map()
    except Exception as e:
        return raw, None, f"Failed to fetch MDI map: {e}"
    cp = cmap.get(raw)
    if cp is None:
        return raw, None, "MDI icon not found"
    return raw, cp, None


def _register_host_static_routes_fallback(app: Any, *, route_prefix: str = "/static/host") -> None:
    endpoint = "host_static_asset"
    if endpoint in getattr(app, "view_functions", {}):
        return

    base_dir = Path(__file__).resolve().parent
    asset_map = {
        "host_ui.js": (base_dir / "host_ui.js", "application/javascript"),
        "host_ui.css": (base_dir / "host_ui.css", "text/css"),
    }

    @app.get(f"{route_prefix}/<path:asset_name>", endpoint=endpoint)
    def host_static_asset_route(asset_name: str) -> Any:
        from flask import Response

        entry = asset_map.get(str(asset_name or "").strip().lower())
        if entry is None:
            return Response("Not Found", status=404, mimetype="text/plain")

        asset_path, mimetype = entry
        try:
            payload = asset_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logging.warning("host static asset unavailable at %s (%s)", asset_path, e)
            payload = ""
        resp = Response(payload, status=200, mimetype=mimetype)
        resp.headers["Cache-Control"] = "public, max-age=300"
        return resp



def create_app(
    *,
    autostart_override: Optional[bool] = None,
) -> Any:
    try:
        from flask import Flask, Response, jsonify, redirect, request, send_file
    except Exception as e:
        raise RuntimeError("Flask is required for webui mode. Install with: pip install flask") from e

    app = Flask(__name__, static_folder=None)
    cfg_path = default_webui_config_path()
    def _env_flag(name: str, default: bool) -> bool:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return str(raw).strip().lower() not in {"0", "false", "no", "off"}

    autostart = _env_flag("AUTOSTART", True) if autostart_override is None else bool(autostart_override)
    python_bin = os.environ.get("WEBUI_PYTHON", sys.executable or "python3")
    self_script = Path(os.environ.get("PORTABLE_HOST_METRICS_SCRIPT", str(Path(__file__).resolve())))
    pub = RunnerManager(self_script=self_script, python_bin=python_bin)

    try:
        from host_metrics.host_metrics_ui_assets import register_host_static_routes
    except Exception:
        try:
            from host_metrics_ui_assets import register_host_static_routes
        except Exception as e:
            logging.warning(
                "host_metrics_ui_assets import failed; using inline static asset fallback (%s)",
                e,
            )
            register_host_static_routes = _register_host_static_routes_fallback

    register_host_static_routes(app)

    @app.get("/")
    def index() -> str:
        cfg = load_cfg(cfg_path)
        logs = pub.logs_tail_text()
        comm_logs = pub.comm_logs_tail_text()
        msg = request.args.get("msg", "").strip()
        err = request.args.get("err", "").strip()

        msg_html = f'<div class="ok">{html.escape(msg)}</div>' if msg else ""
        err_html = f'<div class="err">{html.escape(err)}</div>' if err else ""
        homeassistant_mode = is_home_assistant_app_mode()
        ha_proxy_section = ""
        if homeassistant_mode:
            ha_proxy_section = f"""
      <details class=\"section\" data-section-key=\"ha_proxy\"><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-shield-check-outline\"></span></span>Home Assistant Proxy (Green Mode)</summary><div class=\"section-body\">
      <div class=\"hint\">Use these to pull metrics from Home Assistant's <b>System Monitor</b> integration instead of direct host access. This is required for VMs and high security ratings.</div>
      <div class=\"hint\" style=\"color:var(--accent); margin-bottom:12px;\"><b>Note:</b> You can now also manage these entities in the Home Assistant Add-on \"Configuration\" tab.</div>
      <div class=\"row\"><label>Auto-Discovery</label><div><button id=\"discoverHaProxyBtn\" class=\"secondary\" type=\"button\">Discover Entities</button><div id=\"discoverHaProxyResult\" class=\"hint\" style=\"margin-top:6px;\">Scans your Home Assistant instance for System Monitor sensors.</div></div></div>
      <div class=\"field\">
        <label for=\"ha_entity_cpu\">CPU Usage Entity</label>
        <input type=\"text\" name=\"ha_entity_cpu\" id=\"ha_entity_cpu\" value=\"{html.escape(str(cfg.get('ha_entity_cpu', '')))}\" placeholder=\"sensor.processor_use\" list=\"haSensorsList\">
        <div class=\"hint\">Entity ID for CPU percentage (e.g. <code>sensor.processor_use</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_mem\">Memory Usage Entity</label>
        <input type=\"text\" name=\"ha_entity_mem\" id=\"ha_entity_mem\" value=\"{html.escape(str(cfg.get('ha_entity_mem', '')))}\" placeholder=\"sensor.memory_use_percent\" list=\"haSensorsList\">
        <div class=\"hint\">Entity ID for Memory percentage (e.g. <code>sensor.memory_use_percent</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_temp\">CPU Temperature Entity</label>
        <input type=\"text\" name=\"ha_entity_temp\" id=\"ha_entity_temp\" value=\"{html.escape(str(cfg.get('ha_entity_temp', '')))}\" placeholder=\"sensor.processor_temperature\" list=\"haSensorsList\">
        <div class=\"hint\">Entity ID for CPU Temperature (e.g. <code>sensor.processor_temperature</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_disk_pct\">Disk Usage (%) Entity</label>
        <input type=\"text\" name=\"ha_entity_disk_pct\" id=\"ha_entity_disk_pct\" value=\"{html.escape(str(cfg.get('ha_entity_disk_pct', '')))}\" placeholder=\"sensor.disk_usage_percent_root\" list=\"haSensorsList\">
        <div class=\"hint\">Entity ID for disk percentage (e.g. <code>sensor.disk_usage_percent_root</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_net_rx\">Network RX Entity (Throughput In)</label>
        <input type=\"text\" name=\"ha_entity_net_rx\" id=\"ha_entity_net_rx\" value=\"{html.escape(str(cfg.get('ha_entity_net_rx', '')))}\" placeholder=\"sensor.network_throughput_in_eth0\" list=\"haSensorsList\">
        <div class=\"hint\">Entity ID for network download speed (e.g. <code>sensor.network_throughput_in_eth0</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_net_tx\">Network TX Entity (Throughput Out)</label>
        <input type=\"text\" name=\"ha_entity_net_tx\" id=\"ha_entity_net_tx\" value=\"{html.escape(str(cfg.get('ha_entity_net_tx', '')))}\" placeholder=\"sensor.network_throughput_out_eth0\" list=\"haSensorsList\">
        <div class=\"hint\">Entity ID for network upload speed (e.g. <code>sensor.network_throughput_out_eth0</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_disk_temp\">Disk Temperature Entity</label>
        <input type=\"text\" name=\"ha_entity_disk_temp\" id=\"ha_entity_disk_temp\" value=\"{html.escape(str(cfg.get('ha_entity_disk_temp', '')))}\" placeholder=\"sensor.disk_temperature_sda\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for disk temperature (e.g. <code>sensor.disk_temperature_sda</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_uptime\">Uptime (or Last Boot) Entity</label>
        <input type=\"text\" name=\"ha_entity_uptime\" id=\"ha_entity_uptime\" value=\"{html.escape(str(cfg.get('ha_entity_uptime', '')))}\" placeholder=\"sensor.last_boot\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for uptime (e.g. <code>sensor.last_boot</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_disk_read\">Disk Read speed Entity (B/s)</label>
        <input type=\"text\" name=\"ha_entity_disk_read\" id=\"ha_entity_disk_read\" value=\"{html.escape(str(cfg.get('ha_entity_disk_read', '')))}\" placeholder=\"sensor.disk_read_speed_sda\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for disk read throughput (e.g. <code>sensor.disk_read_speed_sda</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_disk_write\">Disk Write speed Entity (B/s)</label>
        <input type=\"text\" name=\"ha_entity_disk_write\" id=\"ha_entity_disk_write\" value=\"{html.escape(str(cfg.get('ha_entity_disk_write', '')))}\" placeholder=\"sensor.disk_write_speed_sda\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for disk write throughput (e.g. <code>sensor.disk_write_speed_sda</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_fan\">Fan Speed Entity (RPM)</label>
        <input type=\"text\" name=\"ha_entity_fan\" id=\"ha_entity_fan\" value=\"{html.escape(str(cfg.get('ha_entity_fan', '')))}\" placeholder=\"sensor.fan_speed\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for fan speed (e.g. <code>sensor.fan_speed</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_gpu_util\">GPU Utilization Entity (%)</label>
        <input type=\"text\" name=\"ha_entity_gpu_util\" id=\"ha_entity_gpu_util\" value=\"{html.escape(str(cfg.get('ha_entity_gpu_util', '')))}\" placeholder=\"sensor.gpu_utilization\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for GPU utilization (e.g. <code>sensor.gpu_utilization</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_gpu_temp\">GPU Temperature Entity</label>
        <input type=\"text\" name=\"ha_entity_gpu_temp\" id=\"ha_entity_gpu_temp\" value=\"{html.escape(str(cfg.get('ha_entity_gpu_temp', '')))}\" placeholder=\"sensor.gpu_temperature\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for GPU temperature (e.g. <code>sensor.gpu_temperature</code>)</div>
      </div>
      <div class=\"field\">
        <label for=\"ha_entity_gpu_vram\">GPU VRAM Usage Entity (%)</label>
        <input type=\"text\" name=\"ha_entity_gpu_vram\" id=\"ha_entity_gpu_vram\" value=\"{html.escape(str(cfg.get('ha_entity_gpu_vram', '')))}\" placeholder=\"sensor.gpu_vram_usage\" list=\"haSensorsList\">
        <div class=\"hint\">Optional. Entity ID for GPU VRAM usage (e.g. <code>sensor.gpu_vram_usage</code>)</div>
      </div>
      </div></details>
      """

        workload_section_title = "Add-ons" if homeassistant_mode else "Docker"
        workload_enable_label = "Enable Add-on Polling" if homeassistant_mode else "Enable Docker Polling"
        workload_enable_name = "addons_polling_enabled" if homeassistant_mode else "docker_polling_enabled"
        workload_enable_hint = (
            "Turn add-on polling on or off without changing the Home Assistant Supervisor data source."
            if homeassistant_mode
            else "Turn Docker polling on or off without deleting the socket path."
        )
        workload_source_label = "Add-on Source" if homeassistant_mode else "Docker Socket"
        workload_source_hint = (
            "Home Assistant app mode reads add-ons from the Supervisor API. This value is ignored."
            if homeassistant_mode
            else "Only used when Docker polling is enabled."
        )
        workload_interval_label = "Add-on Poll Interval (s)" if homeassistant_mode else "Docker Poll Interval (s)"
        workload_interval_name = "addons_interval" if homeassistant_mode else "docker_interval"
        workload_interval_value = cfg.get(workload_interval_name, 2.0)
        workload_interval_hint = (
            "How often the Supervisor add-on list is refreshed. Set to <code>0</code> to disable add-on polling."
            if homeassistant_mode
            else "Set to <code>0</code> to disable Docker polling entirely. <code>2</code> is a good default on low-power hosts."
        )
        vm_section_title = "Integrations" if homeassistant_mode else "Virtual Machines"
        vm_enable_label = "Enable Integration Polling" if homeassistant_mode else "Enable VM Polling"
        vm_enable_name = "integrations_polling_enabled" if homeassistant_mode else "vm_polling_enabled"
        vm_enable_hint = (
            "Turn integration polling on or off without changing the Home Assistant Core query settings."
            if homeassistant_mode
            else "Turn VM polling on or off without deleting the <code>virsh</code> settings."
        )
        vm_binary_label = "Integration Source" if homeassistant_mode else "Virsh Binary"
        vm_binary_hint = (
            "Home Assistant app mode reads integrations from the Home Assistant Core WebSocket API. This value is ignored."
            if homeassistant_mode
            else "Path to <code>virsh</code>. Use an absolute path if the Web UI launches outside your shell environment."
        )
        vm_uri_label = "Integration Query" if homeassistant_mode else "Virsh URI"
        vm_uri_hint = (
            "Home Assistant app mode groups entity-registry entries by integration domain. This value is ignored."
            if homeassistant_mode
            else "Optional libvirt connection URI, for example <code>qemu:///system</code>."
        )
        integrations_interval_label = "Integration Poll Interval (s)" if homeassistant_mode else "VM Poll Interval (s)"
        integrations_interval_name = "integrations_interval" if homeassistant_mode else "vm_interval"
        integrations_interval_value = cfg.get(integrations_interval_name, 5.0)
        integrations_interval_hint = (
            "How often the Home Assistant integration registry is refreshed. <code>5</code> is a good default."
            if homeassistant_mode
            else "How often VM data is refreshed. <code>5</code> is a good default for low-power hosts."
        )
        activity_enable_hint = (
            "Pulls the latest Home Assistant logbook entries into the dashboard and the ESP activity page."
            if homeassistant_mode
            else "Recent activity is only available in Home Assistant app mode."
        )
        activity_source_value = "Home Assistant Core Logbook API" if homeassistant_mode else "Unavailable outside Home Assistant mode"
        activity_source_hint = (
            "Uses <code>/core/api/logbook/&lt;timestamp&gt;</code> through Supervisor and keeps a compact recent-activity cache for the dashboard and ESP transport."
            if homeassistant_mode
            else "Recent activity is not available outside Home Assistant app mode."
        )
        readonly_attr = ' readonly' if homeassistant_mode else ''
        workload_source_value = (
            "Home Assistant Supervisor API"
            if homeassistant_mode
            else html.escape(str(cfg.get('docker_socket', '/var/run/docker.sock')))
        )
        vm_binary_value = (
            "Home Assistant Core WebSocket API"
            if homeassistant_mode
            else html.escape(str(cfg.get('virsh_binary', 'virsh')))
        )
        vm_uri_value = (
            "config/entity_registry/list_for_display"
            if homeassistant_mode
            else html.escape(str(cfg.get('virsh_uri', '')))
        )
        workload_source_control = (
            f"<div class=\"hint\" style=\"margin-bottom:6px;\"><code>{workload_source_value}</code></div>"
            if homeassistant_mode
            else f"<input name=\"docker_socket\" type=\"text\" value=\"{workload_source_value}\"{readonly_attr}>"
        )
        vm_binary_control = (
            f"<div class=\"hint\" style=\"margin-bottom:6px;\"><code>{vm_binary_value}</code></div>"
            if homeassistant_mode
            else f"<input name=\"virsh_binary\" type=\"text\" value=\"{vm_binary_value}\"{readonly_attr}>"
        )
        vm_uri_control = (
            f"<div class=\"hint\" style=\"margin-bottom:6px;\"><code>{vm_uri_value}</code></div>"
            if homeassistant_mode
            else f"<input name=\"virsh_uri\" type=\"text\" value=\"{vm_uri_value}\"{readonly_attr}>"
        )
        if homeassistant_mode:
            power_commands_body = f"""
      <div class=\"row\"><label>Power Control Path</label><div><input type=\"text\" value=\"Home Assistant Supervisor host API\" readonly><div class=\"hint\">Uses <code>POST /host/shutdown</code> for <code>CMD=shutdown</code> and <code>POST /host/reboot</code> for <code>CMD=restart</code> / <code>CMD=reboot</code>.</div></div></div>
      <div class=\"row\"><label>Allow Host Commands</label><div><input name=\"power_control_enabled\" type=\"checkbox\" {'checked' if cfg.get('power_control_enabled') else ''}><div class=\"hint\">Lets the ESP request host actions like shutdown and restart through Home Assistant Supervisor.</div></div></div>
            """
        else:
            power_readonly_attr = ''
            host_power_detect_hint = "Auto-fills common power commands for this operating system. Review before saving."
            shutdown_command_hint = "Optional override for <code>CMD=shutdown</code>. Example: <code>systemctl poweroff</code>"
            restart_command_hint = "Optional override for <code>CMD=restart</code> / <code>CMD=reboot</code>."
            preview_host_power_hint = "Shows what will run for <code>CMD=shutdown</code> and <code>CMD=restart</code> (no execution)."
            host_cmd_use_sudo_hint = "Only enable if you configured sudo permissions for this process."
            power_commands_body = f"""
      <div class=\"row\"><label>Host Power Command Defaults</label><div><button id=\"detectHostPowerBtn\" class=\"secondary\" type=\"button\">Detect Host Commands</button><div class=\"hint\">{host_power_detect_hint}</div><div id=\"hostPowerDetectResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
      <div class=\"row\"><label>Shutdown Command</label><div><input id=\"shutdownCmdInput\" name=\"shutdown_cmd\" type=\"text\" value=\"{html.escape(str(cfg.get('shutdown_cmd', '')))}\"{power_readonly_attr}><div class=\"hint\">{shutdown_command_hint}</div></div></div>
      <div class=\"row\"><label>Restart Command</label><div><input id=\"restartCmdInput\" name=\"restart_cmd\" type=\"text\" value=\"{html.escape(str(cfg.get('restart_cmd', '')))}\"{power_readonly_attr}><div class=\"hint\">{restart_command_hint}</div></div></div>
      <div class=\"row\"><label>Preview Host Commands</label><div><button id=\"previewHostPowerBtn\" class=\"secondary\" type=\"button\">Preview Commands</button><div class=\"hint\">{preview_host_power_hint}</div><pre id=\"hostPowerPreviewBox\" style=\"margin-top:8px; max-height:160px; min-height:80px;\">Click Preview Commands to see resolved host commands.</pre></div></div>
      <div class=\"row\"><label>Allow Host Commands</label><div><input name=\"allow_host_cmds\" type=\"checkbox\" {'checked' if cfg.get('allow_host_cmds') else ''}><div class=\"hint\">Lets the ESP request host actions like shutdown/restart. Leave off unless you need it.</div></div></div>
      <div class=\"row\"><label>Use sudo for Host Commands</label><div><input name=\"host_cmd_use_sudo\" type=\"checkbox\" {'checked' if cfg.get('host_cmd_use_sudo') else ''}><div class=\"hint\">{host_cmd_use_sudo_hint}</div></div></div>
            """
        workload_summary_label = "Add-on Summary" if homeassistant_mode else "Docker Summary"
        workload_summary_sub = "Run / Stop / Issue" if homeassistant_mode else "Run / Stop / Unhealthy"
        vm_summary_label = "Integration Summary" if homeassistant_mode else "VM Summary"
        vm_summary_sub = "Loaded integrations" if homeassistant_mode else "Run / Pause / Stop / Other"
        workload_list_label = "Add-ons" if homeassistant_mode else "Containers"
        workload_waiting_text = "Waiting for add-on data..." if homeassistant_mode else "Waiting for Docker data..."
        workload_show_all = "Show all add-ons" if homeassistant_mode else "Show all containers"
        vm_list_label = "Integrations" if homeassistant_mode else "Virtual Machines"
        vm_waiting_text = "Waiting for integration data..." if homeassistant_mode else "Waiting for VM data..."
        vm_show_all = "Show all integrations" if homeassistant_mode else "Show all virtual machines"
        st = pub.status(cfg)
        # When in HA mode, many local telemetry settings are redundant because of the proxy entities.
        if homeassistant_mode:
            telemetry_body = f"""
      <div class=\"row\"><label>Proxy Active</label><div><div class=\"ok\" style=\"display:inline-block; padding:4px 8px;\">Enabled</div><div class=\"hint\">Metrics are currently being proxied from Home Assistant entities defined above.</div></div></div>
      <div class=\"row\"><label>Update Interval (s)</label><div><input name=\"interval\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(cfg.get('interval', 1.0)))}\"><div class=\"hint\">How often metrics are sent to the ESP device.</div></div></div>
      <div class=\"row\"><label>Connection Timeout (s)</label><div><input name=\"timeout\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(cfg.get('timeout', 2.0)))}\"><div class=\"hint\">Timeout used for serial reads and host metric checks.</div></div></div>
      <div class=\"hint\" style=\"margin-top:16px;\"><b>Local Fallback:</b> If a proxy entity above is left blank, the bridge will still attempt to auto-detect hardware values (Interface, Disk, Temp) using local sensors, but this may fail in restricted VM environments.</div>
            """
        else:
            telemetry_body = f"""
      <div class=\"row\"><label>Network Interface</label><div><input id=\"ifaceInput\" name=\"iface\" type=\"text\" value=\"{html.escape(str(cfg.get('iface', '')))}\"><div class=\"hint\">Optional. Leave blank to auto-detect, or set a name like <code>eth0</code>/<code>br0</code>.</div></div></div>
      <div class=\"row\"><label>Detected Interfaces</label><div><div class=\"actions\" style=\"margin-top:0;\"><select id=\"ifaceSelect\" style=\"min-width:280px; flex:1;\"><option value=\"\">(click Refresh Interfaces)</option></select><button id=\"refreshIfaceBtn\" class=\"secondary\" type=\"button\">Refresh Interfaces</button><button id=\"useIfaceBtn\" class=\"secondary\" type=\"button\">Use Interface</button></div><div id=\"ifaceResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
      <div class=\"row\"><label>Update Interval (s)</label><div><input name=\"interval\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(cfg.get('interval', 1.0)))}\"><div class=\"hint\">How often metrics are sent to the ESP device.</div></div></div>
      <div class=\"row\"><label>Connection Timeout (s)</label><div><input name=\"timeout\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(cfg.get('timeout', 2.0)))}\"><div class=\"hint\">Timeout used for serial reads and host metric checks.</div></div></div>
      <div class=\"row\"><label>Disk Device</label><div><input id=\"diskDeviceInput\" name=\"disk_device\" type=\"text\" value=\"{html.escape(str(cfg.get('disk_device', '')))}\"><div class=\"hint\">Optional. Set a device path like <code>/dev/sda</code> if auto-detection is not correct.</div></div></div>
      <div class=\"row\"><label>Disk Temp Device</label><div><input id=\"diskTempDeviceInput\" name=\"disk_temp_device\" type=\"text\" value=\"{html.escape(str(cfg.get('disk_temp_device', '')))}\"><div class=\"hint\">Optional override for temperature checks (for example <code>/dev/nvme0</code> or <code>/dev/sda</code>).</div></div></div>
      <div class=\"row\"><label>Detected Disk Devices</label><div><div class=\"actions\" style=\"margin-top:0;\"><select id=\"diskDeviceSelect\" style=\"min-width:280px; flex:1;\"><option value=\"\">(click Refresh Disks)</option></select><button id=\"refreshDiskBtn\" class=\"secondary\" type=\"button\">Refresh Disks</button><button id=\"useDiskBtn\" class=\"secondary\" type=\"button\">Use for Disk</button><button id=\"useDiskTempBtn\" class=\"secondary\" type=\"button\">Use for Temp</button><button id=\"useDiskBothBtn\" class=\"secondary\" type=\"button\">Use for Both</button></div><div id=\"diskResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
      <div class=\"row\"><label>CPU Temp Sensor</label><div><div style=\"display:flex; align-items:center; gap:8px; flex-wrap:wrap;\"><input id=\"cpuTempSensorInput\" name=\"cpu_temp_sensor\" type=\"text\" value=\"{html.escape(str(cfg.get('cpu_temp_sensor', '')))}\"><span id=\"cpuTempSensorChip\" class=\"sensor-chip auto\">Auto</span></div><div class=\"hint\">Optional. Leave blank for auto CPU temp detection, or choose a detected sensor below.</div></div></div>
      <div class=\"row\"><label>Detected CPU Temp Sensors</label><div><div class=\"actions\" style=\"margin-top:0;\"><select id=\"cpuTempSensorSelect\" style=\"min-width:280px; flex:1;\"><option value=\"\">(click Refresh CPU Temp Sensors)</option></select><button id=\"refreshCpuTempSensorBtn\" class=\"secondary\" type=\"button\">Refresh CPU Temp Sensors</button><button id=\"useCpuTempSensorBtn\" class=\"secondary\" type=\"button\">Use Sensor</button></div><div id=\"cpuTempSensorResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
      <div class=\"row\"><label>Enable GPU Metrics</label><div><input name=\"gpu_polling_enabled\" type=\"checkbox\" {'checked' if cfg.get('gpu_polling_enabled', True) else ''}><div class=\"hint\">Turn GPU temperature, utilization, and VRAM polling on or off without affecting other telemetry.</div></div></div>
      <div class=\"row\"><label>Fan Sensor</label><div><div style=\"display:flex; align-items:center; gap:8px; flex-wrap:wrap;\"><input id=\"fanSensorInput\" name=\"fan_sensor\" type=\"text\" value=\"{html.escape(str(cfg.get('fan_sensor', '')))}\"><span id=\"fanSensorChip\" class=\"sensor-chip auto\">Auto</span></div><div class=\"hint\">Optional. Leave blank for auto fan detection, or choose a detected sensor below.</div></div></div>
      <div class=\"row\"><label>Detected Fan Sensors</label><div><div class=\"actions\" style=\"margin-top:0;\"><select id=\"fanSensorSelect\" style=\"min-width:280px; flex:1;\"><option value=\"\">(click Refresh Fan Sensors)</option></select><button id=\"refreshFanSensorBtn\" class=\"secondary\" type=\"button\">Refresh Fan Sensors</button><button id=\"useFanSensorBtn\" class=\"secondary\" type=\"button\">Use Fan</button></div><div id=\"fanSensorResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
            """
        root = _get_root_path()
        body = f"""
<div id=\"setupView\" class=\"grid\">
  <div class=\"card\">
    {msg_html}
    {err_html}
    <form method=\"post\" action=\"{root}/save\">
      <div class=\"quick-setup\">
        <h3><span class="quick-setup-icon" aria-hidden="true"><span class="mdi mdi-auto-fix"></span></span>Quick Setup</h3>
        <p>For most users: pick a serial port, test it, then save and restart the agent.</p>
        <ol>
          <li>Click <b>Refresh Ports</b> and choose your device (prefer <code>/dev/serial/by-id/...</code> on Linux/Unraid).</li>
          <li>Click <b>Use Port</b>, then click <b>Test Port</b>.</li>
          <li>Click <b>Save + Restart</b> at the bottom of the left panel.</li>
        </ol>
      </div>
      <details class=\"section\" data-section-key=\"connection\" open><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-usb-port\"></span></span>Bridge Connection</summary><div class=\"section-body\">
      <div class=\"hint\" style=\"color:var(--accent); margin-bottom:12px;\"><b>Home Assistant Users:</b> You can also manage these settings in the Add-on \"Configuration\" tab.</div>
      <div class=\"row\"><label>Serial Port</label><div><div style=\"display:flex; align-items:center; gap:8px; flex-wrap:wrap;\"><input id=\"serialPortInput\" name=\"serial_port\" type=\"text\" value=\"{html.escape(str(cfg.get('serial_port', '')))}\"><span id=\"serialPortChip\" class=\"sensor-chip auto\">Auto</span></div><div class=\"hint\">Use a stable path like <code>/dev/serial/by-id/&lt;device&gt;</code> on Linux/Unraid.</div></div></div>      <div class=\"row\"><label>Detected Ports</label><div><div class=\"actions\" style=\"margin-top:0;\"><select id=\"serialPortsSelect\" style=\"min-width:280px; flex:1;\"><option value=\"\">(click Refresh Ports)</option></select><button id=\"refreshPortsBtn\" class=\"secondary\" type=\"button\">Refresh Ports</button><button id=\"useSelectedPortBtn\" class=\"secondary\" type=\"button\">Use Port</button></div><div class=\"hint\">Choose a detected port, then click <b>Use Port</b> to copy it into Serial Port.</div><div id=\"portsResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
      <div class=\"row\"><label>Baud Rate</label><div><input id=\"baudInput\" name=\"baud\" type=\"number\" value=\"{html.escape(str(cfg.get('baud', 115200)))}\"><div class=\"hint\">Most setups use <code>115200</code>.</div></div></div>
      <div class=\"row\"><label>Port Test</label><div><button id=\"testSerialBtn\" class=\"secondary\" type=\"button\">Test Port</button><div class=\"hint\">Checks whether the selected Serial Port can be opened right now.</div><div id=\"testSerialResult\" class=\"hint\" style=\"margin-top:6px;\"></div></div></div>
      </div></details>
      {ha_proxy_section}
      <details class=\"section\" data-section-key=\"host_metrics\" open><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-chart-line\"></span></span>Telemetry</summary><div class=\"section-body\">
      {telemetry_body}
      </div></details>
      <details class=\"section\" data-section-key=\"docker\"><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-docker\"></span></span>{workload_section_title}</summary><div class=\"section-body\">
      <div class=\"row\"><label>{workload_enable_label}</label><div><input name=\"{workload_enable_name}\" type=\"checkbox\" {'checked' if cfg.get(workload_enable_name, True) else ''}><div class=\"hint\">{workload_enable_hint}</div></div></div>
      <div class=\"row\"><label>{workload_source_label}</label><div>{workload_source_control}<div class=\"hint\">{workload_source_hint}</div></div></div>
      <div class=\"row\"><label>{workload_interval_label}</label><div><input name=\"{workload_interval_name}\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(workload_interval_value))}\"><div class=\"hint\">{workload_interval_hint}</div></div></div>
      </div></details>
      <details class=\"section\" data-section-key=\"virtual_machines\"><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-monitor-multiple\"></span></span>{vm_section_title}</summary><div class=\"section-body\">
      <div class=\"row\"><label>{vm_enable_label}</label><div><input name=\"{vm_enable_name}\" type=\"checkbox\" {'checked' if cfg.get(vm_enable_name, True) else ''}><div class=\"hint\">{vm_enable_hint}</div></div></div>
      <div class=\"row\"><label>{vm_binary_label}</label><div>{vm_binary_control}<div class=\"hint\">{vm_binary_hint}</div></div></div>
      <div class=\"row\"><label>{vm_uri_label}</label><div>{vm_uri_control}<div class=\"hint\">{vm_uri_hint}</div></div></div>
      <div class=\"row\"><label>{integrations_interval_label}</label><div><input name=\"{integrations_interval_name}\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(integrations_interval_value))}\"><div class=\"hint\">{integrations_interval_hint}</div></div></div>
      </div></details>
      <details class=\"section\" data-section-key=\"recent_activity\"><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-history\"></span></span>Recent Activity</summary><div class=\"section-body\">
      <div class=\"row\"><label>Enable Activity Polling</label><div><input name=\"activity_polling_enabled\" type=\"checkbox\" {'checked' if cfg.get('activity_polling_enabled', True) else ''}><div class=\"hint\">{activity_enable_hint}</div></div></div>
      <div class=\"row\"><label>Activity Source</label><div><div class=\"hint\" style=\"margin-bottom:6px;\"><code>{activity_source_value}</code></div><div class=\"hint\">{activity_source_hint}</div></div></div>
      <div class=\"row\"><label>Activity Poll Interval (s)</label><div><input name=\"activity_interval\" type=\"number\" step=\"0.1\" value=\"{html.escape(str(cfg.get('activity_interval', 10.0)))}\"><div class=\"hint\">How often the recent activity list is refreshed. <code>10</code> is a good default.</div></div></div>
      <div class=\"row\"><label>Recent Activity Items</label><div><input name=\"activity_limit\" type=\"number\" step=\"1\" min=\"1\" max=\"25\" value=\"{html.escape(str(cfg.get('activity_limit', 12)))}\"><div class=\"hint\">How many latest logbook entries to keep in the dashboard cache. The ESP page always uses the newest 5 compact entries.</div></div></div>
      <div class=\"row\"><label>Activity Lookback (min)</label><div><input name=\"activity_lookback_minutes\" type=\"number\" step=\"1\" min=\"5\" max=\"1440\" value=\"{html.escape(str(cfg.get('activity_lookback_minutes', 180)))}\"><div class=\"hint\">How far back the logbook query searches before trimming to the latest items.</div></div></div>
      </div></details>
      <details class=\"section\" data-section-key=\"power_commands\"><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-power\"></span></span>Power Commands</summary><div class=\"section-body\">
      {power_commands_body}
      </div></details>
      <div class=\"actions form-actions-sticky\">
        <button type=\"submit\">Save + Restart</button>
        <button class=\"secondary\" type=\"submit\" formaction=\"{root}/save?restart=0\">Save Only</button>
      </div>
      <details class=\"section\" data-section-key=\"advanced_ui\"><summary><span class=\"section-icon\" aria-hidden=\"true\"><span class=\"mdi mdi-cog-outline\"></span></span>Advanced</summary><div class=\"section-body\">
      <div class=\"hint\">Config file: <code>{html.escape(str(cfg_path))}</code></div>
      <div class=\"hint\">Script path: <code>{html.escape(str(self_script))}</code></div>
      <div class=\"hint\">Autostart: <code>{'enabled' if autostart else 'disabled'}</code></div>
      </div></details>
    </form>
  </div>
  <div class="card">
    <div class="hero">
      <div class="hero-shell">
        <div class="hero-copy">
          <div class="hero-title">Bridge Status</div>
          <div class="hero-transport">Transport: USB CDC</div>
          <div class="status hero-status" id="statusLine">Agent: <b>{'Running' if st['running'] else 'Stopped'}</b> | PID: <b>{st['pid'] or '--'}</b> | Started: <b>{fmt_ts(st['started_at'])}</b> | Last Exit Code: <b>{st['last_exit'] if st['last_exit'] is not None else '--'}</b></div>
          <div class="hero-meta">
            <div class="status-pill" id="telemetryHealth">Telemetry: Waiting</div>
            <div class="status-pill" id="serialHealth">Serial: Unknown</div>
            <div class="status-pill" id="hostNameStatus">Host: --</div>
            <div class="status-pill" id="activeIfaceStatus">Active Interface: --</div>
            <div class="status-pill" id="haApiStatus">HA APIs: --</div>
            <div class="status-pill" id="serialReconnects">Reconnects: 0</div>
            <div class="status-pill" id="serialEventAge">Comm: --</div>
            <div class="status-pill" id="espBootCount">ESP Boots: 0</div>
            <div class="status-pill" id="espBootAge">Last ESP Boot: --</div>
            <div class="status-pill" id="espBootReason">Last ESP Reset: --</div>
          </div>
          <div class="actions" style="margin: 0;">
            <form method="post" action="{root}/start" style="display:inline;"><button class="secondary" type="submit">Start</button></form>
            <form method="post" action="{root}/restart" style="display:inline;"><button type="submit">Restart</button></form>
            <form method="post" action="{root}/stop" style="display:inline;"><button class="danger" type="submit">Stop</button></form>
            <form method="get" action="{root}/" style="display:inline;"><button class="secondary" type="submit">Refresh</button></form>
          </div>
        </div>
        <div class="hero-art" aria-hidden="true"><span class="mdi mdi-chart-timeline-variant"></span></div>
      </div>
    </div>
    <div class="metrics-grid" id="metricsPreview">
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-cpu-64-bit"></span></span>CPU</div><div class="metric-value" id="mCPU">Waiting...</div><div class="metric-sub">Usage</div></div>
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-memory"></span></span>Memory</div><div class="metric-value" id="mMEM">Waiting...</div><div class="metric-sub">Used</div></div>
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-thermometer"></span></span>CPU Temp</div><div class="metric-value" id="mTEMP">Waiting...</div><div class="metric-sub">Sensor</div></div>
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-lan"></span></span>Network</div><div class="metric-value" id="mNET">Waiting...</div><div class="metric-sub">RX / TX</div></div>
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-harddisk"></span></span>Disk</div><div class="metric-value" id="mDISK">Waiting...</div><div class="metric-sub">Temp / Usage</div></div>
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-puzzle-outline"></span></span>Add-ons</div><div class="metric-value" id="mDOCKER">Waiting...</div><div class="metric-sub">On / Off / Issue</div></div>
      <div class="metric-card"><div class="metric-label"><span class="metric-icon" aria-hidden="true"><span class="mdi mdi-devices"></span></span>Integrations</div><div class="metric-value" id="mVMS">Waiting...</div><div class="metric-sub">Loaded integrations</div></div>
    </div>
    <details class="section" data-section-key="comm_logs_control"><summary><span class="section-icon" aria-hidden="true"><span class="mdi mdi-transit-connection-variant"></span></span>Bridge Logs</summary><div class="section-body">
    <div class="actions" style="margin: 0 0 12px;">
      <button id="clearCommLogsBtn" class="secondary" type="button">Clear Bridge Logs</button>
      <button id="downloadCommLogsBtn" class="secondary" type="button">Download Bridge Logs</button>
    </div>
    <pre id="commLogs">{html.escape(comm_logs) if comm_logs else 'No communication events yet. Serial disconnects/reconnects will appear here.'}</pre>
    </div></details>
    <details class="section" data-section-key="logs_control"><summary><span class="section-icon" aria-hidden="true"><span class="mdi mdi-file-document-outline"></span></span>Logs</summary><div class="section-body">
    <div class="actions" style="margin: 0 0 12px;">
      <button id="clearLogsBtn" class="secondary" type="button">Clear Logs</button>
      <button id="downloadLogsBtn" class="secondary" type="button">Download Logs</button>
      <label class="hint" style="display:flex; align-items:center; gap:8px; margin:0;">
        <input id="hideMetricLogsChk" type="checkbox" style="width:16px; height:16px; margin:0;">
        Hide metric frames
      </label>
    </div>
    <pre id="logs">{html.escape(logs) if logs else 'No logs yet. Start the agent or click Refresh to load recent output.'}</pre>
    </div></details>
  </div>
</div>
<div id="monitorView" class="card">
  <div class="monitor-shell">
    <div class="dashboard-head">
      <div class="dashboard-title">Dashboard</div>
      <div class="dashboard-subtitle">Live host telemetry, bridge health, and ESP preview</div>
    </div>
    <div class="summary-bar" id="monitorSummaryBar">
      <div class="summary-chip"><div class="k">Agent</div><div class="v" id="sumAgent">--</div></div>
      <div class="summary-chip"><div class="k">Serial / Workloads</div><div class="v" id="sumDocker">--</div></div>
      <div class="summary-chip"><div class="k">Last Telemetry</div><div class="v" id="sumAge">--</div></div>
      <div class="summary-chip"><div class="k">Host Power</div><div class="v" id="sumPower">--</div></div>
      <div class="summary-chip"><div class="k">Platform</div><div class="v" id="sumMode">Home Assistant</div></div>
      <div class="summary-chip"><div class="k">HA APIs</div><div class="v" id="sumHaApis">--</div></div>
      <div class="summary-chip"><div class="k">HA Data</div><div class="v" id="sumHaData">--</div></div>
      <div class="summary-chip"><div class="k">Activity</div><div class="v" id="sumHaActivity">--</div></div>
    </div>
    <div class="monitor-grid">
      <section class="mgroup span6">
        <h3><span class="gicon" aria-hidden="true"><span class="mdi mdi-cellphone-cog"></span></span>ESP Screen Preview</h3>
        <div class="esp-preview-wrap">
          <div class="esp-preview-toolbar">
            <div class="esp-preview-tabs" id="espPreviewTabs">
              <button class="secondary" type="button" data-esp-page="home"><span class="mdi mdi-home-outline" aria-hidden="true"></span>Home</button>
              <button class="secondary" type="button" data-esp-page="docker"><span class="mdi mdi-puzzle-outline" aria-hidden="true"></span>Add-ons</button>
              <button class="secondary" type="button" data-esp-page="settings_1"><span class="mdi mdi-brightness-6" aria-hidden="true"></span>Settings 1</button>
              <button class="secondary" type="button" data-esp-page="settings_2"><span class="mdi mdi-power" aria-hidden="true"></span>Settings 2</button>
              <button class="secondary" type="button" data-esp-page="info_1"><span class="mdi mdi-access-point-network" aria-hidden="true"></span>Network</button>
              <button class="secondary" type="button" data-esp-page="info_2"><span class="mdi mdi-monitor-dashboard" aria-hidden="true"></span>System</button>
              <button class="secondary" type="button" data-esp-page="info_3"><span class="mdi mdi-thermometer" aria-hidden="true"></span>CPU Temp</button>
              <button class="secondary" type="button" data-esp-page="info_4"><span class="mdi mdi-harddisk" aria-hidden="true"></span>Disk Temp</button>
              <button class="secondary" type="button" data-esp-page="info_5"><span class="mdi mdi-chart-donut" aria-hidden="true"></span>Disk Usage</button>
              <button class="secondary" type="button" data-esp-page="info_6"><span class="mdi mdi-graph-line" aria-hidden="true"></span>GPU</button>
              <button class="secondary" type="button" data-esp-page="info_7"><span class="mdi mdi-timer-outline" aria-hidden="true"></span>Uptime</button>
              <button class="secondary" type="button" data-esp-page="info_8"><span class="mdi mdi-card-text-outline" aria-hidden="true"></span>Host Name</button>
              <button class="secondary" type="button" data-esp-page="activity"><span class="mdi mdi-history" aria-hidden="true"></span>Activity</button>
              <button class="secondary" type="button" data-esp-page="vms"><span class="mdi mdi-devices" aria-hidden="true"></span>Integrations</button>
            </div>
          </div>
          <div class="esp-shell">
            <div class="esp-viewport" id="espPreviewViewport">
              <div class="esp-display-stage" id="espPreviewStage">
                <div class="esp-screen home-mode" id="espPreviewScreen" tabindex="0" title="Swipe in the preview, click HOME quadrants, or use arrow keys to navigate">
                  <div class="esp-top" id="espPreviewTop">
                    <div class="esp-top-title" id="espTopTitle">HOME</div>
                    <div class="esp-top-pills" id="espTopPills"></div>
                    <div class="esp-page-indicator" id="espPageIndicator" aria-hidden="true"></div>
                  </div>
                  <div class="esp-page active" id="espPageHome">
                    <div class="esp-home-full">
                      <div class="esp-home-canvas">
                      <div class="esp-home-cross-v top"></div>
                      <div class="esp-home-cross-v bottom"></div>
                      <div class="esp-home-cross-h left"></div>
                      <div class="esp-home-cross-h right"></div>
                      <div class="esp-home-ring"></div>
                      <div class="esp-home-btn tl" data-esp-nav="docker" title="Add-ons"><span class="mdi mdi-puzzle-outline"></span></div>
                      <div class="esp-home-btn tr" data-esp-nav="vms" title="Integrations"><span class="mdi mdi-devices"></span></div>
                      <div class="esp-home-btn bl" data-esp-nav="info_1" data-esp-long-nav="activity" title="Info"><span class="mdi mdi-information-outline"></span></div>
                      <div class="esp-home-btn br" data-esp-nav="settings_1" title="Settings"><span class="mdi mdi-cog-outline"></span></div>
                      <div class="esp-home-center" title="Screen Saver"><span class="mdi mdi-home-assistant"></span></div>
                    </div>
                  </div>
                </div>
                <div class="esp-page" id="espPageInfo1">
                <div class="esp-dualmetric-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-dualmetric-card">
                    <div class="esp-dualmetric-stats">
                      <div class="esp-dualmetric-dot left"></div>
                      <div class="esp-dualmetric-lbl left">RX</div>
                      <div class="esp-dualmetric-val left" id="espNetRxVal">--</div>
                      <div class="esp-dualmetric-unit" style="left:114px;">MB/s</div>
                      <div class="esp-dualmetric-dot right"></div>
                      <div class="esp-dualmetric-lbl right">TX</div>
                      <div class="esp-dualmetric-val right" id="espNetTxVal">--</div>
                      <div class="esp-dualmetric-unit" style="left:290px;">MB/s</div>
                    </div>
                    <div class="esp-sys-chartbox">
                      <div id="espNetGraph"></div>
                      <div class="esp-sys-loading" id="espNetLoading">Loading...</div>
                    </div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo2">
                <div class="esp-sys-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-sys-card">
                    <div class="esp-sys-stats">
                      <div class="esp-sys-dot cpu"></div>
                      <div class="esp-sys-t" style="left:42px; top:12px;">CPU</div>
                      <div class="esp-sys-v" id="espSysCpuVal" style="left:42px; top:22px;">--</div>
                      <div class="esp-sys-u" style="left:108px; top:42px;">%</div>
                      <div class="esp-sys-dot mem"></div>
                      <div class="esp-sys-t" style="left:226px; top:12px;">MEMORY</div>
                      <div class="esp-sys-v mem" id="espSysMemVal" style="left:226px; top:22px;">--</div>
                      <div class="esp-sys-u" style="left:282px; top:42px;">%</div>
                    </div>
                    <div class="esp-sys-chartbox">
                      <div id="espSysGraph"></div>
                      <div class="esp-sys-loading" id="espSysLoading">Loading...</div>
                    </div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageDocker">
                <div class="esp-workload-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-workload-list" id="espDockerRows"></div>
                  <div class="esp-workload-empty" id="espDockerEmpty" hidden>
                    <div class="esp-workload-empty-icon"><span class="mdi mdi-puzzle-outline"></span></div>
                    <div class="esp-workload-empty-title"></div>
                    <div class="esp-workload-empty-subtitle"></div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageVms">
                <div class="esp-workload-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-workload-list" id="espVmsRows"></div>
                  <div class="esp-workload-empty" id="espVmsEmpty" hidden>
                    <div class="esp-workload-empty-icon"><span class="mdi mdi-monitor-multiple"></span></div>
                    <div class="esp-workload-empty-title"></div>
                    <div class="esp-workload-empty-subtitle"></div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo3">
                <div class="esp-metric-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-metric-card">
                    <div class="esp-metric-stats">
                      <div class="esp-metric-dot"></div>
                      <div class="esp-metric-title">CPU TEMP</div>
                      <div class="esp-metric-value" id="espCpuTempVal">--</div>
                      <div class="esp-metric-unit" style="left:108px; top:42px;">°C</div>
                    </div>
                    <div class="esp-sys-chartbox">
                      <div id="espCpuTempGraph"></div>
                      <div class="esp-sys-loading" id="espCpuTempLoading">Loading...</div>
                    </div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo4">
                <div class="esp-metric-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-metric-card">
                    <div class="esp-metric-stats">
                      <div class="esp-metric-dot violet"></div>
                      <div class="esp-metric-title">DISK TEMP</div>
                      <div class="esp-metric-value violet" id="espDiskTempVal">--</div>
                      <div class="esp-metric-unit" style="left:108px; top:42px;">°C</div>
                    </div>
                    <div class="esp-sys-chartbox">
                      <div id="espDiskTempGraph"></div>
                      <div class="esp-sys-loading" id="espDiskTempLoading">Loading...</div>
                    </div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo5">
                <div class="esp-metric-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-metric-card">
                    <div class="esp-metric-stats">
                      <div class="esp-metric-dot"></div>
                      <div class="esp-metric-title">DISK USAGE</div>
                      <div class="esp-metric-value" id="espDiskUsageVal">--</div>
                      <div class="esp-metric-unit" style="left:108px; top:42px;">%</div>
                    </div>
                    <div class="esp-sys-chartbox">
                      <div id="espDiskUsageGraph"></div>
                      <div class="esp-sys-loading" id="espDiskUsageLoading">Loading...</div>
                    </div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo6">
                <div class="esp-dualmetric-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-dualmetric-card">
                    <div class="esp-dualmetric-stats">
                      <div class="esp-dualmetric-dot left"></div>
                      <div class="esp-dualmetric-lbl left">GPU</div>
                      <div class="esp-dualmetric-val left" id="espGpuUtilVal">--</div>
                      <div class="esp-dualmetric-unit" style="left:114px;">%</div>
                      <div class="esp-dualmetric-dot right"></div>
                      <div class="esp-dualmetric-lbl right">TEMP</div>
                      <div class="esp-dualmetric-val right" id="espGpuTempVal">--</div>
                      <div class="esp-dualmetric-unit" style="left:290px;">°C</div>
                    </div>
                    <div class="esp-sys-chartbox">
                      <div id="espGpuGraph"></div>
                      <div class="esp-sys-loading" id="espGpuLoading">Loading...</div>
                    </div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo7">
                <div class="esp-uptime-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-uptime-card">
                    <div class="esp-uptime-status" id="espUptimeStatus"></div>
                    <div class="esp-uptime-value" id="espUptimeVal">--</div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageInfo8">
                <div class="esp-hostname-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-hostname-card">
                    <div class="esp-hostname-value" id="espHostNameVal">Waiting for host...</div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageActivity">
                <div class="esp-activity-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-activity-card">
                    <div class="esp-activity-empty" id="espActivityEmpty">Waiting for recent activity...</div>
                    <div class="esp-activity-rows" id="espActivityRows"></div>
                  </div>
                </div>
              </div>
                <div class="esp-page" id="espPageSettings1">
                <div class="esp-settings1-page">
                  <div class="esp-page-hint"></div>
                  <div class="esp-settings1-label">Screen Brightness</div>
                  <div class="esp-settings1-slider">
                    <div class="esp-settings1-track">
                      <div class="esp-settings1-fill" id="espBrightnessFill"></div>
                      <div class="esp-settings1-knob" id="espBrightnessKnob"></div>
                    </div>
                  </div>
                  <div class="esp-settings1-value" id="espBrightnessVal">255</div>
                </div>
              </div>
                <div class="esp-page" id="espPageSettings2">
                <div class="esp-power-exact">
                  <div class="esp-page-hint"></div>
                  <div class="esp-power-status" id="espPowerStatusExact" hidden></div>
                  <div class="esp-power-btn shutdown">Shutdown</div>
                  <div class="esp-power-btn restart">Restart</div>
                </div>
              </div>
                <div class="esp-preview-modal" id="espDockerModal" hidden>
                <div class="esp-preview-modal-card">
                  <div class="esp-preview-modal-header">
                    <div class="esp-preview-modal-heading">
                      <span class="mdi mdi-puzzle-outline"></span>
                      <div>
                        <div class="esp-preview-modal-title">Add-ons</div>
                        <div class="esp-preview-modal-subtitle">Home Assistant app control</div>
                      </div>
                    </div>
                    <button class="esp-preview-modal-close" type="button" data-esp-modal-close="docker" aria-label="Close Add-on preview">
                      <span class="mdi mdi-close"></span>
                    </button>
                  </div>
                  <div class="esp-preview-modal-body">
                    <div class="esp-preview-modal-name" id="espDockerModalName">--</div>
                    <div class="esp-state-pill other esp-preview-modal-status" id="espDockerModalStatus"></div>
                    <div class="esp-preview-modal-detail" id="espDockerModalDetail"></div>
                  </div>
                  <div class="esp-preview-modal-footer">
                    <button class="esp-modal-action start" type="button" data-esp-docker-action="start">Start</button>
                    <button class="esp-modal-action stop" type="button" data-esp-docker-action="stop">Stop</button>
                  </div>
                </div>
              </div>
                <div class="esp-preview-modal" id="espVmsModal" hidden>
                <div class="esp-preview-modal-card">
                  <div class="esp-preview-modal-header">
                    <div class="esp-preview-modal-heading">
                      <span class="mdi mdi-devices"></span>
                      <div>
                        <div class="esp-preview-modal-title">Integrations</div>
                        <div class="esp-preview-modal-subtitle">Loaded integration overview</div>
                      </div>
                    </div>
                    <button class="esp-preview-modal-close" type="button" data-esp-modal-close="vms" aria-label="Close Integration preview">
                      <span class="mdi mdi-close"></span>
                    </button>
                  </div>
                  <div class="esp-preview-modal-body">
                    <div class="esp-preview-modal-name" id="espVmsModalName">--</div>
                    <div class="esp-state-pill other esp-preview-modal-status" id="espVmsModalStatus"></div>
                    <div class="esp-preview-modal-detail" id="espVmsModalDetail"></div>
                  </div>
                  <div class="esp-preview-modal-footer">
                    <button class="esp-modal-action start" type="button" data-esp-vms-action="start">Start</button>
                    <button class="esp-modal-action stop" type="button" data-esp-vms-action="stop">Stop</button>
                    <button class="esp-modal-action restart" type="button" data-esp-vms-action="restart">Restart</button>
                  </div>
                  <div class="esp-preview-modal-footnote">Hold Stop on the device for force off</div>
                </div>
                </div>
              </div>
              </div>
            </div>
          </div>
          <div class="esp-preview-meta"><span id="espFooterPage">Preview • HOME</span><span id="espFooterPort">Port: --</span></div>
          <div class="monitor-note">Interactive browser simulator driven by live bridge telemetry. Swipe in the preview, click HOME quadrants, long-press Info for Activity, or long-press Add-on and Integration rows for actions.</div>
        </div>
      </section>
      <section class="mgroup span6"><h3><span class="gicon" aria-hidden="true"><span class="mdi mdi-chart-box-outline"></span></span>System</h3><div class="mgroup-grid">
        <div class="mcard" id="mcCPU"><div class="metric-label">CPU Usage</div><div class="metric-value" id="mvCPU">--</div><div class="metric-sub" id="msCPU"></div><svg id="sparkCPU"></svg></div>
        <div class="mcard" id="mcMEM"><div class="metric-label">Memory Usage</div><div class="metric-value" id="mvMEM">--</div><div class="metric-sub" id="msMEM"></div><svg id="sparkMEM"></svg></div>
        <div class="mcard" id="mcTEMP"><div class="metric-label">CPU Temperature</div><div class="metric-value" id="mvTEMP">--</div><div class="metric-sub" id="msTEMP"></div><svg id="sparkTEMP"></svg></div>
        <div class="mcard" id="mcUP"><div class="metric-label">Uptime</div><div class="metric-value" id="mvUP">--</div><div class="metric-sub" id="msUP"></div><svg id="sparkUP"></svg></div>
      </div></section>
      <section class="mgroup span6"><h3><span class="gicon" aria-hidden="true"><span class="mdi mdi-lan"></span></span>Network & Storage</h3><div class="mgroup-grid">
        <div class="mcard" id="mcNET"><div class="metric-label">Network RX / TX</div><div class="metric-value" id="mvNET">--</div><div class="metric-sub" id="msNET">kbps</div><svg id="sparkNET"></svg></div>
        <div class="mcard" id="mcDISKIO"><div class="metric-label">Disk Read / Write</div><div class="metric-value" id="mvDISKIO">--</div><div class="metric-sub" id="msDISKIO">kB/s</div><svg id="sparkDISKIO"></svg></div>
        <div class="mcard" id="mcDISKTEMP"><div class="metric-label">Disk Temperature</div><div class="metric-value" id="mvDISK">--</div><div class="metric-sub" id="msDISK"></div><svg id="sparkDISK"></svg></div>
        <div class="mcard" id="mcDISKPCT"><div class="metric-label">Disk Usage</div><div class="metric-value" id="mvDISKPCT">--</div><div class="metric-sub" id="msDISKPCT"></div><svg id="sparkDISKPCT"></svg></div>
      </div></section>
      <section class="mgroup span6"><h3><span class="gicon" aria-hidden="true"><span class="mdi mdi-fan"></span></span>Cooling & GPU</h3><div class="mgroup-grid">
        <div class="mcard" id="mcFAN"><div class="metric-label">Fan RPM</div><div class="metric-value" id="mvFAN">--</div><div class="metric-sub" id="msFAN"></div><svg id="sparkFAN"></svg></div>
        <div class="mcard" id="mcGPUU"><div class="metric-label">GPU Utilization</div><div class="metric-value" id="mvGPUU">--</div><div class="metric-sub" id="msGPUU"></div><svg id="sparkGPUU"></svg></div>
        <div class="mcard" id="mcGPUT"><div class="metric-label">GPU Temperature</div><div class="metric-value" id="mvGPUT">--</div><div class="metric-sub" id="msGPUT"></div><svg id="sparkGPUT"></svg></div>
        <div class="mcard" id="mcGPUVM"><div class="metric-label">GPU VRAM</div><div class="metric-value" id="mvGPUVM">--</div><div class="metric-sub" id="msGPUVM"></div><svg id="sparkGPUVM"></svg></div>
      </div></section>
      <section class="mgroup span6"><h3><span class="gicon" aria-hidden="true"><span class="mdi mdi-apps"></span></span>Workloads</h3><div class="mgroup-grid">
        <div class="mcard"><div class="metric-label">{workload_summary_label}</div><div class="metric-value" id="mvDockerCounts">--</div><div class="metric-sub" id="msDockerCounts">{workload_summary_sub}</div></div>
        <div class="mcard"><div class="metric-label">{vm_summary_label}</div><div class="metric-value" id="mvVmCounts">--</div><div class="metric-sub" id="msVmCounts">{vm_summary_sub}</div></div>
        <div class="mcard"><div class="metric-label">{workload_list_label}</div><div class="metric-sub" id="dockerMoreHint">{workload_waiting_text}</div><ul class="docker-list" id="dockerPreviewList"></ul><details><summary class="monitor-note">{workload_show_all}</summary><ul class="docker-list" id="dockerAllList"></ul></details></div>
        <div class="mcard"><div class="metric-label">{vm_list_label}</div><div class="metric-sub" id="vmMoreHint">{vm_waiting_text}</div><ul class="docker-list" id="vmPreviewList"></ul><details><summary class="monitor-note">{vm_show_all}</summary><ul class="docker-list" id="vmAllList"></ul></details></div>
      </div></section>
      <section class="mgroup span12"><h3><span class="gicon" aria-hidden="true"><span class="mdi mdi-history"></span></span>Recent Activity</h3>
        <div class="mcard">
          <div class="metric-label">Home Assistant Logbook</div>
          <div class="metric-sub" id="activityHint">Waiting for recent activity...</div>
          <div class="activity-empty" id="activityEmpty">Waiting for recent activity...</div>
          <ul class="activity-list" id="activityList"></ul>
        </div>
      </section>
    </div>
  </div>
</div>
<datalist id=\"haSensorsList\"></datalist>
<script>
window.__HOST_METRICS_BOOT__ = {{
  nextLogId: {st['next_log_id']},
  nextCommLogId: {st.get('next_comm_log_id', 1)},
}};
</script>
<script src="{root}/static/host/host_ui.js"></script>
"""
        return page_html("ESP Host Bridge", body)

    @app.post("/save")
    def save() -> Any:
        cfg = cfg_from_form(request.form)
        ok, message = validate_cfg(cfg)
        if not ok:
            return _redir(message, key="err")
        atomic_write_json(cfg_path, cfg)
        restart = int(request.args.get("restart", "1"))
        if restart:
            ok_run, message_run = pub.restart(cfg)
            if not ok_run:
                return _redir(message_run, key="err")
            return _redir("Saved and restarted")
        return _redir("Saved")

    @app.post("/start")
    def start_proc() -> Any:
        cfg = load_cfg(cfg_path)
        ok, message = pub.start(cfg)
        return _redir(message, key="msg" if ok else "err")

    @app.post("/restart")
    def restart_proc() -> Any:
        cfg = load_cfg(cfg_path)
        ok, message = pub.restart(cfg)
        return _redir(message, key="msg" if ok else "err")

    @app.post("/stop")
    def stop_proc() -> Any:
        ok, message = pub.stop()
        return _redir(message, key="msg" if ok else "err")

    @app.get("/api/status")
    def api_status() -> Any:
        return jsonify(pub.status(load_cfg(cfg_path)))

    @app.get("/api/discover-ha-proxy")
    def api_discover_ha_proxy() -> Any:
        try:
            cfg = load_cfg(cfg_path)
            timeout = _clean_float(cfg.get("timeout"), 5.0)
            data = discover_ha_proxy_entities(timeout=timeout)
            return jsonify(data)
        except Exception as e:
            logging.error("HA discovery failed: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.get("/api/ha-entities")
    def api_ha_entities() -> Any:
        states = get_home_assistant_all_states()
        sensors = []
        for s in states:
            eid = s.get("entity_id", "")
            if eid.startswith("sensor."):
                friendly = s.get("attributes", {}).get("friendly_name", eid)
                sensors.append({"id": eid, "name": friendly})
        sensors.sort(key=lambda x: x["id"])
        return jsonify({"sensors": sensors})

    @app.get("/api/config")
    def api_config() -> Any:
        return jsonify(load_cfg(cfg_path))

    @app.get("/api/ports")
    def api_ports() -> Any:
        return jsonify({"ports": list_serial_port_choices()})

    @app.get("/api/hardware-choices")
    def api_hardware_choices() -> Any:
        return jsonify(detect_hardware_choices())

    @app.post("/api/test-serial")
    def api_test_serial() -> Any:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            port = payload.get("port")
            baud = payload.get("baud", 115200)
        else:
            port = None
            baud = 115200
        ok, message = test_serial_open(None if port is None else str(port), baud)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    if not is_home_assistant_app_mode():
        @app.get("/api/host-power-defaults")
        def api_host_power_defaults() -> Any:
            return jsonify(detect_host_power_command_defaults())

        @app.post("/api/host-power-preview")
        def api_host_power_preview() -> Any:
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                payload = {}
            use_sudo = _clean_bool(payload.get("host_cmd_use_sudo"), False)
            shutdown_cmd = _clean_str(payload.get("shutdown_cmd"), "")
            restart_cmd = _clean_str(payload.get("restart_cmd"), "")

            def _preview(cmd_name: str) -> Dict[str, Any]:
                argv, err = resolve_host_command_argv(
                    cmd_name,
                    use_sudo=use_sudo,
                    shutdown_cmd=shutdown_cmd,
                    restart_cmd=restart_cmd,
                )
                if argv is None:
                    return {"ok": False, "command": "", "message": err or "not available"}
                return {
                    "ok": True,
                    "command": " ".join(shlex.quote(x) for x in argv),
                    "message": "ok",
                }

            return jsonify({
                "shutdown": _preview("shutdown"),
                "restart": _preview("restart"),
            })

    @app.get("/api/logs")
    def api_logs() -> Any:
        since = request.args.get("since", default="1")
        try:
            since_id = max(1, int(since))
        except ValueError:
            since_id = 1
        rows, next_id = pub.logs_since(since_id)
        return jsonify({"lines": rows, "next": next_id})

    @app.post("/api/logs/clear")
    def api_logs_clear() -> Any:
        pub.clear_logs()
        return jsonify({"ok": True, "message": "Logs cleared"})

    @app.get("/api/logs/text")
    def api_logs_text() -> Any:
        body = pub.logs_all_text() or "No logs yet. Start the agent or click Refresh to load recent output.\n"
        ts = time.strftime("%Y%m%d-%H%M%S")
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="esp-host-bridge-{ts}.log"'},
        )

    @app.get("/api/comm-logs")
    def api_comm_logs() -> Any:
        since = request.args.get("since", default="1")
        try:
            since_id = max(1, int(since))
        except ValueError:
            since_id = 1
        rows, next_id = pub.comm_logs_since(since_id)
        return jsonify({"lines": rows, "next": next_id})

    @app.post("/api/comm-logs/clear")
    def api_comm_logs_clear() -> Any:
        pub.clear_comm_logs()
        return jsonify({"ok": True, "message": "Communication logs cleared"})

    @app.get("/api/comm-logs/text")
    def api_comm_logs_text() -> Any:
        body = pub.comm_logs_all_text() or "No communication events yet. Serial disconnects/reconnects will appear here.\n"
        ts = time.strftime("%Y%m%d-%H%M%S")
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="esp-host-bridge-comm-{ts}.log"'},
        )


    @app.post("/api/start")
    def api_start() -> Any:
        payload = request.get_json(silent=True) or {}
        cfg = normalize_cfg(payload) if isinstance(payload, dict) and payload else load_cfg(cfg_path)
        ok_valid, msg_valid = validate_cfg(cfg)
        if not ok_valid:
            return jsonify({"ok": False, "message": msg_valid}), 400
        ok, message = pub.start(cfg)
        if ok and isinstance(payload, dict) and payload:
            atomic_write_json(cfg_path, cfg)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.post("/api/stop")
    def api_stop() -> Any:
        ok, message = pub.stop()
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    @app.post("/api/restart")
    def api_restart() -> Any:
        payload = request.get_json(silent=True) or {}
        cfg = normalize_cfg(payload) if isinstance(payload, dict) and payload else load_cfg(cfg_path)
        ok_valid, msg_valid = validate_cfg(cfg)
        if not ok_valid:
            return jsonify({"ok": False, "message": msg_valid}), 400
        if isinstance(payload, dict) and payload:
            atomic_write_json(cfg_path, cfg)
        ok, message = pub.restart(cfg)
        return jsonify({"ok": ok, "message": message}), (200 if ok else 400)

    def maybe_autostart() -> None:
        if not autostart:
            return
        cfg = load_cfg(cfg_path)
        ok, msg = validate_cfg(cfg)
        if not ok:
            pub.log_event(f"[autostart skipped] {msg}")
            return
        ok_start, message = pub.start(cfg)
        if ok_start:
            pub.log_event("[autostart enabled]")
        else:
            pub.log_event(f"[autostart skipped] {message}")

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        atomic_write_json(cfg_path, webui_default_cfg())
    maybe_autostart()
    atexit.register(pub.stop_noexcept)
    return app


def run_webui(args: argparse.Namespace) -> int:
    app = create_app()
    port = int(args.port or os.environ.get("WEBUI_PORT", str(WEBUI_DEFAULT_PORT)))
    host = args.host or os.environ.get("WEBUI_HOST", "0.0.0.0")
    app.run(host=host, port=port, debug=False)
    return 0


def webui_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="esp-host-bridge webui")
    ap.add_argument("--host", default=None, help="Bind host (default from WEBUI_HOST or 0.0.0.0)")
    ap.add_argument("--port", type=int, default=None, help="Bind port (default from WEBUI_PORT or 8654)")
    return ap


def parse_mode_and_args(argv: list[str]) -> tuple[str, argparse.Namespace]:
    if len(argv) <= 1:
        return "webui", webui_arg_parser().parse_args([])

    mode = argv[1].lower()
    if mode == "agent":
        return "agent", agent_arg_parser().parse_args(argv[2:])
    if mode == "webui":
        return "webui", webui_arg_parser().parse_args(argv[2:])

    if any(a.startswith("--") for a in argv[1:]):
        # Backward-compatible behavior: treat top-level flags as agent mode.
        return "agent", agent_arg_parser().parse_args(argv[1:])

    raise SystemExit("Usage: esp-host-bridge [webui|agent] [options]")


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    mode, args = parse_mode_and_args(argv)
    if mode == "agent":
        return run_agent(args)
    return run_webui(args)


if __name__ == "__main__":
    raise SystemExit(main())
