from unittest.mock import Mock

import pytest
import requests

from builders import FIXTURES
from collectors import docs_schedule
from collectors._http import same_host
from collectors.arm_models import ArmModelsCollector, region_names

LOCATIONS = [
    {"name": "eastus2", "displayName": "East US 2"},
    {"name": "swedencentral", "displayName": "Sweden Central"},
]


def test_region_names_map_display_names_and_skip_global():
    assert region_names(["East US 2", "sweden central", "Global", "New Region"], LOCATIONS) == [
        "eastus2",
        "newregion",
        "swedencentral",
    ]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://management.azure.com/subscriptions/x?api-version=1&$skiptoken=a", True),
        ("http://management.azure.com/subscriptions/x", False),
        ("https://management.azure.com.evil.example/x", False),
        ("https://evil.example/?https://management.azure.com", False),
    ],
)
def test_followed_links_must_stay_on_the_expected_host(url, expected):
    assert same_host(url, "management.azure.com") is expected


@pytest.mark.parametrize("page", [{}, {"value": None}, {"value": {}}])
def test_models_rejects_malformed_pages(monkeypatch, page):
    collector = ArmModelsCollector("00000000-0000-0000-0000-000000000000", "2026-09-01", None)
    monkeypatch.setattr(collector, "_get", lambda *_: page)
    with pytest.raises(ValueError, match="invalid Models API page"):
        collector.models("eastus2")


def _docs_http(monkeypatch, responses):
    http = Mock()
    http.__enter__ = Mock(return_value=http)
    http.__exit__ = Mock(return_value=False)
    http.get.side_effect = responses
    monkeypatch.setattr(docs_schedule._http, "session", lambda: http)
    return http


def _response(body="", status=200, content_type="text/html"):
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response._content = body if isinstance(body, bytes) else body.encode("utf-8")
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


def test_docs_prefers_published_markdown_without_github(monkeypatch):
    markdown = (FIXTURES / "model_retirement_schedule_2026-10-05.md").read_text(encoding="utf-8")
    http = _docs_http(monkeypatch, [_response(markdown, content_type="text/markdown; charset=utf-8")])
    assert docs_schedule.fetch() == markdown
    http.get.assert_called_once_with(
        f"{docs_schedule.LEARN_URL}?accept=text/markdown", timeout=docs_schedule._http.TIMEOUT
    )


@pytest.mark.parametrize("failure", [
    "[!INCLUDE [schedule](../includes/schedule.md)]",
    "<html><main>HTML instead of Markdown</main></html>",
    "| Model | Retirement date |\n|---|---|\n| m | 2027-01-01 |",
    "empty page",
    b"| Model | Version | Lifecycle | Retirement date | Replacement |\n| m | 1 | GA | \xff | x |",
    404,
    requests.Timeout(),
])
def test_docs_falls_back_to_published_html(monkeypatch, failure, caplog):
    first = failure if isinstance(failure, Exception) else (
        _response(status=failure) if isinstance(failure, int) else _response(failure)
    )
    html = (FIXTURES / "model_retirement_schedule_published.html").read_text(encoding="utf-8")
    http = _docs_http(monkeypatch, [first, _response(html)])
    markdown = docs_schedule.fetch()
    assert "| gpt-x | 1 | GA | 2026-12-01 | new-x |" in markdown
    assert [call.args[0] for call in http.get.call_args_list] == [
        f"{docs_schedule.LEARN_URL}?accept=text/markdown", docs_schedule.LEARN_URL,
    ]
    assert "stage=" in caplog.text and "http_status=" in caplog.text


@pytest.mark.parametrize("last, stage, status", [
    (_response(status=404), "download", 404),
    (requests.Timeout(), "download", None),
    (_response("<main>No schedule</main>"), "validate", 200),
    (_response("<main><table><tr><td colspan='2'>x</td></tr></table></main>"), "parse", 200),
])
def test_docs_failures_include_safe_diagnostics(monkeypatch, last, stage, status):
    _docs_http(monkeypatch, [_response(status=404), last])
    with pytest.raises(docs_schedule.DocsCollectionError) as caught:
        docs_schedule.fetch()
    assert caught.value.diagnostics == {
        "source_url": docs_schedule.LEARN_URL, "stage": stage, "http_status": status,
    }


