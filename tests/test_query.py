import pytest

from core import events as ev
from core.query import date_window, query_changes

TODAY = "2026-10-07"


def _event(event_id, date, event_type=ev.STATUS_CHANGED, *, kind="observed", category="lifecycle", model="gpt-x",
           fmt="OpenAI", sku=None, billing=("token",), **extra):
    event = {
        "id": event_id,
        "kind": kind,
        "category": category,
        "source": "arm",
        "type": event_type,
        "date": date,
        "billing": list(billing),
        **extra,
    }
    if category == "price":
        event["meter"] = {"product": "Azure OpenAI GPT5", "sku": "5.4 opt Gl", "meter": "5.4 opt Gl 1M Tokens"}
    else:
        event["model"] = {"format": fmt, "name": model, "version": "1"}
        event["sku"] = sku
    return event


def _ids(result):
    return [event["id"] for group in result["groups"] for event in group["events"]]


def test_windows_do_not_overlap():
    assert date_window("today", TODAY) == (TODAY, TODAY)
    assert date_window("past", TODAY, 7) == ("2026-09-30", "2026-10-06")
    assert date_window("past", TODAY, 30) == ("2026-09-07", "2026-10-06")
    assert date_window("upcoming", TODAY, 7) == ("2026-10-08", "2026-10-14")


@pytest.mark.parametrize("days", [0, 31])
def test_days_out_of_range(days):
    with pytest.raises(ValueError):
        date_window("past", TODAY, days)


