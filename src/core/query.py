"""Queries shared by the REST endpoints and MCP tools: change windows plus model/price catalog lookups."""
from __future__ import annotations

import base64
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from . import events as ev
from .normalize import billing_of
from .price_alias import map_price_models
from .price_parse import DIMENSION_VALUES, DIMENSIONS, TOKEN_UNIT, label_meters

MAX_DAYS = 30
DEFAULT_DAYS = 7
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
DEFAULT_RESPONSE_BYTES = 65_536
MIN_RESPONSE_BYTES = 16_384
MAX_RESPONSE_BYTES = 1_048_576
_METADATA_RESERVE_BYTES = 8192
STATUSES = ("Preview", "GenerallyAvailable", "Legacy", "Deprecating", "Deprecated")
DEPLOYMENTS = ("global", "datazone", "regional")
SCOPES = ("today", "past", "upcoming")
CATEGORIES = (ev.LIFECYCLE, ev.PRICE)
BILLINGS = ("token", "provisioned")

_LINK_WINDOW_DAYS = 7
_PLANNED_RETIREMENTS = frozenset({ev.RETIRING, ev.SKU_DEPRECATING})
_REMOVALS = frozenset({ev.MODEL_REMOVED, ev.SKU_REMOVED})
_STATUS_CHANGES = frozenset({ev.STATUS_CHANGED, ev.SKU_STATUS_CHANGED})
# Price events mapped to an ARM model carry `model` too and group under it; `price_model` stays on the event.
_SUBJECT_FIELDS = ("model", "price_model", "meter")
# Status meaning "no longer callable" per source: ARM `Deprecated`, docs `Retired`.
_RETIRED_STATUS = {"arm": "Deprecated", "docs": "Retired"}


def collection_health(
    statuses: Mapping[str, Mapping[str, Any] | None], *, today: str, served_at: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    sources = {}
    for name, saved in statuses.items():
        saved = saved or {}
        snapshot_at = (served_at or {}).get(name, saved.get("snapshot_at"))
        stale = not snapshot_at or ev.local_date(snapshot_at) != today or snapshot_at != saved.get("snapshot_at")
        state = saved.get("status", "unknown")
        if state == "complete" and stale:
            state = "stale"
        sources[name] = {
            "status": state,
            "snapshot_at": snapshot_at,
            "last_attempt_at": saved.get("last_attempt_at"),
            "last_success_at": saved.get("last_success_at"),
            "stale_regions": saved.get("stale_regions", []),
            "failed_regions": saved.get("failed_regions", []),
        }
    return {"date": today, "timezone": ev.LOCAL_TZ_NAME,
            "complete": bool(sources) and all(source["status"] == "complete" for source in sources.values()),
            "sources": sources}


def date_window(scope: str, today: str, days: int = DEFAULT_DAYS) -> tuple[str, str]:
    """Inclusive Asia/Shanghai date range; today, past and upcoming never overlap."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    if scope == "today":
        return today, today
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    if scope == "past":
        return ev.shift_date(today, -days), ev.shift_date(today, -1)
    return ev.shift_date(today, 1), ev.shift_date(today, days)


def query_changes(
    events: Iterable[Mapping[str, Any]],
    *,
    scope: str,
    today: str,
    days: int = DEFAULT_DAYS,
    category: str | None = None,
    billing: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    max_bytes: int = DEFAULT_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Return every event in the window grouped by model (or price model / meter); changes are listed, not netted."""
    start, end = date_window(scope, today, days)
    _check_choice("category", category, CATEGORIES)
    _check_choice("billing", billing, BILLINGS)
    _check_limit(limit)
    if not MIN_RESPONSE_BYTES <= max_bytes <= MAX_RESPONSE_BYTES:
        raise ValueError(f"max_bytes must be between {MIN_RESPONSE_BYTES} and {MAX_RESPONSE_BYTES}")

    unique = {event["id"]: event for event in events}
    selected = [
        dict(event)
        for event in unique.values()
        if start <= event["date"] <= end and _matches(event, category, billing)
    ]
    _link_planned_retirements(selected)
    for event in selected:
        if "related_event_ids" in event:
            event["related_event_ids"] = sorted(set(event["related_event_ids"]))
    ordered = _group(selected, newest_first=scope != "upcoming")
    fingerprint = ev.event_id({"scope": scope, "from": start, "to": end, "category": category,
                               "billing": billing, "groups": ordered})
    offset, detail_offset = _cursor_offset(cursor, fingerprint, len(selected))
    result = {
        "scope": scope,
        "timezone": ev.LOCAL_TZ_NAME,
        "from": start,
        "to": end,
        "filters": {"category": category, "billing": billing},
        "total_events": len(selected),
        "returned_events": 0,
        "truncated": False,
        "next_cursor": None,
        "max_bytes": max_bytes,
        "byte_limited": False,
        "summary": dict(sorted(Counter(event["type"] for event in selected).items())),
        "unparsed_price_events": sum(1 for event in selected if event.get("unparsed")),
        "groups": [],
    }
    budget = max_bytes - response_size(result) - _METADATA_RESERVE_BYTES
    groups, offset, detail_offset, byte_limited = _limit(ordered, limit, offset, detail_offset, budget)
    result.update(groups=groups, returned_events=sum(len(group["events"]) for group in groups),
                  truncated=offset < len(selected), byte_limited=byte_limited,
                  next_cursor=_encode_cursor(fingerprint, offset, detail_offset) if offset < len(selected) else None)
    return result


def _check_choice(name: str, value: str | None, choices: tuple[str, ...]) -> None:
    if value is not None and value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}")


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")


