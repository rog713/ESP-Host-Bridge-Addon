from __future__ import annotations

import logging
from typing import Any, Dict

from ..metrics import compact_activity_entries, get_home_assistant_logbook_entries
from .base import CleanerSet, ConfigFieldSpec, DashboardDetailSpec, IntegrationSpec, PollContext, PreviewPageSpec

ACTIVITY_WARN_INTERVAL_SECONDS = 30.0

ACTIVITY_CONFIG_FIELDS = (
    ConfigFieldSpec(
        "activity_polling_enabled",
        "bool",
        True,
        checkbox=True,
        label="Enable Activity Polling",
        hint="Poll the Home Assistant logbook and surface recent activity in the dashboard and ESP preview.",
        section_key="activity",
        homeassistant_label="Enable Activity Polling",
        homeassistant_hint="Poll the Home Assistant logbook and surface recent activity in the dashboard and ESP preview.",
    ),
    ConfigFieldSpec(
        "activity_interval",
        "float",
        10.0,
        cli_flag="--activity-interval",
        label="Activity Poll Interval (s)",
        hint="How often recent Home Assistant activity is refreshed.",
        section_key="activity",
        input_step="0.1",
    ),
    ConfigFieldSpec(
        "activity_limit",
        "int",
        12,
        cli_flag="--activity-limit",
        label="Activity Row Limit",
        hint="Maximum recent logbook entries to retain internally. The ESP preview still shows the latest five.",
        section_key="activity",
        input_step="1",
    ),
    ConfigFieldSpec(
        "activity_lookback_minutes",
        "int",
        180,
        cli_flag="--activity-lookback-minutes",
        label="Activity Lookback (min)",
        hint="Lookback window used when querying the Home Assistant logbook API.",
        section_key="activity",
        input_step="1",
    ),
)

ACTIVITY_DASHBOARD_DETAILS = (
    DashboardDetailSpec(
        detail_id="activity",
        title="Recent Activity",
        render_kind="activity_list",
        waiting_text="Waiting for recent activity...",
        show_all_text="Show recent activity",
        homeassistant_title="Recent Activity",
        homeassistant_waiting_text="Waiting for recent activity...",
        homeassistant_show_all_text="Show recent activity",
        span_class="span12",
    ),
)

