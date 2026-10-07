"""Builders for synthetic ARM / Retail Prices payloads used by unit tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.normalize import normalize_models
from core.price_parse import normalize_prices

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def sku(name: str = "GlobalStandard", status: str = "GenerallyAvailable", retirement: str | None = None,
        scope: str = "Base") -> dict[str, Any]:
    item = {"name": name, "scope": scope, "lifecycleStatus": status}
    if retirement:
        item["deprecationDate"] = retirement
    return item


def arm_item(name: str = "gpt-x", version: str = "1", *, fmt: str = "OpenAI", status: str = "GenerallyAvailable",
             kind: str = "AIServices", skus: list[dict[str, Any]] | None = None, inference: str | None = None,
             fine_tune: str | None = None, replacement: dict[str, Any] | None = None) -> dict[str, Any]:
    model: dict[str, Any] = {
        "format": fmt,
        "name": name,
        "version": version,
        "lifecycleStatus": status,
        "skus": skus if skus is not None else [sku()],
    }
    deprecation = {k: v for k, v in (("inference", inference), ("fineTune", fine_tune)) if v}
    if deprecation:
        model["deprecation"] = deprecation
    if replacement:
        model["replacementConfig"] = replacement
    return {"kind": kind, "model": model}


def snapshot(raw_by_region: dict[str, list[dict[str, Any]]], collected_at: str = "2026-10-07T00:00:00Z",
             **kwargs: Any) -> dict[str, Any]:
    return normalize_models(raw_by_region, collected_at=collected_at, **kwargs)


def price_row(price: float, *, region: str = "eastus2", sku_name: str = "5.4 opt Gl", unit: str = "1M",
              product: str = "Azure OpenAI GPT5", effective: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    return {
        "type": "Consumption",
        "productName": product,
        "skuName": sku_name,
        "meterName": f"{sku_name} {'1M Tokens' if unit == '1M' else 'Tokens'}",
        "meterId": f"{sku_name}-{region}",
        "armRegionName": region,
        "unitOfMeasure": unit,
        "unitPrice": price,
        "tierMinimumUnits": 0.0,
        "effectiveStartDate": effective,
    }


def price_snapshot(rows: list[dict[str, Any]], collected_at: str = "2026-10-07T00:00:00Z") -> dict[str, Any]:
    return normalize_prices(rows, collected_at=collected_at)