def _matches(event: Mapping[str, Any], category: str | None, billing: str | None) -> bool:
    if category and event["category"] != category:
        return False
    return not billing or billing in event.get("billing", ())


def _subject_field(event: Mapping[str, Any]) -> str:
    return next(field for field in _SUBJECT_FIELDS if event.get(field))


def _subject(event: Mapping[str, Any]) -> dict[str, Any]:
    field = _subject_field(event)
    return {"category": event["category"], field: event[field]}


def _link_planned_retirements(events: list[dict[str, Any]]) -> None:
    """Cross-reference a scheduled retirement with the removal/Deprecated status observed when it took effect."""
    by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_subject[ev.canonical_json(_subject(event))].append(event)
    for group in by_subject.values():
        planned = [
            e for e in group
            if e["kind"] == ev.SCHEDULED and e["type"] in _PLANNED_RETIREMENTS and e.get("field") != "fine_tune"
        ]
        realized = [
            e for e in group
            if e["kind"] == ev.OBSERVED
            and (
                e["type"] in _REMOVALS
                or (e["type"] in _STATUS_CHANGES and e.get("new") == _RETIRED_STATUS.get(e.get("source")))
            )
        ]
        for plan in planned:
            last_day = ev.shift_date(plan["date"], _LINK_WINDOW_DAYS)
            for observed in realized:
                if observed.get("sku") == plan.get("sku") and plan["date"] <= observed["date"] <= last_day:
                    plan.setdefault("related_event_ids", []).append(observed["id"])
                    observed.setdefault("related_event_ids", []).append(plan["id"])


def _chronological(event: Mapping[str, Any]) -> tuple[str, int, str, str]:
    return (event["date"], 0 if event["kind"] == ev.SCHEDULED else 1, event["type"], event["id"])


def _group(events: list[dict[str, Any]], *, newest_first: bool) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=_chronological):
        field = _subject_field(event)
        subject = {"category": event["category"], field: event[field]}
        key = ev.canonical_json(subject)
        compact = {k: v for k, v in event.items() if k not in ("category", field)}
        groups.setdefault(key, {"subject": subject, "events": []})["events"].append(compact)
    ordered = [groups[key] for key in sorted(groups)]
    if newest_first:
        ordered.sort(key=lambda group: group["events"][-1]["date"], reverse=True)
    else:
        ordered.sort(key=lambda group: group["events"][0]["date"])
    return ordered


def response_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _encode_cursor(fingerprint: str, offset: int, detail_offset: int = 0) -> str:
    payload = {"fingerprint": fingerprint, "offset": offset, "detail_offset": detail_offset}
    return base64.urlsafe_b64encode(ev.canonical_json(payload).encode()).decode()


