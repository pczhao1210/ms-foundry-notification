import pytest

from builders import FIXTURES, arm_item, sku, snapshot
from core import events as ev
from core.docs import build_docs_schedule, diff_docs, parse_schedule

AT = "2026-10-07T00:00:00Z"
HEADER = (
    "## Foundry Models sold by Azure\n\n### Azure OpenAI\n\n"
    "| Model | Version | Lifecycle | Retirement date | Replacement |\n|---|---|---|---|---|\n"
)


def _docs(rows: str, at: str = AT) -> dict:
    return parse_schedule(HEADER + rows, collected_at=at)


@pytest.fixture(scope="module")
def page() -> dict:
    text = (FIXTURES / "model_retirement_schedule_2026-10-05.md").read_text(encoding="utf-8")
    return parse_schedule(text, collected_at=AT)


def test_parses_every_lifecycle_table_of_the_page(page):
    rows = page["rows"]
    assert len(rows) == 183
    assert rows["azure|Azure OpenAI|gpt-4o|2024-05-13"] == {
        "section": "azure",
        "vendor": "Azure OpenAI",
        "model": "gpt-4o",
        "version": "2024-05-13",
        "lifecycle": "Deprecated",
        "retirement": "2026-12-09T00:00:00Z",
        "retirement_note": None,
        "replacement": "gpt-5.6-sol",
    }
    assert {"Anthropic", "Fireworks", "Nixtla"} <= {r["vendor"] for r in rows.values() if r["section"] == "partner"}
    assert {r["lifecycle"] for r in rows.values()} == {"GA", "Preview", "Legacy", "Deprecated", "Retired"}


def test_fine_tuned_table_with_other_columns_is_skipped(page):
    assert all(row["retirement_note"] is None for row in page["rows"].values())
    text = HEADER.split("| Model")[0] + "| Model | Version | Training retirement date |\n|---|---|---|\n| ft | 1 | 2027-01-01 |\n"
    assert parse_schedule(text, collected_at=AT)["rows"] == {}


def test_cleans_cells():
    rows = _docs(
        "| **claude-x** (gated research preview) | — | Preview | 2027-04-02 | [new-a](../a.md)<sup>1</sup>, new-b<sup>1</sup> |\n"
        "| m | 1 | GA | No earlier than 2027-01-01 | — |\n"
    )["rows"]
    assert rows["azure|Azure OpenAI|claude-x|"]["model"] == "claude-x"
    assert rows["azure|Azure OpenAI|claude-x|"]["version"] is None
    assert rows["azure|Azure OpenAI|claude-x|"]["replacement"] == "new-a, new-b"
    assert rows["azure|Azure OpenAI|m|1"]["retirement"] is None
    assert rows["azure|Azure OpenAI|m|1"]["retirement_note"] == "No earlier than 2027-01-01"


def test_duplicate_rows_get_distinct_keys(page):
    assert "azure|Azure OpenAI|gpt-realtime-mini|2025-10-06#2" in page["rows"]
    rows = _docs("| m | 1 | GA | 2027-01-01 | — |\n| m | 1 | GA | 2027-02-01 | — |\n")["rows"]
    assert [row["retirement"][:10] for row in rows.values()] == ["2027-01-01", "2027-02-01"]


def test_diff_reports_field_changes_of_rows_in_both_versions():
    prev = _docs("| m | 1 | Preview | 2026-12-01 | — |\n| gone | 1 | GA | — | — |\n", at="2026-10-06T00:00:00Z")
    curr = _docs("| m | 1 | GA | 2027-01-15 | n |\n| new | 1 | GA | — | — |\n")
    events = diff_docs(prev, curr)
    assert {(e["type"], e["field"], e["old"], e["new"]) for e in events} == {
        (ev.STATUS_CHANGED, "lifecycle", "Preview", "GA"),
        (ev.RETIREMENT_DATE_CHANGED, "inference", "2026-12-01T00:00:00Z", "2027-01-15T00:00:00Z"),
        (ev.REPLACEMENT_CHANGED, "replacement", None, "n"),
    }
    assert all(e["source"] == "docs" and e["kind"] == "observed" and e["date"] == "2026-10-07" for e in events)
    assert all(e["model"] == {"format": None, "name": "m", "version": "1"} and e["billing"] == [] for e in events)
    assert diff_docs(prev, curr) == events


def test_diff_resolves_arm_model_case_insensitively():
    arm = snapshot({"eastus2": [arm_item("M", "1", skus=[sku("ProvisionedManaged")])]})
    prev = _docs("| m | 1 | Preview | — | — |\n", at="2026-10-06T00:00:00Z")
    curr = _docs("| m | 1 | GA | — | — |\n")
    [event] = diff_docs(prev, curr, arm)
    assert event["model"] == {"format": "OpenAI", "name": "M", "version": "1"}
    assert event["billing"] == ["provisioned"]


def test_schedule_only_adds_dates_arm_does_not_report():
    arm = snapshot(
        {
            "eastus2": [
                arm_item("a", "1", inference="2026-12-01T00:00:00Z"),
                arm_item("b", "1", inference="2027-01-01T00:00:00Z"),
            ]
        }
    )
    docs = _docs(
        "| a | 1 | Deprecated | 2026-12-01 | — |\n"
        "| b | 1 | Deprecated | 2026-12-15 | x |\n"
        "| c | 1 | GA | 2027-02-01 | — |\n"
        "| d | 1 | GA | — | — |\n"
    )
    events = {e["model"]["name"]: e for e in build_docs_schedule(docs, arm)}
    assert set(events) == {"b", "c"}
    assert events["b"]["model"] == {"format": "OpenAI", "name": "b", "version": "1"}
    assert events["b"]["date"] == "2026-12-15"
    assert events["b"]["details"] == {
        "vendor": "Azure OpenAI",
        "lifecycle": "Deprecated",
        "replacement": "x",
        "arm_retirement_dates": ["2027-01-01"],
    }
    assert events["c"]["model"] == {"format": None, "name": "c", "version": "1"}
    assert events["c"]["billing"] == []
    assert "arm_retirement_dates" not in events["c"]["details"]
    assert all(
        (e["kind"], e["source"], e["type"], e["field"]) == ("scheduled", "docs", ev.RETIRING, "inference")
        for e in events.values()
    )


def test_schedule_against_fixture_catalog(page):
    from builders import load_fixture

    arm = snapshot({"eastus2": load_fixture("models_eastus2_2026-09-01.json")["value"]})
    events = build_docs_schedule(page, arm)
    refs = [e["model"] for e in events]
    assert {"format": "OpenAI", "name": "gpt-4o", "version": "2024-05-13"} not in refs
    [nano] = [e for e in events if e["model"]["name"] == "gpt-4.1-nano"]
    assert nano["date"] == "2026-10-14"
    assert nano["details"]["arm_retirement_dates"] == ["2027-04-14"]
