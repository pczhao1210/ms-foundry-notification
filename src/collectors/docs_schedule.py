"""Collect the published Learn schedule; GitHub is used only for optional historical versions."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import requests

from core.docs import parse_schedule, schedule_html_to_markdown, validate_schedule

from . import _http

REPO = "MicrosoftDocs/azure-ai-docs"
BRANCH = "main"
PATH = "articles/foundry/openai/includes/concepts-model-retirement-schedule-content.md"
RAW = "https://raw.githubusercontent.com"
COMMITS = f"https://api.github.com/repos/{REPO}/commits"
LEARN_URL = "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule"
log = logging.getLogger(__name__)


class DocsCollectionError(RuntimeError):
    def __init__(self, *, source_url: str, stage: str, http_status: int | None = None) -> None:
        self.diagnostics = {"source_url": source_url, "stage": stage, "http_status": http_status}
        super().__init__(f"docs collection failed: source={source_url} stage={stage} http_status={http_status}")


def fetch(ref: str | None = None, *, previous: Mapping[str, Any] | None = None) -> str:
    if ref is not None:
        response = _http.session().get(f"{RAW}/{REPO}/{ref}/{PATH}", timeout=_http.TIMEOUT)
        response.raise_for_status()
        return response.text
    with _http.session() as http:
        for content_format, url in (("markdown", f"{LEARN_URL}?accept=text/markdown"), ("html", LEARN_URL)):
            status = None
            stage = "download"
            try:
                response = http.get(url, timeout=_http.TIMEOUT)
                status = response.status_code
                response.raise_for_status()
                stage = "parse"
                markdown = response.text if content_format == "markdown" else schedule_html_to_markdown(response.text)
                snapshot = parse_schedule(markdown, collected_at="1970-01-01T00:00:00Z")
                stage = "validate"
                validate_schedule(snapshot, previous)
                log.info("docs fetched: source=%s format=%s http_status=%s rows=%s",
                         url, content_format, status, len(snapshot["rows"]))
                return markdown
            except (requests.RequestException, ValueError) as exc:
                log.warning("docs fetch failed: source=%s format=%s http_status=%s stage=%s error=%s",
                            url, content_format, status, stage, type(exc).__name__)
                if content_format == "html":
                    raise DocsCollectionError(source_url=url, stage=stage, http_status=status) from exc
    raise AssertionError("no Docs source attempted")


def _commits(**params: Any) -> list[dict[str, Any]]:
    return _http.get_json(
        _http.session(),
        COMMITS,
        params={"path": PATH, "sha": BRANCH, **params},
        headers={"Accept": "application/vnd.github+json"},
    )


def history(since: str) -> list[tuple[str, str]]:
    """(commit time, markdown) oldest first: the version in effect at `since`, then every later commit."""
    later = _commits(since=since, per_page=100)
    baseline = _commits(until=since, per_page=1)
    commits = [*baseline, *reversed(later)]
    return [(commit["commit"]["committer"]["date"], fetch(commit["sha"])) for commit in commits]