def _cursor_offset(cursor: str | None, fingerprint: str, total: int) -> tuple[int, int]:
    if cursor is None:
        return 0, 0
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise ValueError("invalid cursor")
    try:
        payload = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        offset = payload["offset"]
        detail_offset = payload.get("detail_offset", 0)
        expected = payload["fingerprint"]
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("invalid cursor") from None
    if expected != fingerprint:
        raise ValueError("cursor expired or query changed; restart without cursor")
    if type(offset) is not int or not 0 <= offset < total or type(detail_offset) is not int or detail_offset < 0:
        raise ValueError("invalid cursor offset")
    if offset == 0 and detail_offset == 0:
        raise ValueError("invalid cursor offset")
    return offset, detail_offset


def _limit(
    groups: list[dict[str, Any]], limit: int, offset: int, detail_offset: int, budget: int,
) -> tuple[list[dict[str, Any]], int, int, bool]:
    entries = [(group, event) for group in groups for event in group["events"]]
    result: list[dict[str, Any]] = []
    returned = 0
    while offset < len(entries) and returned < limit:
        group, event = entries[offset]
        changes = event.get("changes", [])
        if detail_offset and detail_offset >= len(changes):
            raise ValueError("invalid cursor detail offset")

        def candidate(part: Mapping[str, Any]) -> list[dict[str, Any]]:
            same = bool(result) and result[-1]["subject"] == group["subject"]
            events = [*result[-1]["events"], part] if same else [part]
            page_group = {"subject": group["subject"], "events": events}
            if len(events) < len(group["events"]) or any("changes_total" in item for item in events):
                page_group["truncated"] = True
            return [*(result[:-1] if same else result), page_group]

        def fragment(count: int) -> dict[str, Any]:
            end = detail_offset + count
            return {**event, "changes": changes[detail_offset:end], "changes_offset": detail_offset,
                    "changes_total": len(changes), "details_truncated": end < len(changes)}

        whole = fragment(len(changes) - detail_offset) if detail_offset else event
        page = candidate(whole)
        if response_size(page) <= budget:
            result = page
            offset, detail_offset = offset + 1, 0
            returned += 1
            continue
        low, high = 0, len(changes) - detail_offset
        while low < high:
            middle = (low + high + 1) // 2
            if response_size(candidate(fragment(middle))) <= budget:
                low = middle
            else:
                high = middle - 1
        if low:
            result = candidate(fragment(low))
            detail_offset += low
            if detail_offset == len(changes):
                offset, detail_offset = offset + 1, 0
        elif not result:
            raise ValueError(f"event {event['id']} cannot fit in max_bytes; increase max_bytes")
        return result, offset, detail_offset, True
    return result, offset, detail_offset, False


