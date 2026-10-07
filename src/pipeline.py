"""Daily job: snapshot every source, diff against the previous snapshot and persist events.

Steps are isolated: a failing docs or prices step never blocks the ARM lifecycle data (and vice versa).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Protocol

from core import config
from core import events as ev
from core.diff import diff_models, diff_prices
from core.docs import build_docs_schedule, diff_docs, parse_schedule
from core.normalize import normalize_models
from core.price_alias import annotate_price_events
from core.price_parse import label_meters, normalize_prices
from core.schedule import build_schedule, reconcile_schedule

log = logging.getLogger(__name__)

BACKFILL_DAYS = 30
_MAX_LOGGED_NAMES = 50


class Sources(Protocol):
    def arm_models(self) -> tuple[dict[str, list[dict[str, Any]]], list[str]]: ...
    def docs_markdown(self) -> str: ...
    def docs_history(self, since: str) -> list[tuple[str, str]]: ...
    def retail_prices(self) -> list[dict[str, Any]]: ...


class LiveSources:
    def __init__(self, settings: config.Settings, credential: Any) -> None:
        self._settings = settings
        self._credential = credential

    def arm_models(self) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        from collectors.arm_models import ArmModelsCollector

        collector = ArmModelsCollector(
            self._settings.require("subscription_id"), self._settings.models_api_version, self._credential
        )
        return collector.collect(self._settings.regions)

    def docs_markdown(self) -> str:
        from collectors import docs_schedule

        return docs_schedule.fetch()

    def docs_history(self, since: str) -> list[tuple[str, str]]:
        from collectors import docs_schedule

        return docs_schedule.history(since)

    def retail_prices(self) -> list[dict[str, Any]]:
        from collectors import retail_prices

        return retail_prices.collect()


def live() -> tuple[Any, LiveSources]:
    from azure.identity import DefaultAzureCredential

    from core.store import connect

    settings = config.load()
    credential = DefaultAzureCredential()
    return connect(settings, credential), LiveSources(settings, credential)


def run_daily(store: Any, sources: Sources, now: datetime | None = None) -> dict[str, Any]:
    """Run all steps; failures are recorded in `report["failed"]` instead of aborting the remaining steps."""
    now = now or datetime.now(timezone.utc)
    collected_at = ev.to_utc_iso(now.isoformat())
    today = ev.local_today(now)
    report: dict[str, Any] = {"date": today, "collected_at": collected_at, "failed": []}
    context: dict[str, Any] = {"store": store, "sources": sources, "today": today, "collected_at": collected_at}
    steps: tuple[tuple[str, Callable[[dict[str, Any]], dict[str, Any]]], ...] = (
        ("arm", _arm_step),
        ("docs", _docs_step),
        ("prices", _prices_step),
    )
    for name, step in steps:
        try:
            report[name] = step(context)
        except Exception as exc:  # noqa: BLE001 - recorded and re-raised by the caller after all steps ran
            log.exception("daily %s step failed", name)
            report[name] = {"error": type(exc).__name__}
            report["failed"].append(name)
    log.info("daily run report: %s", json.dumps(report, ensure_ascii=False))
    return report


def _catalog(context: Mapping[str, Any]) -> dict[str, Any] | None:
    return context.get("arm") or context["store"].latest_snapshot("arm")


def _reconcile(store: Any, source: str, computed: list[dict[str, Any]], today: str) -> dict[str, int]:
    existing = store.schedule_dates(source)
    upserts, deletes = reconcile_schedule(existing, computed, today)
    store.apply_schedule(upserts, {event_id: existing[event_id] for event_id in deletes})
    return {"schedule_upserts": len(upserts), "schedule_deletes": len(deletes)}


def _arm_step(context: dict[str, Any]) -> dict[str, Any]:
    store, today = context["store"], context["today"]
    previous = store.latest_snapshot("arm", before=today)
    raw, failed = context["sources"].arm_models()
    if not raw:
        raise RuntimeError(f"no region returned models ({len(failed)} failed)")
    snapshot = normalize_models(raw, collected_at=context["collected_at"], failed_regions=failed, previous=previous)
    store.save_snapshot(today, "arm", snapshot)
    context["arm"] = snapshot
    # The first snapshot is only a baseline: everything in it would otherwise look newly added.
    observed = diff_models(previous, snapshot) if previous else []
    store.replace_observed(today, "arm", observed)
    return {
        "baseline_at": previous["collected_at"] if previous else None,
        "regions": len(snapshot["regions"]),
        "stale_regions": snapshot["stale_regions"],
        "failed_regions": snapshot["failed_regions"],
        "models": len(snapshot["models"]),
        "observed": len(observed),
        **_reconcile(store, "arm", build_schedule(snapshot), today),
    }


def _docs_step(context: dict[str, Any]) -> dict[str, Any]:
    store, today = context["store"], context["today"]
    arm = _catalog(context)
    previous = store.latest_snapshot("docs", before=today)
    snapshot = parse_schedule(context["sources"].docs_markdown(), collected_at=context["collected_at"])
    if not snapshot["rows"]:
        raise RuntimeError("no rows parsed from the retirement schedule; did the page layout change?")
    store.save_snapshot(today, "docs", snapshot)
    report: dict[str, Any] = {"baseline_at": previous["collected_at"] if previous else None, "rows": len(snapshot["rows"])}
    if previous:
        observed = diff_docs(previous, snapshot, arm)
        store.replace_observed(today, "docs", observed)
        report["observed"] = len(observed)
    else:
        report["backfilled"] = _backfill_docs(context, arm)
    report.update(_reconcile(store, "docs", build_docs_schedule(snapshot, arm), today))
    return report


def _backfill_docs(context: Mapping[str, Any], arm: Mapping[str, Any] | None) -> int | None:
    """First run only: replay the page's Git history so `past` queries include docs changes from day one."""
    today = context["today"]
    since = ev.shift_date(today, -BACKFILL_DAYS)
    try:
        versions = context["sources"].docs_history(ev.to_utc_iso(f"{since}T00:00:00+08:00"))
    except Exception:  # noqa: BLE001 - best effort (GitHub API rate limits); daily diffs work without it
        log.warning("docs history backfill skipped", exc_info=True)
        return None
    snapshots = [parse_schedule(markdown, collected_at=at) for at, markdown in versions]
    events = {
        event["id"]: event
        for prev, curr in zip(snapshots, snapshots[1:])
        for event in diff_docs(prev, curr, arm)
        if since <= event["date"] <= today
    }
    context["store"].add_observed(events.values())
    return len(events)


