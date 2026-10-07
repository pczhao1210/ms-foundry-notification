import pytest

from builders import arm_item, load_fixture, price_snapshot, sku, snapshot
from core.query import get_model, get_prices, search_models

REPLACEMENT = {"targetModelName": "gpt-b", "targetModelVersion": "2"}
ARM = snapshot(
    {
        "eastus2": [
            arm_item(
                "gpt-a",
                "1",
                skus=[sku("GlobalStandard"), sku("ProvisionedManaged")],
                inference="2027-01-01T00:00:00Z",
                replacement=REPLACEMENT,
            ),
            arm_item("gpt-b", "2", status="Preview"),
            arm_item("claude-x", "1", fmt="Anthropic"),
        ],
        "westus": [
            arm_item("gpt-a", "1", status="Deprecating", inference="2026-12-01T00:00:00Z"),
            arm_item("gpt-b", "2", status="Preview"),
        ],
    }
)


def _names(result):
    return [model["name"] for model in result["models"]]


def test_search_without_filters_summarizes_each_version():
    result = search_models(ARM)
    assert _names(result) == ["claude-x", "gpt-a", "gpt-b"]
    assert result["truncated"] is False
    gpt_a = result["models"][1]
    assert gpt_a["region_count"] == 2
    assert gpt_a["billing"] == ["provisioned", "token"]
    assert gpt_a["inference_retirement"] == "2026-12-01T00:00:00Z"
    assert gpt_a["status_counts"] == {"Deprecating": 1, "GenerallyAvailable": 1}
    assert gpt_a["replacement"] == [{"model": "gpt-b", "version": "2", "auto_upgrade_start": None}]
    assert "status_counts" not in result["models"][2]


@pytest.mark.parametrize(
    "filters, names, region_count",
    [
        ({"vendor": "anthropic"}, ["claude-x"], 1),
        ({"status": "Deprecating"}, ["gpt-a"], 1),
        ({"region": "WestUS", "name_contains": "A"}, ["gpt-a"], 1),
        ({"billing": "provisioned"}, ["gpt-a"], 1),
        ({"name_contains": "GPT"}, ["gpt-a", "gpt-b"], 2),
    ],
)
def test_search_filters_apply_per_region(filters, names, region_count):
    result = search_models(ARM, **filters)
    assert _names(result) == names
    assert result["models"][0]["region_count"] == region_count


def test_search_limit_and_validation():
    result = search_models(ARM, limit=1)
    assert result["truncated"] is True and result["total"] == 3 and len(result["models"]) == 1
    with pytest.raises(ValueError):
        search_models(ARM, status="GA")


def test_get_model_groups_identical_regions():
    result = get_model(ARM, name="GPT-B")
    assert result["found"] is True
    [model] = result["models"]
    [group] = model["availability"]
    assert group["regions"] == ["eastus2", "westus"]
    assert group["status"] == "Preview"
    assert group["skus"][0]["name"] == "GlobalStandard"

    [gpt_a] = get_model(ARM, name="gpt-a", version="1", vendor="openai")["models"]
    assert [g["regions"] for g in gpt_a["availability"]] == [["eastus2"], ["westus"]]


def test_get_model_suggests_names_when_missing():
    result = get_model(ARM, name="gpt")
    assert result["found"] is False
    assert result["suggestions"] == ["gpt-a", "gpt-b"]
    with pytest.raises(ValueError):
        get_model(ARM, name=" ")


@pytest.fixture(scope="module")
def catalogs():
    arm = snapshot({"eastus2": load_fixture("models_eastus2_2026-09-01.json")["value"]})
    prices = price_snapshot(load_fixture("retail_prices_foundry_eastus2_2026-10-06.json")["Items"])
    return prices, arm


def test_prices_by_arm_model_include_every_price_name(catalogs):
    result = get_prices(*catalogs, model="GPT-5.4")
    assert result["match"] == "arm_model"
    assert result["unit"] == "USD/1M tokens"
    assert {row["price_model"] for row in result["prices"]} == {"5.4", "54"}
    assert all(row["model"] == {"format": "OpenAI", "name": "gpt-5.4"} for row in result["prices"])
    first = result["prices"][0]
    assert (first["deployment"], first["tier"], first["token"], first["price"]) == ("global", "standard", "input", 2.5)
    assert first["regions"] == ["eastus2"]


def test_prices_filters_and_fallback(catalogs):
    result = get_prices(*catalogs, model="gpt-5.4", deployment="datazone", region="EASTUS2")
    assert result["total"] > 0 and {row["deployment"] for row in result["prices"]} == {"datazone"}
    assert get_prices(*catalogs, model="gpt-5.4", region="westus")["total"] == 0

    fallback = get_prices(*catalogs, model="Llama 3.3")
    assert fallback["match"] == "name_contains"
    assert {row["price_model"] for row in fallback["prices"]} == {"Llama 3.3 70B"}

    with pytest.raises(ValueError):
        get_prices(*catalogs, model="gpt-5.4", deployment="standard")
    with pytest.raises(ValueError):
        get_prices(*catalogs, model="")
