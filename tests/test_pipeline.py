import copy
from datetime import datetime, timezone

import pytest

import pipeline
from builders import arm_item, price_row
from core import events as ev
from core.store import Store

DAY1 = datetime(2026, 10, 6, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 7, tzinfo=timezone.utc)
DOCS_HEADER = (
    "## Foundry Models sold by Azure\n\n### Azure OpenAI\n\n"
    "| Model | Version | Lifecycle | Retirement date | Replacement |\n|---|---|---|---|---|\n"
)


class FakeStore(Store):
    """In-memory stand-in with the same semantics as core.store.Store."""

    def __init__(self):
        self.snapshots = {}
        self.observed = {}
        self.schedule = {}
        self.pending = {}
        self.statuses = {}

    def source_status(self, kind):
        return copy.deepcopy(self.statuses.get(kind))

    def save_source_status(self, kind, status):
        self.statuses[kind] = copy.deepcopy(status)

    def stage_run(self, day, kind, batch):
        self.pending[(day, kind)] = copy.deepcopy(batch)

    def pending_runs(self, kind):
        return [(day, copy.deepcopy(batch)) for (day, name), batch in sorted(self.pending.items()) if name == kind]

    def discard_run(self, day, kind):
        del self.pending[(day, kind)]

    def save_snapshot(self, day, kind, data):
        self.snapshots[(day, kind)] = copy.deepcopy(data)

    def latest_snapshot(self, kind, before=None):
        days = [day for day, k in self.snapshots if k == kind and (before is None or day < before)]
        return copy.deepcopy(self.snapshots[(max(days), kind)]) if days else None

    def replace_observed(self, day, source, events):
        self.observed = {
            key: event
            for key, event in self.observed.items()
            if not (event["date"] == day and event["source"] == source)
        }
        self.add_observed(events)

    def add_observed(self, events):
        for event in events:
            self.observed[event["id"]] = copy.deepcopy(event)

    def schedule_dates(self, source, *, start=None):
        return {key: event["date"] for key, event in self.schedule.items()
                if event["source"] == source and (start is None or event["date"] >= start)}

    def apply_schedule(self, upserts, deletes):
        for key in deletes:
            del self.schedule[key]
        for event in upserts:
            self.schedule[event["id"]] = copy.deepcopy(event)


class FakeSources:
    def __init__(self, arm, docs, prices, history=None):
        self.arm, self.docs, self.prices, self.history = arm, docs, prices, history
        self.failed_regions = []
        self.fail = set()
        self.history_since = None

    def _check(self, name):
        if name in self.fail:
            raise RuntimeError(f"{name} unavailable")

    def arm_models(self):
        self._check("arm")
        return self.arm, self.failed_regions

    def docs_markdown(self):
        self._check("docs")
        return DOCS_HEADER + self.docs

    def docs_history(self, since):
        self.history_since = since
        if self.history is None:
            raise RuntimeError("rate limited")
        return [(at, DOCS_HEADER + rows) for at, rows in self.history]

    def retail_prices(self):
        self._check("prices")
        return self.prices


def _sources(**overrides):
    values = {
        "arm": {"eastus2": [arm_item("gpt-x", "1", inference="2026-12-01T00:00:00Z")]},
        "docs": "| gpt-x | 1 | GA | 2026-12-01 | — |\n| old | 1 | GA | 2026-11-01 | — |\n",
        "prices": [price_row(1.0)],
        **overrides,
    }
    return FakeSources(**values)


def _schedule(store):
    return {(e["source"], e["model"]["name"], e["date"]) for e in store.schedule.values()}


def test_first_run_stores_baselines_and_schedule_only():
    store, sources = FakeStore(), _sources()
    report = pipeline.run_daily(store, sources, DAY1)
    assert report["failed"] == []
    assert set(store.snapshots) == {("2026-10-06", kind) for kind in ("arm", "docs", "prices")}
    assert store.observed == {}
    assert report["docs"]["backfilled"] is None
    # The docs date for gpt-x equals the ARM date, so only the docs-only model is added from the page.
    assert _schedule(store) == {("arm", "gpt-x", "2026-12-01"), ("docs", "old", "2026-11-01")}


def test_second_run_records_changes_from_every_source():
    store, sources = FakeStore(), _sources()
    pipeline.run_daily(store, sources, DAY1)
    sources.arm = {"eastus2": [arm_item("gpt-x", "1", status="Deprecating", inference="2026-11-15T00:00:00Z")]}
    sources.docs = "| gpt-x | 1 | Deprecated | 2026-11-15 | — |\n| old | 1 | GA | 2026-11-01 | — |\n"
    sources.prices = [price_row(2.0)]

    report = pipeline.run_daily(store, sources, DAY2)

    assert report["failed"] == []
    assert {(e["source"], e["type"]) for e in store.observed.values()} == {
        ("arm", ev.STATUS_CHANGED),
        ("arm", ev.RETIREMENT_DATE_CHANGED),
        ("docs", ev.STATUS_CHANGED),
        ("docs", ev.RETIREMENT_DATE_CHANGED),
        ("retail_prices", ev.PRICE_CHANGED),
    }
    assert all(e["date"] == "2026-10-07" for e in store.observed.values())
    [price] = [e for e in store.observed.values() if e["category"] == "price"]
    assert price["mapped"] is False
    assert _schedule(store) == {("arm", "gpt-x", "2026-11-15"), ("docs", "old", "2026-11-01")}
    assert report["arm"]["schedule_deletes"] == 1

    observed = set(store.observed)
    pipeline.run_daily(store, sources, DAY2)
    assert set(store.observed) == observed


