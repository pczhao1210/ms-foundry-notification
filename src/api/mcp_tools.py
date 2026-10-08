"""Read-only MCP tools (Streamable HTTP at /runtime/webhooks/mcp, `mcp_extension` system key) over `api.service`.

Each tool mirrors a REST endpoint and returns the same JSON structure.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

import azure.functions as func

from api import service
from core.query import DEFAULT_DAYS, DEFAULT_LIMIT, DEFAULT_RESPONSE_BYTES, MAX_DAYS, MAX_LIMIT

bp = func.Blueprint()
log = logging.getLogger(__name__)

_TERMS = (
    "Status terms: events with source=arm use API values Preview, GenerallyAvailable (=GA), Legacy, "
    "Deprecating (= docs 'Deprecated': existing deployments keep working, no new deployments) and "
    "Deprecated (= docs 'Retired': calls fail with HTTP 410). Events with source=docs use docs terms "
    "(GA/Preview/Legacy/Deprecated/Retired); source=retail_prices events are list-price changes."
)
_TIME = "Timestamps are UTC ISO 8601; event `date` and day windows use Asia/Shanghai calendar days."
_LIMITS = (
    "Default 200 events per page. Pass next_cursor as cursor with the same filters to read the next page; "
    "next_cursor=null marks the end. If the cursor expires after a data update or date change, restart without it."
    " Default response budget is 65536 UTF-8 bytes. Large price events can span pages: concatenate changes by "
    "event id and changes_offset until changes_total is reached; do not discard repeated event ids. "
    "collection.complete=false means one or more sources are missing, stale, running or failed, not no changes."
)
_PRICES = "Token prices are USD per 1M tokens (retail list price, Anthropic/Marketplace models not included)."


def _prop(name: str, kind: str, description: str, required: bool = False) -> dict[str, Any]:
    return {"propertyName": name, "propertyType": kind, "description": description, "isRequired": required}


_CATEGORY = _prop("category", "string", "Optional filter: 'lifecycle' or 'price'.")
_BILLING = _prop(
    "billing",
    "string",
    "Optional filter: 'token' (GlobalStandard, DataZoneStandard, Standard, Batch, DeveloperTier) or "
    "'provisioned' (*ProvisionedManaged / PTU).",
)
_DAYS = _prop("days", "integer", f"Window length in days, 1-{MAX_DAYS}. Default {DEFAULT_DAYS}.")
_LIMIT = _prop("limit", "integer", f"Events per page, 1-{MAX_LIMIT}. Default {DEFAULT_LIMIT}.")
_CURSOR = _prop("cursor", "string", "Optional next_cursor from the previous page; keep the same filters.")
_MAX_BYTES = _prop("max_bytes", "integer", "Response budget in UTF-8 bytes, 16384-1048576; default 65536.")


def _properties(*props: Mapping[str, Any]) -> str:
    return json.dumps(list(props))


def _arguments(context: Any) -> dict[str, Any]:
    payload = json.loads(context) if isinstance(context, (str, bytes)) else context
    return dict((payload or {}).get("arguments") or {})


def _text(args: Mapping[str, Any], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _days(args: Mapping[str, Any]) -> int:
    return _integer(args, "days", DEFAULT_DAYS)


def _integer(args: Mapping[str, Any], name: str, default: int) -> int:
    value = args.get(name)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _page(args: Mapping[str, Any]) -> dict[str, Any]:
    return {"limit": _integer(args, "limit", DEFAULT_LIMIT), "cursor": _text(args, "cursor"),
            "max_bytes": _integer(args, "max_bytes", DEFAULT_RESPONSE_BYTES)}


def _run(context: Any, call: Callable[[dict[str, Any]], Any]) -> str:
    try:
        result = call(_arguments(context))
    except (ValueError, service.NotReady) as exc:
        result = {"error": str(exc)}
    except Exception:  # noqa: BLE001 - details go to logs, not to callers
        log.exception("MCP tool failed")
        result = {"error": "internal error"}
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


@bp.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_today_changes",
    description=(
        "Changes to Microsoft Foundry models dated today (Asia/Shanghai): observed catalog changes (new models or "
        "versions, Preview->GA, deprecation, retirement date changes, removals), retirements/auto-upgrades scheduled "
        f"for today, and retail price changes. Grouped by model. {_TERMS} {_TIME} {_LIMITS}"
    ),
    tool_properties=_properties(_CATEGORY, _BILLING, _LIMIT, _CURSOR, _MAX_BYTES),
)
def mcp_get_today_changes(context) -> str:
    return _run(
        context,
        lambda a: service.changes("today", category=_text(a, "category"), billing=_text(a, "billing"), **_page(a)),
    )


@bp.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_upcoming_changes",
    description=(
        "Planned Microsoft Foundry model changes in the next N days, starting tomorrow (Asia/Shanghai): "
        "retirements (field inference/fine_tune), SKU deprecations and auto-upgrade start dates, from the ARM "
        "catalog (source=arm) and the official retirement schedule page when it adds dates ARM lacks "
        f"(source=docs), plus announced future retail prices when provided (source=retail_prices). {_TERMS} {_TIME} {_LIMITS}"
    ),
    tool_properties=_properties(_DAYS, _BILLING, _LIMIT, _CURSOR, _MAX_BYTES),
)
def mcp_get_upcoming_changes(context) -> str:
    return _run(context, lambda a: service.changes("upcoming", days=_days(a), billing=_text(a, "billing"), **_page(a)))


@bp.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_past_changes",
    description=(
        "Microsoft Foundry model changes in the past N days, excluding today (Asia/Shanghai): observed catalog "
        "changes, scheduled retirements whose date has passed, and retail price changes. Every change is listed "
        f"(not netted). {_TERMS} {_TIME} {_LIMITS} {_PRICES}"
    ),
    tool_properties=_properties(_DAYS, _CATEGORY, _BILLING, _LIMIT, _CURSOR, _MAX_BYTES),
)
def mcp_get_past_changes(context) -> str:
    return _run(
        context,
        lambda a: service.changes(
            "past", days=_days(a), category=_text(a, "category"), billing=_text(a, "billing"), **_page(a)
        ),
    )


@bp.mcp_tool_trigger(
    arg_name="context",
    tool_name="search_models",
    description=(
        "Search the current Microsoft Foundry model catalog (latest daily snapshot). Returns one item per "
        "format/name/version with dominant status, region count, billing types and earliest inference retirement. "
        f"Filters apply per region. {_TERMS} {_TIME} At most 200 items; `truncated` marks more."
    ),
    tool_properties=_properties(
        _prop("vendor", "string", "Optional model format / vendor, e.g. 'OpenAI', 'DeepSeek', 'Anthropic'."),
        _prop(
            "status",
            "string",
            "Optional API status: Preview, GenerallyAvailable, Legacy, Deprecating or Deprecated.",
        ),
        _prop("region", "string", "Optional ARM region name, e.g. 'eastus2'."),
        _BILLING,
        _prop("name_contains", "string", "Optional case-insensitive substring of the model name."),
    ),
)
def mcp_search_models(context) -> str:
    return _run(
        context,
        lambda a: service.models(
            **{name: _text(a, name) for name in ("vendor", "status", "region", "billing", "name_contains")}
        ),
    )


@bp.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_model",
    description=(
        "Details of one Microsoft Foundry model: every matching version with regions grouped by identical "
        "lifecycle status, retirement dates, replacement and SKUs (deployment types). Suggests similar names when "
        f"nothing matches. {_TERMS} {_TIME}"
    ),
    tool_properties=_properties(
        _prop("name", "string", "Exact model name (case-insensitive), e.g. 'gpt-4o'.", required=True),
        _prop("version", "string", "Optional model version, e.g. '2024-11-20'."),
        _prop("vendor", "string", "Optional model format / vendor, e.g. 'OpenAI'."),
    ),
)
def mcp_get_model(context) -> str:
    return _run(
        context,
        lambda a: service.model(name=_text(a, "name") or "", version=_text(a, "version"), vendor=_text(a, "vendor")),
    )


@bp.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_model_prices",
    description=(
        "Current retail token prices of a Microsoft Foundry model, one row per meter (deployment, tier, context, "
        "token type) with the regions sharing that price. `model` is matched to the ARM model name first "
        f"(match=arm_model), otherwise as a substring of the price list name (match=name_contains). {_PRICES} "
        "At most 200 rows; `truncated` marks more."
    ),
    tool_properties=_properties(
        _prop("model", "string", "ARM model name such as 'gpt-5.4', or part of a price list name.", required=True),
        _prop("region", "string", "Optional ARM region name, e.g. 'eastus2'."),
        _prop("deployment", "string", "Optional: 'global', 'datazone' or 'regional'."),
    ),
)
def mcp_get_model_prices(context) -> str:
    return _run(
        context,
        lambda a: service.prices(
            model=_text(a, "model") or "", region=_text(a, "region"), deployment=_text(a, "deployment")
        ),
    )
