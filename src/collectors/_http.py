"""HTTP plumbing shared by the collectors: retries on throttling/5xx and host pinning for followed links."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TIMEOUT = (10, 120)
MAX_PAGES = 500


def session() -> requests.Session:
    retry = Retry(
        total=6,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    http = requests.Session()
    http.mount("https://", HTTPAdapter(max_retries=retry))
    return http


def same_host(url: str, expected: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == expected


def get_json(http: requests.Session, url: str, **kwargs: Any) -> Any:
    response = http.get(url, timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    return response.json()