def test_failing_step_does_not_block_the_others():
    store, sources = FakeStore(), _sources()
    sources.fail = {"docs"}
    report = pipeline.run_daily(store, sources, DAY1)
    assert report["failed"] == ["docs"]
    assert report["docs"] == {"error": "RuntimeError"}
    assert {kind for _, kind in store.snapshots} == {"arm", "prices"}


def test_source_status_preserves_last_success_when_collection_fails():
    store, sources = FakeStore(), _sources()
    pipeline.run_daily(store, sources, DAY1)
    sources.fail = {"arm"}
    pipeline.run_daily(store, sources, DAY2)
    status = store.source_status("arm")
    assert status["status"] == "failed"
    assert status["last_attempt_at"] == "2026-10-07T00:00:00Z"
    assert status["last_success_at"] == "2026-10-06T00:00:00Z"
    assert status["snapshot_at"] == "2026-10-06T00:00:00Z"
    assert store.source_status("prices")["status"] == "complete"


def test_status_write_failure_is_reported_without_blocking_other_sources(monkeypatch):
    store, sources = FakeStore(), _sources()
    save_status = store.save_source_status

    def fail_completion(kind, status):
        if kind == "arm" and status["status"] == "complete":
            raise RuntimeError("status unavailable")
        save_status(kind, status)

    monkeypatch.setattr(store, "save_source_status", fail_completion)
    report = pipeline.run_daily(store, sources, DAY1)
    assert report["failed"] == ["arm"]
    assert report["arm"]["status_error"] == "RuntimeError"
    assert store.source_status("arm")["status"] == "collecting"
    assert store.source_status("prices")["status"] == "complete"


def test_arm_without_any_region_fails_but_prices_still_run():
    store, sources = FakeStore(), _sources(arm={})
    sources.failed_regions = ["eastus2"]
    report = pipeline.run_daily(store, sources, DAY1)
    assert report["failed"] == ["arm"]
    assert ("2026-10-06", "prices") in store.snapshots


@pytest.mark.parametrize("kind, source, event_type", [("arm", "arm", ev.STATUS_CHANGED),
                                                    ("docs", "docs", ev.STATUS_CHANGED),
                                                    ("prices", "retail_prices", ev.PRICE_CHANGED)])
