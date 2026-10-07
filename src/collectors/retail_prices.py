"""Collect Foundry Models meters from the public Azure Retail Prices API (no authentication)."""
from __future__ import annotations

from typing import Any

from . import _http

PRICES_HOST = "prices.azure.com"
PRICES_URL = f"https://{PRICES_HOST}/api/retail/prices"
FILTER = "serviceName eq 'Foundry Models'"


def collect() -> list[dict[str, Any]]:
    http = _http.session()
    url: str | None = PRICES_URL
    params: dict[str, str] | None = {"$filter": FILTER}
    items: list[dict[str, Any]] = []
    for _ in range(_http.MAX_PAGES):
        if not url:
            return items
        if not _http.same_host(url, PRICES_HOST):
            raise ValueError("unexpected NextPageLink host")
        page = _http.get_json(http, url, params=params)
        items.extend(page.get("Items", []))
        url, params = page.get("NextPageLink"), None
    raise RuntimeError("too many Retail Prices pages")
