"""Structural diff of two consecutive snapshots into observed events (no text diff)."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from functools import partial
from typing import Any

from . import events as ev
from .normalize import billing_of, model_key, model_ref, sku_ref
from .price_parse import DIMENSION_VALUES, DIMENSIONS, TOKEN_UNIT, label_meters


def diff_models(prev: Mapping[str, Any], curr: Mapping[str, Any]) -> list[dict[str, Any]]:
    base = {
        "kind": ev.OBSERVED,
        "category": ev.LIFECYCLE,
        "source": "arm",
        "date": ev.local_date(curr["collected_at"]),
    }
    context = {"observed_at": curr["collected_at"], "baseline_at": prev["collected_at"]}
    # Regions without a baseline yesterday would otherwise report every model as newly added.
    skip = set(prev.get("failed_regions", ()))
    versions: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for model in prev["models"].values():
        versions[(model["format"], model["name"])].append(model)

    out = ev.RegionAggregator()
    for key in sorted(prev["models"].keys() | curr["models"].keys()):
        before, after = prev["models"].get(key), curr["models"].get(key)
        old_regions, new_regions = _comparable(before, skip), _comparable(after, skip)
        if not old_regions and not new_regions:
            continue
        ref = model_ref(before or after)
        emit = partial(
            _emit, out, {**base, "model": ref}, {"billing": billing_of(old_regions, new_regions), **context}
        )

        if not old_regions:
            existing = [
                {"version": m["version"], "status": _dominant_status(m)}
                for m in versions[(ref["format"], ref["name"])]
                if model_key(m["format"], m["name"], m["version"]) != key and _comparable(m, skip)
            ]
            details: dict[str, Any] = {
                "skus": sorted({sku["name"] for record in new_regions.values() for sku in record["skus"].values()})
            }
            if existing:
                details["existing_versions"] = sorted(existing, key=lambda v: v["version"])
            event_type = ev.NEW_VERSION if existing else ev.MODEL_ADDED
            for region, record in new_regions.items():
                emit(event_type, region, new=record["status"], details=details)
            continue

        if not new_regions:
            for region, record in old_regions.items():
                emit(ev.MODEL_REMOVED, region, old=record["status"])
            continue

        for region in sorted(old_regions.keys() - new_regions.keys()):
            emit(ev.REGION_REMOVED, region, old=old_regions[region]["status"])
        for region in sorted(new_regions.keys() - old_regions.keys()):
            emit(ev.REGION_ADDED, region, new=new_regions[region]["status"])
        for region in sorted(old_regions.keys() & new_regions.keys()):
            _diff_region(emit, region, old_regions[region], new_regions[region])
    return out.events()


def _comparable(model: Mapping[str, Any] | None, skip: set[str]) -> dict[str, Any]:
    if model is None:
        return {}
    return {region: record for region, record in model["regions"].items() if region not in skip}


def _dominant_status(model: Mapping[str, Any]) -> str | None:
    counts = Counter(record["status"] for record in model["regions"].values())
    return min(counts.items(), key=lambda item: (-item[1], item[0] or ""))[0] if counts else None


def _emit(
    out: ev.RegionAggregator,
    base: Mapping[str, Any],
    fields: Mapping[str, Any],
    event_type: str,
    region: str,
    *,
    sku: Mapping[str, Any] | None = None,
    field: str | None = None,
    old: Any = None,
    new: Any = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    identity = {**base, "type": event_type, "sku": sku_ref(sku), "field": field, "old": old, "new": new}
    extra = dict(fields)
    if sku:
        extra["billing"] = [sku["billing"]]
    if details:
        extra["details"] = details
    out.add(identity, region, **extra)


def _diff_region(emit: Any, region: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> None:
    if old["status"] != new["status"]:
        emit(ev.STATUS_CHANGED, region, field="status", old=old["status"], new=new["status"])
    for field in ("inference", "fine_tune"):
        attr = f"{field}_retirement"
        if old[attr] != new[attr]:
            emit(ev.RETIREMENT_DATE_CHANGED, region, field=field, old=old[attr], new=new[attr])
    if old["replacement"] != new["replacement"]:
        emit(ev.REPLACEMENT_CHANGED, region, field="replacement", old=old["replacement"], new=new["replacement"])

    old_skus, new_skus = old["skus"], new["skus"]
    for key in sorted(old_skus.keys() - new_skus.keys()):
        emit(ev.SKU_REMOVED, region, sku=old_skus[key], old=old_skus[key]["status"])
    for key in sorted(new_skus.keys() - old_skus.keys()):
        emit(ev.SKU_ADDED, region, sku=new_skus[key], new=new_skus[key]["status"])
    for key in sorted(old_skus.keys() & new_skus.keys()):
        before, after = old_skus[key], new_skus[key]
        if before["status"] != after["status"]:
            emit(ev.SKU_STATUS_CHANGED, region, sku=after, field="status", old=before["status"], new=after["status"])
        if before["retirement"] != after["retirement"]:
            emit(ev.RETIREMENT_DATE_CHANGED, region, sku=after, field="sku", old=before["retirement"], new=after["retirement"])


def diff_prices(prev: Mapping[str, Any], curr: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Merge same-direction changes across regions per meter, then per parsed model (product + SKU model part)."""
    base = {
        "kind": ev.OBSERVED,
        "category": ev.PRICE,
        "source": "retail_prices",
        "date": ev.local_date(curr["collected_at"]),
    }
    context = {"observed_at": curr["collected_at"], "baseline_at": prev["collected_at"]}
    old_prices, new_prices = prev["prices"], curr["prices"]
    labels = label_meters([*old_prices.values(), *new_prices.values()])
    meters: dict[str, dict[str, Any]] = {}
    for key in sorted(old_prices.keys() | new_prices.keys()):
        before, after = old_prices.get(key), new_prices.get(key)
        if before and after:
            if math.isclose(before["price"], after["price"], rel_tol=1e-9):
                continue
            event_type, direction = ev.PRICE_CHANGED, "up" if after["price"] > before["price"] else "down"
        elif after:
            event_type, direction = ev.PRICE_ADDED, None
        else:
            event_type, direction = ev.PRICE_REMOVED, None
        record = after or before
        meter = {"product": record["product"], "sku": record["sku"], "meter": record["meter"]}
        group = meters.setdefault(
            ev.canonical_json([event_type, direction, meter]),
            {"type": event_type, "direction": direction, "meter": meter, "record": record, "rows": []},
        )
        group["rows"].append(
            {
                "region": record["region"],
                "old": before["price"] if before else None,
                "new": after["price"] if after else None,
                "effective_start": after["effective_start"] if after else None,
            }
        )

    result: list[dict[str, Any]] = []
    families: dict[str, dict[str, Any]] = {}
    for group in meters.values():
        record = group["record"]
        dims = labels.get((record["product"], record["sku"]))
        if dims is None:
            result.append(_meter_event(base, group, context))
            continue
        identity = {
            **base,
            "type": group["type"],
            "price_model": {"product": record["product"], "name": dims["model"]},
            "direction": group["direction"],
        }
        family = families.setdefault(ev.canonical_json(identity), {"identity": identity, "meters": []})
        family["meters"].append(
            {"sku": record["sku"], **{d: dims[d] for d in DIMENSIONS}, **_summarize(group["rows"])}
        )
    result.extend(_model_event(family, context) for family in families.values())
    result.sort(key=ev.sort_key)
    return result