def test_failed_event_write_is_replayed_before_the_next_day(monkeypatch, kind, source, event_type):
    store, sources = FakeStore(), _sources()
    pipeline.run_daily(store, sources, DAY1)
    if kind == "arm":
        sources.arm["eastus2"][0]["model"]["lifecycleStatus"] = "Deprecating"
    elif kind == "docs":
        sources.docs = sources.docs.replace("| gpt-x | 1 | GA |", "| gpt-x | 1 | Deprecated |")
    else:
        sources.prices = [price_row(2.0)]
    replace = store.replace_observed

    def fail_write(day, event_source, events):
        if event_source == source:
            raise RuntimeError("Table unavailable")
        replace(day, event_source, events)

    monkeypatch.setattr(store, "replace_observed", fail_write)
    assert pipeline.run_daily(store, sources, DAY2)["failed"] == [kind]
    assert store.latest_snapshot(kind)["collected_at"] == DAY1.isoformat().replace("+00:00", "Z")

    monkeypatch.setattr(store, "replace_observed", replace)
    report = pipeline.run_daily(store, sources, datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert report["failed"] == []
    [event] = [event for event in store.observed.values() if event["source"] == source]
    assert (event["type"], event["date"]) == (event_type, "2026-10-07")


@pytest.mark.parametrize("method", ["apply_schedule", "save_snapshot", "discard_run"])
def test_interrupted_commit_recovers_even_when_the_source_is_down(monkeypatch, method):
    store, sources = FakeStore(), _sources()
    pipeline.run_daily(store, sources, DAY1)
    sources.arm["eastus2"][0]["model"]["deprecation"]["inference"] = "2026-11-15T00:00:00Z"
    original = getattr(store, method)
    failed = False

    def interrupt(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("interrupted commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, method, interrupt)
    assert pipeline.run_daily(store, sources, DAY2)["failed"] == ["arm"]
    assert ("2026-10-07", "arm") in store.pending
    sources.fail = {"arm"}
    pipeline.run_daily(store, sources, datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert store.pending == {}
    [event] = [event for event in store.observed.values() if event["source"] == "arm"]
    assert (event["type"], event["date"]) == (ev.RETIREMENT_DATE_CHANGED, "2026-10-07")
    assert ("arm", "gpt-x", "2026-11-15") in _schedule(store)
    assert ("arm", "gpt-x", "2026-12-01") not in _schedule(store)
    status = store.source_status("arm")
    assert status["status"] == "failed"
    assert status["snapshot_at"] == status["last_success_at"] == "2026-10-07T00:00:00Z"


def test_first_docs_run_backfills_from_git_history():
    history = [
        ("2026-08-01T00:00:00Z", "| m | 1 | Preview | — | — |\n"),
        ("2026-09-20T02:00:00Z", "| m | 1 | GA | — | — |\n"),
        ("2026-10-01T03:00:00Z", "| m | 1 | GA | 2027-01-01 | — |\n"),
    ]
    store, sources = FakeStore(), _sources(history=history)
    report = pipeline.run_daily(store, sources, DAY1)
    assert sources.history_since == "2026-09-05T16:00:00Z"
    assert report["docs"]["backfilled"] == 2
    assert {(e["type"], e["date"]) for e in store.observed.values()} == {
        (ev.STATUS_CHANGED, "2026-09-20"),
        (ev.RETIREMENT_DATE_CHANGED, "2026-10-01"),
    }


def test_partial_rerun_preserves_todays_successful_region_and_events():
    store, sources = FakeStore(), _sources()
    sources.arm["westus"] = copy.deepcopy(sources.arm["eastus2"])
    pipeline.run_daily(store, sources, DAY1)
    sources.arm["westus"][0]["model"]["lifecycleStatus"] = "Deprecating"
    pipeline.run_daily(store, sources, DAY2)
    observed = copy.deepcopy(store.observed)
    del sources.arm["westus"]
    sources.failed_regions = ["westus"]

    report = pipeline.run_daily(store, sources, DAY2)

    assert report["failed"] == ["arm"]
    assert report["arm"]["status"] == "degraded"
    assert report["arm"]["stale_regions"] == ["westus"]
    assert store.observed == observed
    status = store.source_status("arm")
    assert status["status"] == "degraded" and status["stale_regions"] == ["westus"]
    assert store.latest_snapshot("arm")["models"]["OpenAI|gpt-x|1"]["regions"]["westus"]["status"] == "Deprecating"


@pytest.mark.parametrize("items", [[], [arm_item(kind="OpenAI")]])
def test_empty_region_does_not_publish_model_removals(items):
    store, sources = FakeStore(), _sources()
    pipeline.run_daily(store, sources, DAY1)
    sources.arm = {"eastus2": items}
    report = pipeline.run_daily(store, sources, DAY2)
    assert report["failed"] == ["arm"]
    assert ("2026-10-07", "arm") not in store.snapshots
    assert not any(event["type"] == ev.MODEL_REMOVED for event in store.observed.values())


def test_empty_region_is_carried_forward_when_other_regions_succeed():
    store, sources = FakeStore(), _sources()
    sources.arm["westus"] = copy.deepcopy(sources.arm["eastus2"])
    pipeline.run_daily(store, sources, DAY1)
    sources.arm["westus"] = []
    report = pipeline.run_daily(store, sources, DAY2)
    assert report["failed"] == ["arm"]
    assert report["arm"]["stale_regions"] == ["westus"]
    assert not any(event["source"] == "arm" for event in store.observed.values())


def test_announced_future_price_is_scheduled_and_can_be_withdrawn():
    store, sources = FakeStore(), _sources()
    pipeline.run_daily(store, sources, DAY1)
    sources.prices = [price_row(2.0, effective="2026-11-01T00:00:00Z")]
    report = pipeline.run_daily(store, sources, DAY2)
    assert report["failed"] == []
    assert not any(event["source"] == "retail_prices" for event in store.observed.values())
    [event] = [event for event in store.schedule.values() if event["source"] == "retail_prices"]
    assert (event["kind"], event["date"], event["type"]) == (ev.SCHEDULED, "2026-11-01", ev.PRICE_CHANGED)
    assert [record["price"] for record in store.latest_snapshot("prices")["prices"].values()] == [1.0]
    sources.prices = [price_row(1.0)]
    report = pipeline.run_daily(store, sources, DAY2)
    assert report["prices"]["schedule_deletes"] == 1
    assert not any(event["source"] == "retail_prices" for event in store.schedule.values())


def test_first_price_snapshot_can_contain_only_future_meters():
    store, sources = FakeStore(), _sources(prices=[price_row(2.0, effective="2026-11-01T00:00:00Z")])
    assert pipeline.run_daily(store, sources, DAY1)["failed"] == []
    assert store.latest_snapshot("prices")["prices"] == {}
    assert any(event["source"] == "retail_prices" for event in store.schedule.values())


@pytest.mark.parametrize("previous_skus, expected", [([], ["Azure OpenAI GPT5 / mystery meter"]), (["mystery meter"], [])])
def test_new_unparsed_meters(previous_skus, expected):
    from builders import price_snapshot

    previous = price_snapshot([price_row(1.0, sku_name=name) for name in previous_skus])
    current = price_snapshot([price_row(1.0), price_row(1.0, sku_name="mystery meter")])
    assert pipeline.new_unparsed_meters(previous, current) == expected