def test_docs_html_without_header_charset_is_decoded_as_utf8(monkeypatch):
    from core.docs import diff_docs, parse_schedule

    html = (FIXTURES / "model_retirement_schedule_published.html").read_text(encoding="utf-8")
    previous = parse_schedule(docs_schedule.schedule_html_to_markdown(html), collected_at="2026-10-17T00:00:00Z")
    raw = html.replace("&mdash;", "—").replace("&ndash;", "–").encode("utf-8")
    _docs_http(monkeypatch, [_response(status=404), _response(raw)])
    current = parse_schedule(docs_schedule.fetch(previous=previous), collected_at="2026-10-18T00:00:00Z")
    assert current["rows"] == previous["rows"]
    assert diff_docs(previous, current) == []


def test_docs_historical_ref_never_uses_current_learn_page(monkeypatch):
    http = _docs_http(monkeypatch, [_response("historical markdown")])
    assert docs_schedule.fetch("immutable-commit") == "historical markdown"
    http.get.assert_called_once_with(
        f"{docs_schedule.RAW}/{docs_schedule.REPO}/immutable-commit/{docs_schedule.PATH}",
        timeout=docs_schedule._http.TIMEOUT,
    )


def test_docs_partial_markdown_tries_html_before_failing_coverage(monkeypatch):
    from core.docs import parse_schedule

    html = (FIXTURES / "model_retirement_schedule_published.html").read_text(encoding="utf-8")
    full = docs_schedule.schedule_html_to_markdown(html)
    previous = parse_schedule(full, collected_at="2026-10-13T00:00:00Z")
    partial = full.split("\n## Foundry Models sold by partners")[0]
    assert len(parse_schedule(partial, collected_at="2026-10-17T00:00:00Z")["rows"]) == 3
    http = _docs_http(monkeypatch, [_response(partial), _response(html)])
    assert docs_schedule.fetch(previous=previous) == full
    assert http.get.call_count == 2


def test_incomplete_small_schedule_table_falls_back_and_invalid_html_fails(monkeypatch):
    from core.docs import parse_schedule

    markdown_header = (
        "## Foundry Models sold by Azure\n\n### Azure OpenAI\n\n"
        "| Model | Version | Lifecycle | Retirement date | Replacement |\n|---|---|---|---|---|\n"
    )
    rows = "".join(f"| m{i} | 1 | GA | 2027-01-01 | — |\n" for i in range(5))
    table = (
        "| Model | Version | Lifecycle | Retirement date | Replacement |\n|---|---|---|---|---|\n"
        "| partner-model | 1 | GA | 2027-01-01 | — |\n"
    )
    previous_markdown = markdown_header + rows + "\n### Partner\n\n" + table
    broken_table = table.replace("| Version |", "| Training version |")
    partial_markdown = markdown_header + rows + "\n### Partner\n\n" + broken_table
    previous = parse_schedule(previous_markdown, collected_at="2026-10-13T00:00:00Z")
    assert len(parse_schedule(markdown_header + rows, collected_at="2026-10-17T00:00:00Z")["rows"]) == 5
    valid_html = (
        "<main><h2>Foundry Models sold by Azure</h2><h3>Azure OpenAI</h3><table>"
        "<tr><th>Model</th><th>Version</th><th>Lifecycle</th><th>Retirement date</th><th>Replacement</th></tr>"
        + "".join(
            f"<tr><td>m{i}</td><td>1</td><td>GA</td><td>2027-01-01</td><td>—</td></tr>"
            for i in range(5)
        )
        + "</table><h3>Partner</h3><table>"
        "<tr><th>Model</th><th>Version</th><th>Lifecycle</th><th>Retirement date</th><th>Replacement</th></tr>"
        "<tr><td>partner-model</td><td>1</td><td>GA</td><td>2027-01-01</td><td>—</td></tr>"
        "</table></main>"
    )

    http = _docs_http(monkeypatch, [_response(partial_markdown), _response(valid_html)])
    assert docs_schedule.fetch(previous=previous) != partial_markdown
    assert http.get.call_count == 2

    invalid_html = valid_html.replace("<th>Replacement</th>", "<th>New column</th>")
    _docs_http(monkeypatch, [_response(partial_markdown), _response(invalid_html)])
    with pytest.raises(docs_schedule.DocsCollectionError) as caught:
        docs_schedule.fetch(previous=previous)
    assert caught.value.diagnostics["stage"] == "parse"
