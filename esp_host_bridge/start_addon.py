from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

DEFAULT_PORT = 8654
DEFAULT_HOST = "0.0.0.0"
CONFIG_PATH = Path("/data/config.json")
BASE_DIR = Path(__file__).resolve().parent
APP_DIR = BASE_DIR / "app"
ADDON_CONFIG_PATH = BASE_DIR / "config.yaml"
SUPERVISOR_TOKEN_PATH = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")


def detect_addon_version() -> str:
    try:
        raw = ADDON_CONFIG_PATH.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    match = re.search(r'(?m)^version:\s*"?(.*?)"?\s*$', raw)
    return str(match.group(1) or "").strip() if match else ""


def build_env() -> dict[str, str]:
    env = dict(os.environ)
    if not str(env.get("SUPERVISOR_TOKEN", "") or "").strip():
        try:
            if SUPERVISOR_TOKEN_PATH.is_file():
                env["SUPERVISOR_TOKEN"] = SUPERVISOR_TOKEN_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    env["PYTHONUNBUFFERED"] = "1"
    env["WEBUI_CONFIG"] = str(CONFIG_PATH)
    env["WEBUI_HOST"] = str(env.get("WEBUI_HOST") or DEFAULT_HOST)
    env["WEBUI_PORT"] = str(env.get("WEBUI_PORT") or DEFAULT_PORT)
    env["PYTHONPATH"] = str(APP_DIR) + (f":{env['PYTHONPATH']}" if str(env.get("PYTHONPATH") or "").strip() else "")
    env["ESP_HOST_BRIDGE_PLATFORM_MODE"] = "homeassistant"
    env["ESP_HOST_BRIDGE_SELF_SLUG"] = "esp_host_bridge"
    addon_version = detect_addon_version()
    if addon_version:
        env["ESP_HOST_BRIDGE_VERSION"] = addon_version
    return env


def build_argv(env: dict[str, str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "esp_host_bridge",
        "webui",
        "--host",
        env["WEBUI_HOST"],
        "--port",
        env["WEBUI_PORT"],
    ]


def main() -> int:
    env = build_env()
    argv = build_argv(env)

    if os.environ.get("ESP_HOST_BRIDGE_ADDON_VALIDATE") == "1":
        print(
            json.dumps(
                {
                    "argv": argv,
                    "app_dir": str(APP_DIR),
                    "bridge_version": env.get("ESP_HOST_BRIDGE_VERSION", ""),
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
