"""Filter Azure Retail Prices rows to in-scope Foundry meters and normalize them into a price snapshot."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .events import to_utc_iso

SCHEMA_VERSION = 1
TOKEN_UNIT = "USD/1M tokens"
HOUR_UNIT = "USD/hour"

_TOKEN_MULTIPLIER = {"1K": 1000.0, "1M": 1.0}
_HOUR_UNITS = frozenset({"1 Hour", "1/Hour"})
_EXCLUDED_PRODUCT = re.compile(
    r"^(Managed Compute|Microsoft Agent Pre-Purchase Plan|Foundry Local|Azure OpenAI PP FT)", re.IGNORECASE
)
_EXCLUDED_TEXT = re.compile(
    r"FT|(?i:\bft\b|\brft\b|finetun|training|\btrng\b|hstng|deployment hosting|free meter)"
)

# Closed value sets in display order; every parsed token meter has exactly these dimensions.
DIMENSION_VALUES: dict[str, tuple[str | None, ...]] = {
    "deployment": ("global", "datazone", "regional", None),
    "tier": ("standard", "priority", "flex", "batch"),
    "context": (None, "short", "long"),
    "token": ("input", "cached_input", "cache_write", "output"),
}
DIMENSIONS = tuple(DIMENSION_VALUES)

_WORD = re.compile(r"[^\s_-]+")
_DATA_ZONE = re.compile(r"\bdata\s+zone\b", re.IGNORECASE)
_WORDS: dict[str, tuple[str, str]] = {
    **dict.fromkeys(("inp", "inpt", "input", "in"), ("io", "input")),
    **dict.fromkeys(("opt", "outp", "outpt", "output", "out"), ("io", "output")),
    **dict.fromkeys(("cd", "ch", "cached", "cache", "cchd", "ccchd"), ("cache", "cached")),
    "wr": ("write", "write"),
    **dict.fromkeys(("gl", "glbl", "global"), ("deployment", "global")),
    **dict.fromkeys(("dz", "dzn", "dzone", "datazone"), ("deployment", "datazone")),
    **dict.fromkeys(("regional", "regnl", "rgnl", "regn", "rg"), ("deployment", "regional")),
    "std": ("tier", "standard"),
    "pp": ("tier", "priority"),
    **dict.fromkeys(("flex", "fl"), ("tier", "flex")),
    "batch": ("tier", "batch"),
    **dict.fromkeys(("shortco", "shco"), ("context", "short")),
    **dict.fromkeys(("longco", "loco"), ("context", "long")),
}
# Only valid as the last word: Grok's "4.3 Inp DZ L" meters cost exactly 2x their base meters (long context).
_LAST_WORDS: dict[str, tuple[str, str]] = {"l": ("context", "long")}
# Allowed among the dimension words but kept in the model name, so each modality stays its own price family.
_MODALITIES = frozenset({"aud", "audio", "txt", "text", "img", "image"})
_STAMP = re.compile(r"\d{4}(?:\d{4})?")
_TOKEN_TYPES = {
    ("input", None, None): "input",
    ("output", None, None): "output",
    ("input", "cached", None): "cached_input",
    (None, "cached", None): "cached_input",
    (None, "cached", "write"): "cache_write",
}


def classify(item: Mapping[str, Any]) -> str | None:
    """Return 'token' or 'provisioned' for in-scope meters, None for everything else."""
    if item.get("type") != "Consumption":
        return None
    product = item.get("productName") or ""
    meter = item.get("meterName") or ""
    text = " ".join((product, item.get("skuName") or "", meter))
    if _EXCLUDED_PRODUCT.match(product) or _EXCLUDED_TEXT.search(text):
        return None
    unit = item.get("unitOfMeasure")
    if unit in _TOKEN_MULTIPLIER and meter.endswith("Tokens"):
        return "token"
    if unit in _HOUR_UNITS and "Provisioned" in text:
        return "provisioned"
    return None


def price_key(item: Mapping[str, Any]) -> str:
    fields = ("meterId", "armRegionName", "type", "reservationTerm", "tierMinimumUnits")
    return "|".join("" if item.get(field) is None else str(item[field]) for field in fields)


def parse_meter(sku: str) -> dict[str, str | None] | None:
    """Split a token SKU name into its model part and billing dimensions; None when it doesn't fit the scheme."""
    text = _DATA_ZONE.sub("DataZone", sku)
    words = list(_WORD.finditer(text))
    start = next((i for i, word in enumerate(words) if word.group().casefold() in _WORDS), None)
    # Requires a leading model part; an unknown word among the dimensions could change their meaning.
    if not start:
        return None
    found: dict[str, list[str]] = defaultdict(list)
    model_words: list[str] = []
    last = len(words) - 1
    for index, word in enumerate(words[start:], start):
        key = word.group().casefold()
        if key in _WORDS:
            dimension, value = _WORDS[key]
        elif index == last and key in _LAST_WORDS:
            dimension, value = _LAST_WORDS[key]
        elif key in _MODALITIES:
            dimension, value = "modality", key
            model_words.append(word.group())
        elif index == last and _STAMP.fullmatch(key):
            model_words.append(word.group())
            continue
        else:
            return None
        found[dimension].append(value)
    if any(len(values) > 1 for values in found.values()):
        return None
    single = {dimension: values[0] for dimension, values in found.items()}
    model = " ".join((text[: words[start].start()].rstrip(" _-"), *model_words))
    io = single.get("io")
    # Embedding meters omit the direction; embeddings only bill input tokens.
    if io is None and "embed" in model.casefold():
        io = "input"
    token = _TOKEN_TYPES.get((io, single.get("cache"), single.get("write")))
    if token is None:
        return None
    return {
        "model": model,
        "deployment": single.get("deployment"),
        "tier": single.get("tier", "standard"),
        "context": single.get("context"),
        "token": token,
    }


