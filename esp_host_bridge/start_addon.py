from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_PORT = 8654
DEFAULT_HOST = "0.0.0.0"
CONFIG_PATH = Path("/data/config.json")
APP_DIR = Path("/opt/esp-host-bridge/app")
APP_PATH = APP_DIR / "host_metrics.py"
SUPERVISOR_TOKEN_PATH = Path("/run/s6/container_environment/SUPERVISOR_TOKEN")


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
    env = build_env()
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
