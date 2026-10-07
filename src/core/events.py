"""Event schema shared by diff, schedule and query: type constants, IDs, dates and region aggregation."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ_NAME = "Asia/Shanghai"
LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)

OBSERVED = "observed"
SCHEDULED = "scheduled"
LIFECYCLE = "lifecycle"
PRICE = "price"

MODEL_ADDED = "MODEL_ADDED"
NEW_VERSION = "NEW_VERSION"
MODEL_REMOVED = "MODEL_REMOVED"
REGION_ADDED = "REGION_ADDED"
REGION_REMOVED = "REGION_REMOVED"
STATUS_CHANGED = "STATUS_CHANGED"
RETIREMENT_DATE_CHANGED = "RETIREMENT_DATE_CHANGED"
REPLACEMENT_CHANGED = "REPLACEMENT_CHANGED"
SKU_ADDED = "SKU_ADDED"
SKU_REMOVED = "SKU_REMOVED"
SKU_STATUS_CHANGED = "SKU_STATUS_CHANGED"

RETIRING = "RETIRING"
SKU_DEPRECATING = "SKU_DEPRECATING"
AUTO_UPGRADE_START = "AUTO_UPGRADE_START"

PRICE_CHANGED = "PRICE_CHANGED"
PRICE_ADDED = "PRICE_ADDED"
PRICE_REMOVED = "PRICE_REMOVED"


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_utc_iso(value: str | None) -> str | None:
    if not value:
        return None
    return parse_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date(value: str) -> str:
    return parse_utc(value).astimezone(LOCAL_TZ).date().isoformat()


def local_today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ).date().isoformat()


def shift_date(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_id(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]


def sort_key(event: Mapping[str, Any]) -> tuple[str, str, str]:
    return (event["date"], event["type"], event["id"])


class RegionAggregator:
    """Merges per-region changes sharing the same identity into one event with a `regions` list."""

    def __init__(self) -> None:
        self._events: dict[str, dict[str, Any]] = {}

    def add(self, identity: Mapping[str, Any], region: str, **fields: Any) -> None:
        key = canonical_json(identity)
        event = self._events.get(key)
        if event is None:
            event = self._events[key] = {"id": event_id(identity), **identity, **fields, "regions": set()}
        event["regions"].add(region)

    def events(self) -> list[dict[str, Any]]:
        result = [{**event, "regions": sorted(event["regions"])} for event in self._events.values()]
        result.sort(key=sort_key)
        return result
