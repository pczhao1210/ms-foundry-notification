"""Collect the official model retirement schedule page (Markdown) and its Git history from GitHub."""
from __future__ import annotations

from typing import Any

from . import _http

REPO = "MicrosoftDocs/azure-ai-docs"
BRANCH = "main"
PATH = "articles/foundry/openai/includes/concepts-model-retirement-schedule-content.md"
RAW = "https://raw.githubusercontent.com"
COMMITS = f"https://api.github.com/repos/{REPO}/commits"


def fetch(ref: str = BRANCH) -> str:
    response = _http.session().get(f"{RAW}/{REPO}/{ref}/{PATH}", timeout=_http.TIMEOUT)
    response.raise_for_status()
    return response.text


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
