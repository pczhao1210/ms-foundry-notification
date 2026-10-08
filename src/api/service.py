"""Connects storage to `core.query`; REST and MCP adapters call only these functions."""
from __future__ import annotations

import threading
import time
from typing import Any

from core import config, query
from core import events as ev

SNAPSHOT_TTL_SECONDS = 600
STATUS_TTL_SECONDS = 15


class NotReady(RuntimeError):
    """No snapshot has been collected yet."""


_lock = threading.Lock()
_store: Any = None
_snapshots: dict[str, tuple[float, dict[str, Any] | None]] = {}
_statuses: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _get_store() -> Any:
    global _store
    with _lock:
        if _store is None:
            from core.store import connect

            _store = connect(config.load())
        return _store


def _snapshot(kind: str) -> dict[str, Any]:
    now = time.monotonic()
    cached = _snapshots.get(kind)
    if cached and cached[0] > now:
        data = cached[1]
    else:
        data = _get_store().latest_snapshot(kind)
        _snapshots[kind] = (now + SNAPSHOT_TTL_SECONDS, data)
    if data is None:
        raise NotReady(f"no {kind} snapshot has been collected yet")
    return data


def _health(kinds: tuple[str, ...], served_at: dict[str, str] | None = None) -> dict[str, Any]:
    now = time.monotonic()
    statuses = {}
    for kind in kinds:
        cached = _statuses.get(kind)
        if not cached or cached[0] <= now:
            cached = (now + STATUS_TTL_SECONDS, _get_store().source_status(kind))
            _statuses[kind] = cached
        statuses[kind] = cached[1]
    return query.collection_health(statuses, today=ev.local_today(), served_at=served_at)


def changes(
    scope: str, *, days: int = query.DEFAULT_DAYS, category: str | None = None, billing: str | None = None,
    limit: int = query.DEFAULT_LIMIT, cursor: str | None = None,
    max_bytes: int = query.DEFAULT_RESPONSE_BYTES,
) -> dict[str, Any]:
    today = ev.local_today()
    start, end = query.date_window(scope, today, days)
    events = _get_store().read_events(start, end, category=category, scheduled_only=scope == "upcoming")
    result = query.query_changes(events, scope=scope, today=today, days=days, category=category, billing=billing,
                                 limit=limit, cursor=cursor, max_bytes=max_bytes)
    kinds = ("prices",) if category == "price" else ("arm", "docs") if category == "lifecycle" else ("arm", "docs", "prices")
    result["collection"] = _health(kinds)
    if query.response_size(result) > max_bytes:
        raise ValueError("collection metadata exceeds max_bytes; increase max_bytes")
    return result


def models(**filters: Any) -> dict[str, Any]:
    result = query.search_models(_snapshot("arm"), **filters)
    result["collection"] = _health(("arm",), {"arm": result["collected_at"]})
    return result


def model(**filters: Any) -> dict[str, Any]:
    result = query.get_model(_snapshot("arm"), **filters)
    result["collection"] = _health(("arm",), {"arm": result["collected_at"]})
    return result


def prices(**filters: Any) -> dict[str, Any]:
    try:
        arm = _snapshot("arm")
    except NotReady:
        arm = None
    result = query.get_prices(_snapshot("prices"), arm, **filters)
    result["collection"] = _health(("prices",), {"prices": result["collected_at"]})
    return result
