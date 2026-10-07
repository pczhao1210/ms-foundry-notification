"""Parse the official model retirement schedule (Markdown) and derive docs-sourced events.

Docs terms are kept as-is (GA / Preview / Legacy / Deprecated / Retired); they differ from ARM `lifecycleStatus`.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from . import events as ev
from .normalize import billing_of, model_ref

SCHEMA_VERSION = 1
SOURCE = "docs"

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*#*$")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SUP = re.compile(r"<sup>.*?</sup>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TRAILING_NOTE = re.compile(r"\s*\([^)]*\)\s*$")
_EMPTY = frozenset({"", "—", "–", "-", "n/a", "na", "none"})
_COLUMNS = {
    "model": "model",
    "version": "version",
    "lifecycle": "lifecycle",
    "retirement date": "retirement",
    "replacement": "replacement",
}
_REQUIRED = frozenset({"model", "lifecycle", "retirement"})
_FIELDS = (
    ("lifecycle", ev.STATUS_CHANGED, "lifecycle"),
    ("retirement", ev.RETIREMENT_DATE_CHANGED, "inference"),
    ("replacement", ev.REPLACEMENT_CHANGED, "replacement"),
)


def _clean(cell: str) -> str:
    text = _LINK.sub(r"\1", _SUP.sub("", cell))
    text = _TAG.sub("", text).replace("**", "").replace("`", "")
    return " ".join(text.split())


def _value(cell: str) -> str | None:
    text = _clean(cell)
    return None if text.casefold() in _EMPTY else text


def parse_schedule(markdown: str, *, collected_at: str, commit: str | None = None) -> dict[str, Any]:
    """Rows of every `Model | Version | Lifecycle | Retirement date | Replacement` table; other tables are skipped."""
    rows: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = defaultdict(int)
    section = vendor = None
    columns: dict[str, int] | None = None
    for line in markdown.splitlines():
        text = line.strip()
        heading = _HEADING.match(text)
        if heading:
            level, title = len(heading.group(1)), _clean(heading.group(2))
            if level == 2:
                section, vendor = ("partner" if "partner" in title.casefold() else "azure"), None
            elif level == 3:
                vendor = title
            columns = None
            continue
        if not text.startswith("|"):
            columns = None
            continue
        cells = [cell.strip() for cell in text.strip().strip("|").split("|")]
        if columns is None:
            names = [_clean(cell).casefold() for cell in cells]
            found = {_COLUMNS[name]: i for i, name in enumerate(names) if name in _COLUMNS}
            columns = found if _REQUIRED <= found.keys() else {}
            continue
        if not columns or all(_SEPARATOR.match(cell.replace(" ", "")) for cell in cells if cell):
            continue
        row = _row(cells, columns, section, vendor)
        if row is None:
            continue
        key = "|".join((row["section"] or "", row["vendor"] or "", row["model"], row["version"] or ""))
        seen[key] += 1
        # The page occasionally lists the same model/version twice with different dates.
        rows[key if seen[key] == 1 else f"{key}#{seen[key]}"] = row
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": ev.to_utc_iso(collected_at),
        "commit": commit,
        "rows": dict(sorted(rows.items())),
    }


def _row(cells: list[str], columns: Mapping[str, int], section: str | None, vendor: str | None) -> dict[str, Any] | None:
    def cell(name: str) -> str:
        index = columns.get(name)
        return cells[index] if index is not None and index < len(cells) else ""

    model = _value(cell("model"))
    if model is None:
        return None
    retirement_text = _value(cell("retirement"))
    is_date = bool(retirement_text and _DATE.match(retirement_text))
    return {
        "section": section,
        "vendor": vendor,
        "model": _TRAILING_NOTE.sub("", model) or model,
        "version": _value(cell("version")),
        "lifecycle": _value(cell("lifecycle")),
        "retirement": ev.to_utc_iso(retirement_text) if is_date else None,
        "retirement_note": None if is_date else retirement_text,
        "replacement": _value(cell("replacement")),
    }


class _Catalog:
    """Resolves docs rows to ARM models by case-insensitive name (+ version when the docs give one)."""

    def __init__(self, arm: Mapping[str, Any] | None) -> None:
        self._by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for model in (arm or {}).get("models", {}).values():
            self._by_name[model["name"].casefold()].append(model)

    def match(self, row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        version = (row["version"] or "").casefold()
        return [
            model
            for model in self._by_name.get(row["model"].casefold(), [])
            if not version or model["version"].casefold() == version
        ]

    @staticmethod
    def ref(row: Mapping[str, Any], matches: list[Mapping[str, Any]]) -> dict[str, Any]:
        if len(matches) == 1:
            return model_ref(matches[0])
        if matches:
            return {"format": matches[0]["format"], "name": matches[0]["name"], "version": row["version"]}
        return {"format": None, "name": row["model"], "version": row["version"]}


def _billing(matches: list[Mapping[str, Any]]) -> list[str]:
    return billing_of(*(model["regions"] for model in matches))


def diff_docs(
    prev: Mapping[str, Any], curr: Mapping[str, Any], arm: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Field changes of rows present in both versions; rows added/removed on the page are not lifecycle facts."""
    catalog = _Catalog(arm)
    base = {"kind": ev.OBSERVED, "category": ev.LIFECYCLE, "source": SOURCE, "date": ev.local_date(curr["collected_at"])}
    context = {"observed_at": curr["collected_at"], "baseline_at": prev["collected_at"]}
    result: dict[str, dict[str, Any]] = {}
    for key in sorted(prev["rows"].keys() & curr["rows"].keys()):
        before, after = prev["rows"][key], curr["rows"][key]
        matches = catalog.match(after)
        for attr, event_type, field in _FIELDS:
            if before[attr] == after[attr]:
                continue
            identity = {
                **base,
                "type": event_type,
                "model": catalog.ref(after, matches),
                "sku": None,
                "field": field,
                "old": before[attr],
                "new": after[attr],
            }
            event_id = ev.event_id(identity)
            result[event_id] = {
                "id": event_id,
                **identity,
                "billing": _billing(matches),
                **context,
                "details": {"vendor": after["vendor"], "lifecycle": after["lifecycle"]},
            }
    return sorted(result.values(), key=ev.sort_key)


def build_docs_schedule(docs: Mapping[str, Any], arm: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """RETIRING events for docs dates that ARM does not already report for the same model on the same day."""
    catalog = _Catalog(arm)
    base = {"kind": ev.SCHEDULED, "category": ev.LIFECYCLE, "source": SOURCE, "type": ev.RETIRING}
    result: dict[str, dict[str, Any]] = {}
    for row in docs["rows"].values():
        if not row["retirement"]:
            continue
        day = ev.local_date(row["retirement"])
        matches = catalog.match(row)
        arm_days = sorted(
            {
                ev.local_date(record["inference_retirement"])
                for model in matches
                for record in model["regions"].values()
                if record["inference_retirement"]
            }
        )
        if day in arm_days:
            continue
        identity = {**base, "date": day, "model": catalog.ref(row, matches), "sku": None, "field": "inference"}
        details: dict[str, Any] = {"vendor": row["vendor"], "lifecycle": row["lifecycle"]}
        if row["replacement"]:
            details["replacement"] = row["replacement"]
        if matches:
            details["arm_retirement_dates"] = arm_days
        event_id = ev.event_id(identity)
        result[event_id] = {
            "id": event_id,
            **identity,
            "effective_at": row["retirement"],
            "billing": _billing(matches),
            "details": details,
        }
    return sorted(result.values(), key=ev.sort_key)
