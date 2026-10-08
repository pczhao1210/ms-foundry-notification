"""Collect the ARM model catalog for every region that serves `Microsoft.CognitiveServices/locations/models`."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import _http

ARM_HOST = "management.azure.com"
ARM = f"https://{ARM_HOST}"
PROVIDER = "Microsoft.CognitiveServices"
# `global` answers 400 for this resource type.
SKIP_REGIONS = frozenset({"global"})

log = logging.getLogger(__name__)


def region_names(display_names: Iterable[str], locations: Iterable[Mapping[str, Any]]) -> list[str]:
    """Map provider display names ("East US 2") to ARM region names ("eastus2")."""
    by_display = {location["displayName"].casefold(): location["name"] for location in locations}
    names = {by_display.get(name.casefold(), name.replace(" ", "").lower()) for name in display_names}
    return sorted(names - SKIP_REGIONS)


class ArmModelsCollector:
    def __init__(self, subscription_id: str, api_version: str, credential: Any, *, max_workers: int = 8) -> None:
        self._subscription = subscription_id
        self._api_version = api_version
        self._credential = credential
        self._max_workers = max_workers

    def _get(self, http: Any, url: str) -> Any:
        if not _http.same_host(url, ARM_HOST):
            raise ValueError("refusing to send an ARM token to a non-ARM host")
        token = self._credential.get_token(f"{ARM}/.default").token
        return _http.get_json(http, url, headers={"Authorization": f"Bearer {token}"})

    def regions(self) -> list[str]:
        http = _http.session()
        provider = self._get(http, f"{ARM}/subscriptions/{self._subscription}/providers/{PROVIDER}?api-version=2021-04-01")
        resource = next(
            (rt for rt in provider["resourceTypes"] if rt["resourceType"].casefold() == "locations/models"), None
        )
        if resource is None:
            raise RuntimeError(f"{PROVIDER} does not expose locations/models")
        locations = self._get(http, f"{ARM}/subscriptions/{self._subscription}/locations?api-version=2022-12-01")
        return region_names(resource["locations"], locations["value"])

    def models(self, region: str) -> list[dict[str, Any]]:
        http = _http.session()
        url: str | None = (
            f"{ARM}/subscriptions/{self._subscription}/providers/{PROVIDER}/locations/{region}/models"
            f"?api-version={self._api_version}"
        )
        items: list[dict[str, Any]] = []
        for _ in range(_http.MAX_PAGES):
            if not url:
                return items
            page = self._get(http, url)
            if not isinstance(page, dict) or not isinstance(page.get("value"), list):
                raise ValueError(f"invalid Models API page for region {region}")
            items.extend(page["value"])
            url = page.get("nextLink")
        raise RuntimeError(f"too many pages for region {region}")

    def collect(self, regions: Iterable[str] = ()) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Return (models by region, failed regions); one failing region doesn't fail the run."""
        targets = sorted(set(regions) - SKIP_REGIONS) or self.regions()
        raw: dict[str, list[dict[str, Any]]] = {}
        failed: list[str] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {region: pool.submit(self.models, region) for region in targets}
            for region, future in futures.items():
                try:
                    raw[region] = future.result()
                except Exception:  # noqa: BLE001 - logged and reported as a failed region
                    log.warning("ARM models request failed for region %s", region, exc_info=True)
                    failed.append(region)
        return raw, failed
