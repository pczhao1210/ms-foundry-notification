"""Connects storage to `core.query`; REST and MCP adapters call only these functions."""
from __future__ import annotations

import threading
import time
from typing import Any

from core import config, query
from core import events as ev

SNAPSHOT_TTL_SECONDS = 600


class NotReady(RuntimeError):
    """No snapshot has been collected yet."""


_lock = threading.Lock()
_store: Any = None
_snapshots: dict[str, tuple[float, dict[str, Any] | None]] = {}


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


def changes(
    scope: str, *, days: int = query.DEFAULT_DAYS, category: str | None = None, billing: str | None = None
) -> dict[str, Any]:
    today = ev.local_today()
    start, end = query.date_window(scope, today, days)
    events = _get_store().read_events(start, end)
    return query.query_changes(events, scope=scope, today=today, days=days, category=category, billing=billing)


def models(**filters: Any) -> dict[str, Any]:
    return query.search_models(_snapshot("arm"), **filters)


def model(**filters: Any) -> dict[str, Any]:
    return query.get_model(_snapshot("arm"), **filters)


def prices(**filters: Any) -> dict[str, Any]:
    try:
        arm = _snapshot("arm")
    except NotReady:
        arm = None
    return query.get_prices(_snapshot("prices"), arm, **filters)
