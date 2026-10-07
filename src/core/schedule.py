"""Derive scheduled lifecycle events from a snapshot and reconcile them with the stored schedule."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from . import events as ev
from .normalize import billing_of, model_ref, sku_ref


def build_schedule(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    base = {"kind": ev.SCHEDULED, "category": ev.LIFECYCLE, "source": "arm"}
    out = ev.RegionAggregator()
    for model in snapshot["models"].values():
        ref = model_ref(model)
        billing = billing_of(model["regions"])
        for region, record in model["regions"].items():
            replacement = record["replacement"]
            for field in ("inference", "fine_tune"):
                at = record[f"{field}_retirement"]
                if at:
                    details = {"replacement": replacement} if replacement else None
                    _emit(out, base, ref, ev.RETIRING, region, at, billing, field=field, details=details)
            if replacement and replacement["auto_upgrade_start"]:
                target = {"model": replacement["model"], "version": replacement["version"]}
                _emit(out, base, ref, ev.AUTO_UPGRADE_START, region, replacement["auto_upgrade_start"], billing,
                      details={"target": target})
            model_dates = {record["inference_retirement"], record["fine_tune_retirement"]}
            for sku in record["skus"].values():
                # SKU dates equal to a model-level retirement are already covered by RETIRING.
                if sku["retirement"] and sku["retirement"] not in model_dates:
                    _emit(out, base, ref, ev.SKU_DEPRECATING, region, sku["retirement"], [sku["billing"]], sku=sku)
    return out.events()


def _emit(
    out: ev.RegionAggregator,
    base: Mapping[str, Any],
    ref: Mapping[str, str],
    event_type: str,
    region: str,
    at: str,
    billing: list[str],
    *,
    field: str | None = None,
    sku: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    identity = {
        **base,
        "type": event_type,
        "date": ev.local_date(at),
        "model": ref,
        "sku": sku_ref(sku),
        "field": field,
    }
    extra: dict[str, Any] = {"effective_at": at, "billing": billing}
    if details:
        extra["details"] = details
    out.add(identity, region, **extra)


def reconcile_schedule(
    existing: Mapping[str, str],
    computed: Iterable[Mapping[str, Any]],
    today: str,
) -> tuple[list[Mapping[str, Any]], list[str]]:
    """Return (upserts, delete_ids) for `existing` (id -> date); entries dated today or earlier are frozen history."""
    computed = list(computed)
    computed_ids = {event["id"] for event in computed}
    upserts = [event for event in computed if event["date"] > today or event["id"] not in existing]
    deletes = sorted(event_id for event_id, day in existing.items() if day > today and event_id not in computed_ids)
    return upserts, deletes
