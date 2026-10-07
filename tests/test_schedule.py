from builders import arm_item, load_fixture, sku, snapshot
from core import events as ev
from core.schedule import build_schedule, reconcile_schedule


def test_fixture_sku_dates_equal_to_model_retirement_are_not_duplicated():
    snap = snapshot({"eastus2": load_fixture("models_eastus2_2026-09-01.json")["value"]})
    gpt4o = {"format": "OpenAI", "name": "gpt-4o", "version": "2024-05-13"}
    events = [e for e in build_schedule(snap) if e["model"] == gpt4o]
    summary = {(e["type"], e["date"], e["sku"]["name"] if e["sku"] else None) for e in events}
    assert (ev.RETIRING, "2026-12-09", None) in summary
    assert (ev.SKU_DEPRECATING, "2026-03-31", "Standard") in summary
    assert not any(e["type"] == ev.SKU_DEPRECATING and e["date"] == "2026-12-09" for e in events)


def test_schedule_events_are_aggregated_across_regions():
    replacement = {"targetModelName": "gpt-y", "targetModelVersion": "2", "autoUpgradeStartDate": "2026-11-24T00:00:00Z"}
    item = arm_item(
        inference="2026-12-09T00:00:00Z",
        replacement=replacement,
        skus=[sku(retirement="2026-12-09T00:00:00Z"), sku("Standard", retirement="2026-10-31T00:00:00Z")],
    )
    events = build_schedule(snapshot({"eastus": [item], "westus": [item]}))
    by_type = {e["type"]: e for e in events}
    assert set(by_type) == {ev.RETIRING, ev.AUTO_UPGRADE_START, ev.SKU_DEPRECATING}
    assert all(e["regions"] == ["eastus", "westus"] and e["kind"] == "scheduled" for e in events)

    retiring = by_type[ev.RETIRING]
    assert (retiring["date"], retiring["effective_at"], retiring["field"]) == (
        "2026-12-09",
        "2026-12-09T00:00:00Z",
        "inference",
    )
    assert retiring["details"]["replacement"]["model"] == "gpt-y"
    assert by_type[ev.AUTO_UPGRADE_START]["details"]["target"] == {"model": "gpt-y", "version": "2"}
    assert by_type[ev.SKU_DEPRECATING]["sku"] == {"name": "Standard", "scope": "Base"}
    assert by_type[ev.SKU_DEPRECATING]["date"] == "2026-10-31"


def test_reconcile_replaces_future_entries_and_freezes_history():
    today = "2026-10-07"
    computed = [
        {"id": "moved-new", "date": "2026-11-30"},
        {"id": "future", "date": "2026-10-20"},
        {"id": "past-known", "date": "2026-10-01"},
        {"id": "past-new", "date": "2026-09-01"},
        {"id": "today", "date": today},
    ]
    existing = {
        "moved-old": "2026-10-31",
        "future": "2026-10-20",
        "past-known": "2026-10-01",
        "past-gone": "2026-10-02",
        "today": today,
        "today-gone": today,
    }
    upserts, deletes = reconcile_schedule(existing, computed, today)
    assert [e["id"] for e in upserts] == ["moved-new", "future", "past-new"]
    assert deletes == ["moved-old"]