ACTIVITY_PREVIEW_PAGES = (
    PreviewPageSpec(
        page_id="activity",
        dom_id="espPageActivity",
        preview_order=12,
        render_kind="activity_list",
        title="ACTIVITY",
        footer="Activity",
        render_data={
            "rows_id": "espActivityRows",
            "empty_id": "espActivityEmpty",
        },
        nav_up="home",
        nav_left="info_1",
        nav_right="vms",
        homeassistant_title="ACTIVITY",
        homeassistant_footer="Activity",
    ),
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def parse_compact_activity(value: Any) -> list[dict[str, str]]:
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return []
    rows: list[dict[str, str]] = []
    for item in raw.split(";"):
        token = str(item or "").strip()
        if not token:
            continue
        parts = token.split("|")
        rows.append(
            {
                "name": str(parts[0] if len(parts) > 0 else "").strip(),
                "message": str(parts[1] if len(parts) > 1 else "").strip(),
                "age": str(parts[2] if len(parts) > 2 else "").strip(),
                "source": str(parts[3] if len(parts) > 3 else "").strip(),
                "tail": str(parts[4] if len(parts) > 4 else "").strip(),
            }
        )
    return [row for row in rows if any(row.values())][:5]


def detail_payloads(last_metrics: Dict[str, Any], homeassistant_mode: bool) -> Dict[str, Dict[str, Any]]:
    items = parse_compact_activity(last_metrics.get("ACTIVITY"))
    token = _safe_int(last_metrics.get("HATOKEN"), 0) if homeassistant_mode else 1
    api = _safe_int(last_metrics.get("ACTAPI"), -1) if homeassistant_mode else 1
    enabled = _safe_int(last_metrics.get("ACTEN"), 0) if homeassistant_mode else 1
    if homeassistant_mode and enabled == 0:
        hint = "Recent activity polling is disabled."
    elif homeassistant_mode and token == 0:
        hint = "Supervisor token missing in app container."
    elif homeassistant_mode and api == 0:
        hint = "Logbook API unavailable; check app logs."
    elif not items:
        hint = "No recent activity in the current lookback window."
    elif len(items) == 1:
        hint = "Showing 1 recent logbook entry."
    else:
        hint = f"Showing {len(items)} recent logbook entries."
    return {
        "activity": {
            "kind": "activity_list",
            "items": items,
            "hint": hint,
            "enabled": bool(enabled),
            "token_present": bool(token),
            "api_ok": None if api < 0 else bool(api),
        }
    }


def _cache(state: Any) -> Dict[str, Any]:
    integration_cache = getattr(state, "integration_cache", None)
    if not isinstance(integration_cache, dict):
        integration_cache = {}
        setattr(state, "integration_cache", integration_cache)
    cached = integration_cache.get("activity")
    if not isinstance(cached, dict):
        cached = {
            "items": [],
            "last_refresh_ts": 0.0,
            "last_success_ts": 0.0,
            "last_warn_ts": 0.0,
            "last_error": "",
            "last_error_ts": 0.0,
            "api_ok": None,
            "available": None,
        }
        integration_cache["activity"] = cached
    return cached


def validate_cfg(cfg: Dict[str, Any], clean: CleanerSet) -> list[str]:
    errors: list[str] = []
    if clean.clean_float(cfg.get("activity_interval"), 0.0) < 0.0:
        errors.append("activity_interval must be >= 0")
    if clean.clean_int(cfg.get("activity_limit"), 12) < 1:
        errors.append("activity_limit must be >= 1")
    if clean.clean_int(cfg.get("activity_lookback_minutes"), 180) < 5:
        errors.append("activity_lookback_minutes must be >= 5")
    return errors


def cfg_to_agent_args(cfg: Dict[str, Any], clean: CleanerSet) -> list[str]:
    argv = [
        "--activity-interval",
        str(clean.clean_float(cfg.get("activity_interval"), 10.0)),
        "--activity-limit",
        str(clean.clean_int(cfg.get("activity_limit"), 12)),
        "--activity-lookback-minutes",
        str(clean.clean_int(cfg.get("activity_lookback_minutes"), 180)),
    ]
    if not clean.clean_bool(cfg.get("activity_polling_enabled"), True):
        argv.append("--disable-activity-polling")
    return argv


def poll(ctx: PollContext) -> Dict[str, Any]:
    cache = _cache(ctx.state)
    enabled = ctx.homeassistant_mode and not bool(getattr(ctx.args, "disable_activity_polling", False))
    interval = max(0.0, float(getattr(ctx.args, "activity_interval", 10.0) or 0.0))
    limit = max(1, min(25, _safe_int(getattr(ctx.args, "activity_limit", 12), 12)))
    lookback_minutes = max(5, min(1440, _safe_int(getattr(ctx.args, "activity_lookback_minutes", 180), 180)))

    if enabled and interval > 0.0 and (
        not cache.get("last_refresh_ts") or (ctx.now - float(cache.get("last_refresh_ts") or 0.0)) >= interval
    ):
        try:
            items = get_home_assistant_logbook_entries(
                timeout=ctx.args.timeout,
                limit=limit,
                lookback_minutes=lookback_minutes,
            )
            cache["api_ok"] = True
            cache["available"] = True
            cache["last_success_ts"] = ctx.now
            cache["last_error"] = ""
            cache["last_error_ts"] = 0.0
        except Exception as exc:
            items = []
            cache["api_ok"] = False
            cache["available"] = False
            cache["last_error"] = str(exc).strip()[:200]
            cache["last_error_ts"] = ctx.now
            last_warn_ts = float(cache.get("last_warn_ts") or 0.0)
            if (ctx.now - last_warn_ts) >= ACTIVITY_WARN_INTERVAL_SECONDS:
                logging.warning("Home Assistant logbook API unavailable; continuing without activity data (%s)", exc)
                cache["last_warn_ts"] = ctx.now
        cache["items"] = items
        cache["last_refresh_ts"] = ctx.now

    items = list(cache.get("items") or []) if enabled else []
    if not enabled:
        cache["api_ok"] = None
        cache["available"] = None
    last_refresh_ts = float(cache.get("last_refresh_ts") or 0.0)
    last_success_ts = float(cache.get("last_success_ts") or 0.0)
    last_error_ts = float(cache.get("last_error_ts") or 0.0)
    last_error = str(cache.get("last_error") or "").strip()
    return {
        "enabled": enabled,
        "items": items,
        "compact": compact_activity_entries(items, max_items=5),
        "api_ok": cache.get("api_ok"),
        "health": {
            "integration_id": "activity",
            "enabled": enabled,
            "available": cache.get("available"),
            "source": "home_assistant_logbook",
            "last_refresh_ts": last_refresh_ts or None,
            "last_success_ts": last_success_ts or None,
            "last_error": last_error or None,
            "last_error_ts": last_error_ts or None,
            "commands": [],
            "api_ok": cache.get("api_ok"),
        },
    }


ACTIVITY_INTEGRATION = IntegrationSpec(
    integration_id="activity",
    title="Activity",
    homeassistant_title="Activity",
    homeassistant_only=True,
    section_key="activity",
    icon_class="mdi-history",
    sort_order=3,
    action_group_title="Activity",
    homeassistant_action_group_title="Activity",
    config_fields=ACTIVITY_CONFIG_FIELDS,
    dashboard_details=ACTIVITY_DASHBOARD_DETAILS,
    preview_pages=ACTIVITY_PREVIEW_PAGES,
    validate_cfg=validate_cfg,
    cfg_to_agent_args=cfg_to_agent_args,
    poll=poll,
    detail_payloads=detail_payloads,
)
