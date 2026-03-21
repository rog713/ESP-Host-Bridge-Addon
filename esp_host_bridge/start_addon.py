from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_PORT = 8654
DEFAULT_HOST = "0.0.0.0"
OPTIONS_PATH = Path("/data/options.json")
CONFIG_PATH = Path("/data/config.json")
APP_DIR = Path("/opt/esp-host-bridge/app")
APP_PATH = APP_DIR / "host_metrics.py"
SUPERVISOR_TOKEN_PATH = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")


def load_options() -> dict[str, object]:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        payload = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def sync_config(options: dict[str, object]) -> None:
    """Merge options into config.json to maintain sync with HA Configuration tab."""
    config = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    
    # Update config with everything from options except webui_host/port
    updated = False
    for key, value in options.items():
        if key in ("webui_host", "webui_port"):
            continue
        if config.get(key) != value:
            config[key] = value
            updated = True
            
    if updated or not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def coerce_host(value: object) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_HOST


def coerce_port(value: object) -> int:
    try:
        port = int(value)
    except Exception:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def build_env(options: dict[str, object]) -> dict[str, str]:
    env = dict(os.environ)
    if not str(env.get("SUPERVISOR_TOKEN", "") or "").strip():
        try:
            if SUPERVISOR_TOKEN_PATH.is_file():
                env["SUPERVISOR_TOKEN"] = SUPERVISOR_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    env["PYTHONUNBUFFERED"] = "1"
    env["WEBUI_CONFIG"] = str(CONFIG_PATH)
    env["WEBUI_HOST"] = coerce_host(options.get("webui_host"))
    env["WEBUI_PORT"] = str(coerce_port(options.get("webui_port")))
    env["ESP_HOST_BRIDGE_PLATFORM_MODE"] = "homeassistant"
    env["ESP_HOST_BRIDGE_SELF_SLUG"] = "esp_host_bridge"
    return env


def build_argv(env: dict[str, str]) -> list[str]:
    return [
        sys.executable,
        str(APP_PATH),
        "webui",
        "--host",
        env["WEBUI_HOST"],
        "--port",
        env["WEBUI_PORT"],
    ]


def main() -> int:
    options = load_options()
    sync_config(options)
    env = build_env(options)
    argv = build_argv(env)

    if os.environ.get("ESP_HOST_BRIDGE_ADDON_VALIDATE") == "1":
        print(
            json.dumps(
                {
                    "argv": argv,
                    "app_dir": str(APP_DIR),
                    "app_path": str(APP_PATH),
                    "webui_config": env["WEBUI_CONFIG"],
                    "webui_host": env["WEBUI_HOST"],
                    "webui_port": env["WEBUI_PORT"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    os.chdir(APP_DIR)

    print(f"[esp-host-bridge-addon] starting web UI on {env['WEBUI_HOST']}:{env['WEBUI_PORT']}", flush=True)
    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    raise SystemExit(main())