def _prices_step(context: dict[str, Any]) -> dict[str, Any]:
    store, today = context["store"], context["today"]
    arm = _catalog(context)
    previous = store.latest_snapshot("prices", before=today)
    snapshot = normalize_prices(context["sources"].retail_prices(), collected_at=context["collected_at"])
    if not snapshot["prices"]:
        raise RuntimeError("Retail Prices returned no Foundry Models meters")
    store.save_snapshot(today, "prices", snapshot)
    observed = annotate_price_events(diff_prices(previous, snapshot), arm) if previous else []
    store.replace_observed(today, "retail_prices", observed)
    new_unparsed = new_unparsed_meters(previous, snapshot)
    if new_unparsed:
        log.warning("new token meters the parser cannot read: %s", new_unparsed[:_MAX_LOGGED_NAMES])
    return {
        "baseline_at": previous["collected_at"] if previous else None,
        "meters": len(snapshot["prices"]),
        "observed": len(observed),
        "unparsed_events": sum(1 for event in observed if event.get("unparsed")),
        "unmapped_events": sum(1 for event in observed if event.get("mapped") is False),
        "new_unparsed_meters": len(new_unparsed),
    }


def new_unparsed_meters(previous: Mapping[str, Any] | None, snapshot: Mapping[str, Any]) -> list[str]:
    known = {(record["product"], record["sku"]) for record in previous["prices"].values()} if previous else set()
    labels = label_meters(snapshot["prices"].values())
    return sorted(f"{product} / {sku}" for (product, sku), dims in labels.items() if dims is None and (product, sku) not in known)
