import pytest

from builders import arm_item, load_fixture, price_snapshot, snapshot
from core.price_alias import ALIASES, annotate_price_events, map_price_models
from core.price_parse import label_meters


@pytest.fixture(scope="module")
def arm():
    return snapshot({"eastus2": load_fixture("models_eastus2_2026-09-01.json")["value"]})


@pytest.fixture(scope="module")
def families():
    prices = price_snapshot(load_fixture("retail_prices_foundry_eastus2_2026-10-06.json")["Items"])
    labels = label_meters(prices["prices"].values())
    return {(product, dims["model"]) for (product, _), dims in labels.items() if dims}


def test_alias_targets_exist_in_catalog(arm):
    names = {model["name"] for model in arm["models"].values()}
    assert sorted(set(ALIASES.values()) - names) == []


@pytest.mark.parametrize(
    "family, expected",
    [
        (("Azure OpenAI GPT5", "5.4"), "gpt-5.4"),
        (("Azure OpenAI GPT5", "54"), "gpt-5.4"),
        (("Azure OpenAI GPT5", "56sol"), "gpt-5.6-sol"),
        (("Azure OpenAI GPT5", "chat-latest 05052026"), "gpt-chat-latest"),
        (("Azure OpenAI", "gpt 4o 0513"), "gpt-4o"),
        (("Azure OpenAI", "gpt-4o-mini-transcribe-aud"), "gpt-4o-mini-transcribe"),
        (("Azure OpenAI Media", "Image 2 img"), "gpt-image-2"),
        (("Azure OpenAI Media", "gpt img 1.5 img"), "gpt-image-1.5"),
        (("Azure OpenAI Media", "gpt aud 0828 txt"), "gpt-audio"),
        (("Azure OpenAI Media", "gpt rt 1.5 aud"), "gpt-realtime-1.5"),
        (("Azure OpenAI Media", "gpt rt aud mn 1215"), "gpt-realtime-mini"),
        (("Azure OpenAI", "gpt-image-1 img"), "gpt-image-1"),
        (("Azure OpenAI", "text-embedding-3-large"), "text-embedding-3-large"),
        (("Cohere Models", "Embed v4 Txt"), "embed-v-4-0"),
        (("Azure Deepseek Models", "V4 Flash 0731"), "DeepSeek-V4-Flash-0731"),
        (("Azure Deepseek Models", "V4 Flash"), "DeepSeek-V4-Flash"),
        (("Azure Grok Models", "Code Fast 1"), "grok-code-fast-1"),
        (("Cohere Models", "Command A"), "cohere-command-a"),
        (("Azure Llama Models", "Llama 3.3 70B"), "Llama-3.3-70B-Instruct"),
        # gpt-4o-audio-preview meters must not collapse into gpt-4o.
        (("Azure OpenAI", "gpt-4o-aud-0603"), None),
        (("Azure OpenAI", "gpt-4o-aud-0603-txt"), None),
        (("Azure Kimi", "Model 6"), None),
        (("Azure Grok Models", "Grok 4.1"), None),
    ],
)
def test_maps_fixture_price_names(arm, families, family, expected):
    assert family in families
    target = map_price_models([family], arm)[family]
    assert (target["name"] if target else None) == expected


def test_fixture_coverage(arm, families):
    mapping = map_price_models(families, arm)
    # 162 of 243 parsed price models at the time the fixtures were captured.
    assert sum(1 for target in mapping.values() if target) >= 155


def test_ambiguous_names_stay_unmapped():
    arm = snapshot({"eastus2": [arm_item("x-1"), arm_item("X_1", fmt="Other")]})
    assert map_price_models([("P", "x 1")], arm) == {("P", "x 1"): None}


def test_annotate_adds_model_and_mapped_flag(arm):
    meter = {"product": "p", "sku": "s", "meter": "m"}
    events = [
        {"id": "a", "price_model": {"product": "Azure OpenAI GPT5", "name": "54"}},
        {"id": "b", "price_model": {"product": "Azure Kimi", "name": "Model 6"}},
        {"id": "c", "meter": meter, "unparsed": True},
        {"id": "d", "meter": meter, "billing": ["provisioned"]},
    ]
    annotate_price_events(events, arm)
    assert events[0]["model"] == {"format": "OpenAI", "name": "gpt-5.4"}
    assert events[0]["mapped"] is True
    assert events[1]["mapped"] is False and "model" not in events[1]
    assert events[2]["mapped"] is False
    assert "mapped" not in events[3]
    assert [event["id"] for event in events] == ["a", "b", "c", "d"]
