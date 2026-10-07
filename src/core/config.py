"""Runtime settings read from environment variables (set by infra/ in Azure, by local.settings.json locally)."""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MODELS_API_VERSION = "2026-09-01"


@dataclass(frozen=True)
class Settings:
    subscription_id: str | None
    blob_endpoint: str | None
    table_endpoint: str | None
    models_api_version: str
    regions: tuple[str, ...]
    use_storage_emulator: bool = False

    def require(self, name: str) -> str:
        value = getattr(self, name)
        if not value:
            raise RuntimeError(f"setting {name} is not configured (see core/config.py)")
        return value


def load() -> Settings:
    regions = os.environ.get("FOUNDRY_REGIONS", "")
    return Settings(
        subscription_id=os.environ.get("FOUNDRY_SUBSCRIPTION_ID") or None,
        blob_endpoint=os.environ.get("STORAGE_BLOB_ENDPOINT") or None,
        table_endpoint=os.environ.get("STORAGE_TABLE_ENDPOINT") or None,
        models_api_version=os.environ.get("MODELS_API_VERSION") or DEFAULT_MODELS_API_VERSION,
        regions=tuple(region.strip().lower() for region in regions.split(",") if region.strip()),
        use_storage_emulator=os.environ.get("STORAGE_USE_EMULATOR", "").strip().lower() == "true",
    )
