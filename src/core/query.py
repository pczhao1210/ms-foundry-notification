"""Queries shared by the REST endpoints and MCP tools: change windows plus model/price catalog lookups."""
from __future__ import annotations

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
) -> dict[str, Any]:
    """Return every event in the window grouped by model (or price model / meter); changes are listed, not netted."""
    start, end = date_window(scope, today, days)
    _check_choice("category", category, CATEGORIES)
    _check_choice("billing", billing, BILLINGS)
    _check_limit(limit)

    unique = {event["id"]: event for event in events}
    selected = [
        dict(event)
        for event in unique.values()
        if start <= event["date"] <= end and _matches(event, category, billing)
    ]
    _link_planned_retirements(selected)
    groups, truncated = _limit(_group(selected, newest_first=scope != "upcoming"), limit)
    return {
        "scope": scope,
        "timezone": ev.LOCAL_TZ_NAME,
        "from": start,
        "to": end,
        "filters": {"category": category, "billing": billing},
        "total_events": len(selected),
        "returned_events": sum(len(group["events"]) for group in groups),
        "truncated": truncated,
        "summary": dict(sorted(Counter(event["type"] for event in selected).items())),
        "unparsed_price_events": sum(1 for event in selected if event.get("unparsed")),
        "groups": groups,
    }


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


def _limit(groups: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    result: list[dict[str, Any]] = []
    remaining = limit
    for group in groups:
        if remaining == 0:
            return result, True
        if len(group["events"]) > remaining:
            result.append({**group, "events": group["events"][:remaining], "truncated": True})
            return result, True
        result.append(group)
        remaining -= len(group["events"])
    return result, False


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