@pytest.mark.parametrize("kwargs", [{"scope": "week"}, {"category": "other"}, {"billing": "free"}, {"limit": 0}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        query_changes([], **{"scope": "today", "today": TODAY, **kwargs})


def test_today_combines_observed_and_scheduled_for_today_only():
    events = [
        _event("observed-today", TODAY),
        _event("scheduled-today", TODAY, ev.RETIRING, kind="scheduled", model="old"),
        _event("yesterday", "2026-10-06"),
        _event("tomorrow", "2026-10-08", ev.RETIRING, kind="scheduled"),
    ]
    result = query_changes(events, scope="today", today=TODAY)
    assert sorted(_ids(result)) == ["observed-today", "scheduled-today"]
    assert (result["from"], result["to"], result["timezone"]) == (TODAY, TODAY, "Asia/Shanghai")


def test_past_lists_every_change_without_netting():
    events = [
        _event("p1", "2026-10-04", ev.PRICE_CHANGED, category="price", old=1.0, new=1.2),
        _event("p3", "2026-10-06", ev.PRICE_CHANGED, category="price", old=1.1, new=1.0),
        _event("p2", "2026-10-05", ev.PRICE_CHANGED, category="price", old=1.2, new=1.1),
    ]
    result = query_changes(events, scope="past", today=TODAY)
    [group] = result["groups"]
    assert group["subject"]["category"] == "price"
    assert [(e["old"], e["new"]) for e in group["events"]] == [(1.0, 1.2), (1.2, 1.1), (1.1, 1.0)]
    assert "meter" not in group["events"][0]
    assert result["summary"] == {ev.PRICE_CHANGED: 3}


def test_past_30_days_widens_the_same_window():
    events = [_event("recent", "2026-10-05"), _event("older", "2026-09-15"), _event("too-old", "2026-09-01")]
    assert query_changes(events, scope="past", today=TODAY, days=7)["total_events"] == 1
    assert query_changes(events, scope="past", today=TODAY, days=30)["total_events"] == 2


def test_past_groups_by_model_newest_first_with_chronological_events():
    events = [
        _event("a1", "2026-10-01", model="a"),
        _event("b1", "2026-10-05", model="b"),
        _event("a2", "2026-10-03", model="a"),
    ]
    result = query_changes(events, scope="past", today=TODAY)
    assert [g["subject"]["model"]["name"] for g in result["groups"]] == ["b", "a"]
    assert [e["id"] for e in result["groups"][1]["events"]] == ["a1", "a2"]


def test_upcoming_groups_earliest_first():
    events = [
        _event("late", "2026-10-12", ev.RETIRING, kind="scheduled", model="late"),
        _event("soon", "2026-10-08", ev.RETIRING, kind="scheduled", model="soon"),
        _event("far", "2026-11-30", ev.RETIRING, kind="scheduled", model="far"),
    ]
    result = query_changes(events, scope="upcoming", today=TODAY)
    assert [g["subject"]["model"]["name"] for g in result["groups"]] == ["soon", "late"]


def test_filters():
    events = [
        _event("openai", TODAY),
        _event("deepseek", TODAY, fmt="DeepSeek", model="ds", billing=("provisioned",)),
        _event("price", TODAY, ev.PRICE_CHANGED, category="price"),
    ]

    def ids(**kwargs):
        return sorted(_ids(query_changes(events, scope="today", today=TODAY, **kwargs)))

    assert ids(category="price") == ["price"]
    assert ids(category="lifecycle") == ["deepseek", "openai"]
    assert ids(billing="provisioned") == ["deepseek"]


def test_price_events_group_by_parsed_model_and_count_unparsed():
    price_model = {"product": "Azure OpenAI GPT5", "name": "5.6 sol"}
    parsed = _event("parsed", TODAY, ev.PRICE_CHANGED, category="price")
    del parsed["meter"]
    parsed["price_model"] = price_model
    unparsed = _event("unparsed", TODAY, ev.PRICE_CHANGED, category="price", unparsed=True)
    result = query_changes([parsed, unparsed], scope="today", today=TODAY)
    subjects = {event["id"]: group["subject"] for group in result["groups"] for event in group["events"]}
    assert subjects["parsed"] == {"category": "price", "price_model": price_model}
    assert "meter" in subjects["unparsed"]
    assert result["unparsed_price_events"] == 1
    assert not any("price_model" in event for group in result["groups"] for event in group["events"])


def test_mapped_price_events_group_by_arm_model_and_keep_price_names():
    arm_model = {"format": "OpenAI", "name": "gpt-5.4"}
    events = []
    for name in ("5.4", "54"):
        event = _event(name, TODAY, ev.PRICE_CHANGED, category="price", mapped=True)
        del event["meter"]
        event.update(price_model={"product": "Azure OpenAI GPT5", "name": name}, model=arm_model)
        events.append(event)
    [group] = query_changes(events, scope="today", today=TODAY)["groups"]
    assert group["subject"] == {"category": "price", "model": arm_model}
    assert sorted(e["price_model"]["name"] for e in group["events"]) == ["5.4", "54"]
    assert not any("model" in e or "category" in e for e in group["events"])


def test_scheduled_retirement_is_linked_to_observed_removal():
    plan = _event("plan", "2026-10-05", ev.RETIRING, kind="scheduled", field="inference")
    removed = _event("gone", "2026-10-05", ev.MODEL_REMOVED, old="Deprecating")
    unrelated = _event("other", "2026-10-05", ev.MODEL_REMOVED, model="other")
    result = query_changes([removed, plan, unrelated], scope="past", today=TODAY)
    events = {e["id"]: e for g in result["groups"] for e in g["events"]}
    assert events["plan"]["related_event_ids"] == ["gone"]
    assert events["gone"]["related_event_ids"] == ["plan"]
    assert "related_event_ids" not in events["other"]
    gpt_group = next(g for g in result["groups"] if g["subject"]["model"]["name"] == "gpt-x")
    assert [e["id"] for e in gpt_group["events"]] == ["plan", "gone"]
    assert "related_event_ids" not in plan


def test_sku_deprecation_links_only_the_same_sku():
    standard = {"name": "Standard", "scope": "Base"}
    global_standard = {"name": "GlobalStandard", "scope": "Base"}
    events = [
        _event("plan", "2026-10-03", ev.SKU_DEPRECATING, kind="scheduled", sku=standard),
        _event("match", "2026-10-04", ev.SKU_STATUS_CHANGED, sku=standard, new="Deprecated"),
        _event("not-retired", "2026-10-04", ev.SKU_STATUS_CHANGED, sku=standard, new="Deprecating"),
        _event("other-sku", "2026-10-04", ev.SKU_REMOVED, sku=global_standard),
    ]
    result = query_changes(events, scope="past", today=TODAY)
    linked = {e["id"]: e.get("related_event_ids") for g in result["groups"] for e in g["events"]}
    assert linked == {"plan": ["match"], "match": ["plan"], "not-retired": None, "other-sku": None}


def test_docs_status_terms_are_used_for_docs_events():
    events = [
        _event("plan", "2026-10-03", ev.RETIRING, kind="scheduled", source="docs"),
        _event("docs-retired", "2026-10-03", ev.STATUS_CHANGED, source="docs", new="Retired"),
        # Docs "Deprecated" means API Deprecating: still callable by existing deployments.
        _event("docs-deprecated", "2026-10-04", ev.STATUS_CHANGED, source="docs", new="Deprecated"),
    ]
    result = query_changes(events, scope="past", today=TODAY)
    linked = {e["id"]: e.get("related_event_ids") for g in result["groups"] for e in g["events"]}
    assert linked == {"plan": ["docs-retired"], "docs-retired": ["plan"], "docs-deprecated": None}


def test_truncation_reports_totals():
    events = [_event(f"a{i}", "2026-10-06", model="a") for i in range(3)] + [_event("b", "2026-10-05", model="b")]
    result = query_changes(events, scope="past", today=TODAY, limit=2)
    assert result["truncated"] is True
    assert (result["total_events"], result["returned_events"]) == (4, 2)
    [group] = result["groups"]
    assert group["truncated"] is True
    assert [e["id"] for e in group["events"]] == ["a0", "a1"]


def test_duplicate_events_are_counted_once():
    event = _event("same", TODAY)
    assert query_changes([event, dict(event)], scope="today", today=TODAY)["total_events"] == 1