def _summarize(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: row["region"])
    olds = {row["old"] for row in rows}
    news = {row["new"] for row in rows}
    uniform = len(olds) == 1 and len(news) == 1
    old = next(iter(olds)) if uniform else None
    new = next(iter(news)) if uniform else None
    summary = {
        "regions": sorted({row["region"] for row in rows}),
        "old": old,
        "new": new,
        "change_pct": round((new - old) / old * 100, 2) if old and new is not None else None,
        "effective_start": max((row["effective_start"] for row in rows if row["effective_start"]), default=None),
    }
    if not uniform:
        summary["region_prices"] = [{"region": row["region"], "old": row["old"], "new": row["new"]} for row in rows]
    return summary


def _meter_event(base: Mapping[str, Any], group: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    identity = {**base, "type": group["type"], "meter": group["meter"], "direction": group["direction"]}
    record = group["record"]
    event = {
        "id": ev.event_id(identity),
        **identity,
        "billing": [record["billing"]],
        "unit": record["unit"],
        **_summarize(group["rows"]),
        **context,
    }
    if record["billing"] == "token":
        event["unparsed"] = True
    return event


def _model_event(family: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    identity = family["identity"]
    meters = sorted(family["meters"], key=lambda m: tuple(DIMENSION_VALUES[d].index(m[d]) for d in DIMENSIONS))
    regions = sorted({region for meter in meters for region in meter["regions"]})
    changes = []
    for meter in meters:
        change = {k: v for k, v in meter.items() if k != "effective_start"}
        if change["regions"] == regions:
            del change["regions"]
        changes.append(change)
    pcts = {meter["change_pct"] for meter in meters}
    return {
        "id": ev.event_id(identity),
        **identity,
        "billing": ["token"],
        "unit": TOKEN_UNIT,
        "regions": regions,
        "meter_count": len(meters),
        "change_pct": next(iter(pcts)) if len(pcts) == 1 else None,
        "effective_start": max((m["effective_start"] for m in meters if m["effective_start"]), default=None),
        **context,
        "changes": changes,
    }
