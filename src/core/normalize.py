"""Normalize raw ARM `locations/models` responses into a per-region model snapshot."""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from .events import to_utc_iso

SCHEMA_VERSION = 1
INCLUDED_KINDS = frozenset({"AIServices", "MAI"})


def model_key(fmt: str, name: str, version: str) -> str:
    return f"{fmt}|{name}|{version}"


def model_ref(model: Mapping[str, Any]) -> dict[str, str]:
    return {"format": model["format"], "name": model["name"], "version": model["version"]}


def sku_ref(sku: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return {"name": sku["name"], "scope": sku["scope"]} if sku else None


def sku_billing(sku_name: str) -> str:
    return "provisioned" if sku_name.endswith("ProvisionedManaged") else "token"


def billing_of(*region_maps: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return sorted(
        {sku["billing"] for regions in region_maps for record in regions.values() for sku in record["skus"].values()}
    )


def normalize_models(
    raw_by_region: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    collected_at: str,
    failed_regions: Iterable[str] = (),
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a snapshot; failed regions reuse the previous snapshot's data (marked stale) to avoid false removals."""
    models: dict[str, dict[str, Any]] = {}
    fresh: set[str] = set()
    for region_name, items in raw_by_region.items():
        region = region_name.lower()
        fresh.add(region)
        for item in items:
            if item.get("kind") in INCLUDED_KINDS:
                _add_model(models, region, item["model"])

    failed = {region.lower() for region in failed_regions} - fresh
    stale, missing = _carry_forward(models, failed, previous)

    for model in models.values():
        model["regions"] = dict(sorted(model["regions"].items()))
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": to_utc_iso(collected_at),
        "regions": sorted(fresh | set(stale)),
        "stale_regions": stale,
        "failed_regions": missing,
        "models": dict(sorted(models.items())),
    }


def _add_model(models: dict[str, dict[str, Any]], region: str, raw: Mapping[str, Any]) -> None:
    key = model_key(raw["format"], raw["name"], raw["version"])
    model = models.get(key)
    if model is None:
        model = models[key] = {
            "format": raw["format"],
            "name": raw["name"],
            "version": raw["version"],
            "is_marketplace_required": bool(raw.get("isMarketplaceRequired")),
            "created_at": to_utc_iso((raw.get("systemData") or {}).get("createdAt")),
            "regions": {},
        }
    record = _region_record(raw)
    existing = model["regions"].get(region)
    if existing is None:
        model["regions"][region] = record
        return
    # The same model can be listed under both AIServices and MAI kinds.
    for key_, sku in record["skus"].items():
        existing["skus"].setdefault(key_, sku)
    existing["skus"] = dict(sorted(existing["skus"].items()))


def _region_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    deprecation = raw.get("deprecation") or {}
    skus: dict[str, dict[str, Any]] = {}
    for sku in raw.get("skus") or []:
        scope = sku.get("scope")
        # The same SKU name appears twice for Base and Finetune scopes.
        skus[f"{sku['name']}|{scope or ''}"] = {
            "name": sku["name"],
            "scope": scope,
            "billing": sku_billing(sku["name"]),
            "status": sku.get("lifecycleStatus"),
            "retirement": to_utc_iso(sku.get("deprecationDate")),
        }
    return {
        "status": raw.get("lifecycleStatus"),
        "inference_retirement": to_utc_iso(deprecation.get("inference")),
        "fine_tune_retirement": to_utc_iso(deprecation.get("fineTune")),
        "replacement": _replacement(raw.get("replacementConfig")),
        "skus": dict(sorted(skus.items())),
    }


def _replacement(config: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not config or not config.get("targetModelName"):
        return None
    return {
        "model": config["targetModelName"],
        "version": config.get("targetModelVersion"),
        "auto_upgrade_start": to_utc_iso(config.get("autoUpgradeStartDate")),
    }


def _carry_forward(
    models: dict[str, dict[str, Any]],
    failed: set[str],
    previous: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
    stale: list[str] = []
    missing: list[str] = []
    previous_regions = set(previous["regions"]) if previous else set()
    for region in sorted(failed):
        if region not in previous_regions:
            missing.append(region)
            continue
        stale.append(region)
        for key, prev_model in previous["models"].items():
            record = prev_model["regions"].get(region)
            if record is None:
                continue
            model = models.get(key)
            if model is None:
                model = models[key] = {**{k: v for k, v in prev_model.items() if k != "regions"}, "regions": {}}
            model["regions"][region] = copy.deepcopy(record)
    return stale, missing
