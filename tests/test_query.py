import pytest

from core import events as ev
from core.query import collection_health, date_window, query_changes, response_size

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


@pytest.mark.parametrize("state", ["unknown", "collecting", "failed", "degraded"])
def test_collection_is_incomplete_until_every_source_succeeds(state):
    status = {"status": state, "snapshot_at": "2026-10-07T00:00:00Z"}
    result = collection_health({"arm": status, "docs": None}, today=TODAY)
    assert result["complete"] is False
    assert result["sources"]["arm"]["status"] == state
    assert result["sources"]["docs"]["status"] == "unknown"


def test_collection_health_uses_shanghai_day_and_actual_cached_snapshot():
    status = {"status": "complete", "snapshot_at": "2026-10-06T16:00:00Z"}
    assert collection_health({"arm": status}, today=TODAY)["complete"] is True
    cached = collection_health({"arm": status}, today=TODAY, served_at={"arm": "2026-10-06T00:00:00Z"})
    assert cached["complete"] is False
    assert cached["sources"]["arm"]["status"] == "stale"
    assert collection_health({"arm": status}, today="2026-10-08")["sources"]["arm"]["status"] == "stale"


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


def test_cursor_reads_all_events_once_across_group_boundaries():
    events = [_event(str(index), TODAY, model=f"model-{index // 30}") for index in range(201)]
    expected = _ids(query_changes(events, scope="today", today=TODAY, limit=1000))
    cursor, received = None, []
    for _ in range(20):
        page = query_changes(reversed(events), scope="today", today=TODAY, limit=17, cursor=cursor)
        received.extend(_ids(page))
        assert page["total_events"] == 201
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["truncated"] is False
            break
    assert received == expected
    assert len(set(received)) == 201


@pytest.mark.parametrize("cursor", ["not-base64", "e30=", "W10=", "a" * 513])
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(ValueError, match="invalid cursor"):
        query_changes([], scope="today", today=TODAY, cursor=cursor)


def test_cursor_rejects_changed_filters_or_data():
    events = [_event("first", TODAY), _event("second", TODAY)]
    cursor = query_changes(events, scope="today", today=TODAY, limit=1)["next_cursor"]
    with pytest.raises(ValueError, match="cursor expired"):
        query_changes(events, scope="today", today=TODAY, category="lifecycle", cursor=cursor)
    with pytest.raises(ValueError, match="cursor expired"):
        query_changes([*events, _event("third", TODAY)], scope="today", today=TODAY, cursor=cursor)


def test_byte_budget_pages_large_price_details_without_losing_rows():
    rows = [{"sku": str(index), "old": 1, "new": 2, "note": "\u6a21\u578b" * 100} for index in range(100)]
    event = _event("large", TODAY, ev.PRICE_CHANGED, category="price", changes=rows)
    cursor, received = None, []
    for _ in range(100):
        page = query_changes([event], scope="today", today=TODAY, cursor=cursor, max_bytes=16384)
        assert response_size(page) <= 16384
        [part] = [item for group in page["groups"] for item in group["events"]]
        assert part["changes_offset"] == len(received)
        assert part["changes_total"] == 100
        received.extend(part["changes"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert part["details_truncated"] is False
            break
        assert page["byte_limited"] is True
    assert received == rows
    assert event["changes"] == rows


def test_byte_budget_pages_whole_events_before_the_count_limit():
    events = [_event(str(index), TODAY, old="x" * 3000) for index in range(10)]
    first = query_changes(events, scope="today", today=TODAY, max_bytes=16384)
    assert first["byte_limited"] is True and first["returned_events"] < 10
    assert first["next_cursor"] is not None
    with pytest.raises(ValueError, match="increase max_bytes"):
        query_changes([_event("huge", TODAY, old="x" * 20000)], scope="today", today=TODAY, max_bytes=16384)
    assert query_changes([_event("huge", TODAY, old="x" * 20000)], scope="today", today=TODAY)["returned_events"] == 1


@pytest.mark.parametrize("max_bytes", [0, 16383, 1048577])
def test_invalid_response_budget(max_bytes):
    with pytest.raises(ValueError, match="max_bytes"):
        query_changes([], scope="today", today=TODAY, max_bytes=max_bytes)


def test_cursor_fingerprint_is_stable_for_reordered_retirement_links():
    events = [_event("plan", TODAY, ev.RETIRING, kind="scheduled", field="inference"),
              _event("gone", TODAY, ev.MODEL_REMOVED), _event("retired", TODAY, ev.STATUS_CHANGED, new="Deprecated")]
    page = query_changes(events, scope="today", today=TODAY, limit=1)
    following = query_changes(reversed(events), scope="today", today=TODAY, cursor=page["next_cursor"])
    assert following["returned_events"] == 2