def label_meters(records: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, str | None] | None]:
    """Parse token meters keyed by (product, sku); a model whose meters collide on dimensions stays unparsed."""
    names = sorted({(record["product"], record["sku"]) for record in records if record["billing"] == "token"})
    labels = {name: parse_meter(name[1]) for name in names}
    combos: dict[tuple[str, Any], set[tuple[Any, ...]]] = defaultdict(set)
    ambiguous: set[tuple[str, Any]] = set()
    for (product, _), dims in labels.items():
        if dims is None:
            continue
        family, combo = (product, dims["model"]), tuple(dims[d] for d in DIMENSIONS)
        if combo in combos[family]:
            ambiguous.add(family)
        combos[family].add(combo)
    return {
        name: None if dims is None or (name[0], dims["model"]) in ambiguous else dims
        for name, dims in labels.items()
    }


def normalize_prices(
    items: Iterable[Mapping[str, Any]], *, collected_at: str, previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    collected_at = to_utc_iso(collected_at)
    prices: dict[str, dict[str, Any]] = {}
    future: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for item in items:
        billing = classify(item)
        if billing is None:
            continue
        raw_price = float(item["unitPrice"])
        if billing == "token":
            price, unit = raw_price * _TOKEN_MULTIPLIER[item["unitOfMeasure"]], TOKEN_UNIT
        else:
            price, unit = raw_price, HOUR_UNIT
        record = {
            "region": item["armRegionName"],
            "product": item["productName"],
            "sku": item["skuName"],
            "meter": item["meterName"],
            "meter_id": item["meterId"],
            "billing": billing,
            "price": round(price, 9),
            "unit": unit,
            "effective_start": to_utc_iso(item.get("effectiveStartDate")),
        }
        key, effective = price_key(item), record["effective_start"]
        if effective and effective > collected_at:
            future[key][effective] = record
        elif key not in prices or (effective or "") > (prices[key]["effective_start"] or ""):
            prices[key] = record
    for key in future:
        if key not in prices and key in (previous or {}).get("prices", {}):
            prices[key] = dict(previous["prices"][key])
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": collected_at,
        "prices": dict(sorted(prices.items())),
        "future_prices": {key: [records[at] for at in sorted(records)] for key, records in sorted(future.items())},
    }
