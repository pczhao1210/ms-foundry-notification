"""Map parsed Retail Prices model names to ARM models (format, name).

The two APIs share no key, so this is a best-effort name match against the current ARM snapshot. Explicit
aliases cover irregular names; everything else goes through prefix rules. A name maps only when exactly one ARM
model matches; anything else stays unmapped instead of guessing.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

# (price product, parsed price model) -> ARM model name. Every target must exist in the ARM fixture (see tests).
ALIASES: dict[tuple[str, str], str] = {
    ("Azure Deepseek Models", "V3.2 SP"): "DeepSeek-V3.2-Speciale",
    ("Azure Fireworks Models", "FW DS-V4.1-Flash"): "FW-DeepSeek-V4.1-Flash",
    ("Azure Fireworks Models", "FW MiniMax 3"): "FW-MiniMax-M3",
    ("Azure Llama Models", "Llama 3.3 70B"): "Llama-3.3-70B-Instruct",
    ("Azure Llama Models", "Llama 4 Maverick 17B"): "Llama-4-Maverick-17B-128E-Instruct-FP8",
    ("Azure Mistral Models", "Codestral"): "Codestral-2501",
    ("Azure Mistral Models", "MM3.5"): "mistral-medium-3-5",
    ("Cohere Models", "Command A Plus"): "Cohere-command-a-plus-05-2026",
    ("Cohere Models", "Embed v4 Img"): "embed-v-4-0",
    ("Cohere Models", "Embed v4 Txt"): "embed-v-4-0",
    ("Azure OpenAI", "embedding-ada"): "text-embedding-ada-002",
    # The modality sits before a stamp or another model word here; the rules only strip it at the end.
    ("Azure OpenAI Media", "gpt rt aud 0828"): "gpt-realtime",
    ("Azure OpenAI Media", "gpt rt img 0828"): "gpt-realtime",
    ("Azure OpenAI Media", "gpt rt txt 0828"): "gpt-realtime",
    ("Azure OpenAI Media", "gpt rt aud mini"): "gpt-realtime-mini",
    ("Azure OpenAI Media", "gpt rt img mini"): "gpt-realtime-mini",
    ("Azure OpenAI Media", "gpt rt txt mini"): "gpt-realtime-mini",
    ("Azure OpenAI Media", "gpt rt aud mn 1215"): "gpt-realtime-mini",
    ("Azure OpenAI Media", "gpt rt img mn 1215"): "gpt-realtime-mini",
    ("Azure OpenAI Media", "gpt rt txt mn 1215"): "gpt-realtime-mini",
    ("Azure OpenAI Media", "gpt aud mn txt 1215"): "gpt-audio-mini",
    ("Azure OpenAI Media", "gpt 4o tcrb d aud"): "gpt-4o-transcribe-diarize",
    ("Azure OpenAI Media", "gpt 4o tcrb d txt"): "gpt-4o-transcribe-diarize",
    ("Azure OpenAI Media", "gpt4o mn trscb aud 1215"): "gpt-4o-mini-transcribe",
    ("Azure OpenAI Media", "gpt4o mn trscb txt 1215"): "gpt-4o-mini-transcribe",
    ("Azure OpenAI Media", "gpt4o mn tts aud 1215"): "gpt-4o-mini-tts",
    ("Azure OpenAI Media", "gpt4o mn tts txt 1215"): "gpt-4o-mini-tts",
}

# Price products whose model names omit the vendor prefix used by ARM.
PREFIXES: dict[str, tuple[str, ...]] = {
    "Azure OpenAI": ("gpt-",),
    "Azure OpenAI GPT5": ("gpt-",),
    "Azure OpenAI GPT6": ("gpt-",),
    "Azure OpenAI Media": ("gpt-",),
    "Azure Deepseek Models": ("DeepSeek-",),
    "Azure Grok Models": ("grok-",),
    "Azure Kimi": ("Kimi-",),
    "Azure Mistral Models": ("Mistral-",),
    "Cohere Models": ("Cohere-",),
    "MAI Models": ("MAI-",),
}

_SEPARATORS = re.compile(r"[\s._-]+")
# Trailing words that describe the meter rather than the model, stripped in this order and at most once each:
# the token modality, then a date/version stamp (MMDD / MMDDYYYY). "gpt-4o-aud-0603" must not become "gpt-4o".
_MODALITY = re.compile(r"[\s_-]+(?:aud|audio|txt|text|img|image)$", re.IGNORECASE)
_STAMP = re.compile(r"[\s_-]+\d{4}(?:\d{4})?$")
_ABBREVIATIONS = {
    "aud": "audio", "img": "image", "rt": "realtime", "mn": "mini", "trscb": "transcribe", "tcrb": "transcribe"
}
_ABBREVIATION = re.compile(rf"(?<![^\s_-])(?:{'|'.join(_ABBREVIATIONS)})(?![^\s_-])", re.IGNORECASE)


def normalize_name(name: str) -> str:
    return _SEPARATORS.sub("", name.casefold())


def _stripped_forms(name: str) -> list[str]:
    without_modality = _MODALITY.sub("", name)
    forms = [without_modality, _STAMP.sub("", without_modality)]
    return [form for form in dict.fromkeys(forms) if form and form != name]


def _candidates(product: str, name: str) -> set[str]:
    expanded = _ABBREVIATION.sub(lambda match: _ABBREVIATIONS[match.group().casefold()], name)
    return {normalize_name(prefix + form) for prefix in ("", *PREFIXES.get(product, ())) for form in (name, expanded)}


class ArmIndex:
    def __init__(self, arm: Mapping[str, Any] | None) -> None:
        self._targets: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self._names: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for model in (arm or {}).get("models", {}).values():
            target = (model["format"], model["name"])
            self._targets[normalize_name(model["name"])].add(target)
            self._names[model["name"]].add(target)

    def resolve(self, product: str, name: str) -> dict[str, str] | None:
        alias = ALIASES.get((product, name))
        if alias is not None:
            return self._unique(self._names.get(alias, set()))
        # Most specific form first, so "V4 Flash 0731" prefers DeepSeek-V4-Flash-0731 over DeepSeek-V4-Flash.
        for form in (name, *_stripped_forms(name)):
            hits = set().union(*(self._targets.get(c, set()) for c in _candidates(product, form)))
            if hits:
                return self._unique(hits)
        return None

    @staticmethod
    def _unique(hits: set[tuple[str, str]]) -> dict[str, str] | None:
        if len(hits) != 1:
            return None
        fmt, name = next(iter(hits))
        return {"format": fmt, "name": name}


def map_price_models(
    families: Iterable[tuple[str, str]], arm: Mapping[str, Any] | None
) -> dict[tuple[str, str], dict[str, str] | None]:
    index = ArmIndex(arm)
    return {family: index.resolve(*family) for family in set(families)}


def annotate_price_events(events: list[dict[str, Any]], arm: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Add `model` {format, name} and `mapped` to token price events; IDs are unchanged."""
    families = {(e["price_model"]["product"], e["price_model"]["name"]) for e in events if e.get("price_model")}
    mapping = map_price_models(families, arm)
    for event in events:
        if event.get("price_model"):
            target = mapping[(event["price_model"]["product"], event["price_model"]["name"])]
            event["mapped"] = target is not None
            if target:
                event["model"] = target
        elif event.get("unparsed"):
            event["mapped"] = False
    return events