def search_models(
    arm: Mapping[str, Any],
    *,
    vendor: str | None = None,
    status: str | None = None,
    region: str | None = None,
    billing: str | None = None,
    name_contains: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Current catalog, one item per (format, name, version); filters apply per region."""
    _check_choice("status", status, STATUSES)
    _check_choice("billing", billing, BILLINGS)
    _check_limit(limit)
    region = region.lower() if region else None
    items = []
    for model in arm["models"].values():
        if vendor and model["format"].casefold() != vendor.casefold():
            continue
        if name_contains and name_contains.casefold() not in model["name"].casefold():
            continue
        regions = {
            name: record
            for name, record in model["regions"].items()
            if (not region or name == region)
            and (not status or record["status"] == status)
            and (not billing or any(sku["billing"] == billing for sku in record["skus"].values()))
        }
        if regions:
            items.append(_model_summary(model, regions))
    return {
        "collected_at": arm["collected_at"],
        "filters": {
            "vendor": vendor,
            "status": status,
            "region": region,
            "billing": billing,
            "name_contains": name_contains,
        },
        "total": len(items),
        "truncated": len(items) > limit,
        "models": items[:limit],
    }


def _model_summary(model: Mapping[str, Any], regions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(record["status"] for record in regions.values())
    retirements = [record["inference_retirement"] for record in regions.values() if record["inference_retirement"]]
    replacements = {
        ev.canonical_json(record["replacement"]): record["replacement"]
        for record in regions.values()
        if record["replacement"]
    }
    summary: dict[str, Any] = {
        "format": model["format"],
        "name": model["name"],
        "version": model["version"],
        "status": min(statuses.items(), key=lambda item: (-item[1], item[0] or ""))[0],
        "region_count": len(regions),
        "billing": billing_of(regions),
        "inference_retirement": min(retirements, default=None),
    }
    if len(statuses) > 1:
        summary["status_counts"] = {str(status): count for status, count in sorted(statuses.items(), key=str)}
    if replacements:
        summary["replacement"] = [replacements[key] for key in sorted(replacements)]
    if model["is_marketplace_required"]:
        summary["is_marketplace_required"] = True
    return summary


def get_model(
    arm: Mapping[str, Any], *, name: str, version: str | None = None, vendor: str | None = None
) -> dict[str, Any]:
    """All matching versions with regions grouped by identical lifecycle/SKU records."""
    if not name or not name.strip():
        raise ValueError("name is required")
    needle = name.strip().casefold()
    matches = [
        model
        for model in arm["models"].values()
        if model["name"].casefold() == needle
        and (not version or model["version"] == version)
        and (not vendor or model["format"].casefold() == vendor.casefold())
    ]
    result: dict[str, Any] = {
        "collected_at": arm["collected_at"],
        "query": {"name": name, "version": version, "vendor": vendor},
        "found": bool(matches),
        "models": [_model_detail(model) for model in matches],
    }
    if not matches:
        result["suggestions"] = sorted(
            {model["name"] for model in arm["models"].values() if needle in model["name"].casefold()}
        )[:20]
    return result


def _model_detail(model: Mapping[str, Any]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    for region, record in model["regions"].items():
        view = {
            "status": record["status"],
            "inference_retirement": record["inference_retirement"],
            "fine_tune_retirement": record["fine_tune_retirement"],
            "replacement": record["replacement"],
            "skus": list(record["skus"].values()),
        }
        groups.setdefault(ev.canonical_json(view), {"regions": [], **view})["regions"].append(region)
    return {
        "format": model["format"],
        "name": model["name"],
        "version": model["version"],
        "is_marketplace_required": model["is_marketplace_required"],
        "created_at": model["created_at"],
        "region_count": len(model["regions"]),
        "availability": sorted(groups.values(), key=lambda group: (-len(group["regions"]), group["regions"])),
    }


def get_prices(
    prices: Mapping[str, Any],
    arm: Mapping[str, Any] | None,
    *,
    model: str,
    region: str | None = None,
    deployment: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Current token prices (USD / 1M tokens) of an ARM model name; falls back to a price-name substring match."""
    if not model or not model.strip():
        raise ValueError("model is required")
    _check_choice("deployment", deployment, DEPLOYMENTS)
    _check_limit(limit)
    region = region.lower() if region else None
    needle = model.strip().casefold()
    records = [record for record in prices["prices"].values() if record["billing"] == "token"]
    labels = label_meters(records)
    mapping = map_price_models({(p, dims["model"]) for (p, _), dims in labels.items() if dims}, arm)
    mapped = {family for family, target in mapping.items() if target and target["name"].casefold() == needle}

    rows: dict[tuple[str, str, float], dict[str, Any]] = {}
    for record in records:
        dims = labels.get((record["product"], record["sku"]))
        family = (record["product"], dims["model"]) if dims else None
        if mapped:
            if family not in mapped:
                continue
        elif needle not in (dims["model"] if dims else record["sku"]).casefold():
            continue
        if (region and record["region"] != region) or (deployment and (not dims or dims["deployment"] != deployment)):
            continue
        key = (record["product"], record["sku"], record["price"])
        row = rows.get(key)
        if row is None:
            row = rows[key] = {"product": record["product"]}
            if dims:
                row.update(price_model=dims["model"], model=mapping.get(family), **{d: dims[d] for d in DIMENSIONS})
            else:
                row["unparsed"] = True
            row.update(sku=record["sku"], meter=record["meter"], price=record["price"], regions=[])
        row["regions"].append(record["region"])

    ordered = sorted(rows.values(), key=_price_order)
    for row in ordered:
        row["regions"].sort()
    return {
        "collected_at": prices["collected_at"],
        "query": {"model": model, "region": region, "deployment": deployment},
        "match": "arm_model" if mapped else "name_contains",
        "unit": TOKEN_UNIT,
        "total": len(ordered),
        "truncated": len(ordered) > limit,
        "prices": ordered[:limit],
    }


def _price_order(row: Mapping[str, Any]) -> tuple[Any, ...]:
    if row.get("unparsed"):
        return (row["product"], row["sku"], (len(DIMENSIONS),), row["price"])
    dims = tuple(DIMENSION_VALUES[d].index(row[d]) for d in DIMENSIONS)
    return (row["product"], row["price_model"], dims, row["price"])
