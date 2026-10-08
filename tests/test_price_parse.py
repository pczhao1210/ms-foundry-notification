import pytest

from builders import load_fixture, price_row
from core.price_parse import (
    DIMENSIONS,
    HOUR_UNIT,
    TOKEN_UNIT,
    classify,
    label_meters,
    normalize_prices,
    parse_meter,
)

ITEMS = load_fixture("retail_prices_foundry_eastus2_2026-10-06.json")["Items"]


def _consumption(sku_name):
    return next(item for item in ITEMS if item["skuName"] == sku_name and item["type"] == "Consumption")


@pytest.mark.parametrize(
    ("sku_name", "expected"),
    [
        ("5.4 opt Dz", "token"),
        ("4.3 Inp Glbl", "token"),
        ("Provisioned Managed Global", "provisioned"),
        ("3.3 70b Inp FT DZ", None),
        ("4.1 pp inp Gl", None),
        ("gpt-4.1-ft hosting regional", None),
        ("Phi-3-Medium-128K-Instruct-Input-Finetuned", None),
        ("Sora 2 glbl", None),
        ("OCR DZone", None),
        ("Mngd H100_80GB Gl", None),
    ],
)
def test_classify_fixture_rows(sku_name, expected):
    assert classify(_consumption(sku_name)) == expected


def test_reservations_are_out_of_scope():
    assert all(classify(item) is None for item in ITEMS if item["type"] == "Reservation")


def test_normalize_converts_token_prices_to_per_million():
    snap = normalize_prices(ITEMS, collected_at="2026-10-06T00:00:00Z")
    by_sku = {price["sku"]: price for price in snap["prices"].values() if price["product"] != "Azure OpenAI"}
    assert by_sku["5.4 opt Dz"]["price"] == 16.5
    assert by_sku["5.4 opt Dz"]["unit"] == TOKEN_UNIT
    assert by_sku["4.3 Inp Glbl"]["price"] == 1.25
    assert snap["collected_at"] == "2026-10-06T00:00:00Z"


def test_normalized_snapshot_contains_only_token_and_ptu_prices():
    prices = list(normalize_prices(ITEMS, collected_at="2026-10-06T00:00:00Z")["prices"].values())
    assert 0 < len(prices) < len(ITEMS)
    assert {price["unit"] for price in prices} == {TOKEN_UNIT, HOUR_UNIT}
    assert all((price["billing"] == "provisioned") == (price["unit"] == HOUR_UNIT) for price in prices)
    assert not any("FT" in price["meter"] for price in prices)


@pytest.mark.parametrize(
    ("sku_name", "expected"),
    [
        ("5.6 sol ShortCo Cd Wr Std Gl", ("5.6 sol", "global", "standard", "short", "cache_write")),
        ("5.4 longco batch cd inp Dz", ("5.4", "datazone", "batch", "long", "cached_input")),
        ("5.4 pp opt Gl", ("5.4", "global", "priority", None, "output")),
        ("56luna LoCo Cd Inp Fl Gl", ("56luna", "global", "flex", "long", "cached_input")),
        ("gpt-5-codex-ccchd-inp-dzone", ("gpt-5-codex", "datazone", "standard", None, "cached_input")),
        ("o4-mini 0416 Batch Outp Data Zone", ("o4-mini 0416", "datazone", "batch", None, "output")),
        ("codex mini inp cchd rgnl", ("codex mini", "regional", "standard", None, "cached_input")),
        ("V4 Flash 0731 cached DZ", ("V4 Flash 0731", "datazone", "standard", None, "cached_input")),
        ("Phi-4-mini-reasoning-Input", ("Phi-4-mini-reasoning", None, "standard", None, "input")),
        ("4.3 Cached Inp DZ L", ("4.3", "datazone", "standard", "long", "cached_input")),
        ("gpt-image-1-inp-cached-img-dzone", ("gpt-image-1 img", "datazone", "standard", None, "cached_input")),
        ("gpt img 1.5 in cd txt DZ", ("gpt img 1.5 txt", "datazone", "standard", None, "cached_input")),
        ("gpt rt aud mn cd in DZ 1215", ("gpt rt aud mn 1215", "datazone", "standard", None, "cached_input")),
        ("gpt4o mn trscb txt out rg 1215", ("gpt4o mn trscb txt 1215", "regional", "standard", None, "output")),
        ("text-embedding-3-large-glbl", ("text-embedding-3-large", "global", "standard", None, "input")),
        ("Embed v4 Img DZ", ("Embed v4 Img", "datazone", "standard", None, "input")),
    ],
)
def test_parse_meter(sku_name, expected):
    dims = parse_meter(sku_name)
    assert (dims["model"], *(dims[d] for d in DIMENSIONS)) == expected


@pytest.mark.parametrize(
    "sku_name",
    [
        pytest.param("embedding-ada-glbl-new", id="unknown-word-after-dimensions"),
        pytest.param("gpt-35-trb16K-Batch-125-Inp-glbl", id="unknown-word-between-dimensions"),
        pytest.param("4.3 Inp L DZ", id="long-marker-not-last"),
        pytest.param("gpt aud mn in 1215 DZ", id="stamp-not-last"),
        pytest.param("gpt-image-1-inp-img-txt-dzone", id="two-modalities"),
        pytest.param("babbage-002-base-glbl", id="no-token-type"),
        pytest.param("Inp Gl", id="no-model-part"),
        pytest.param("5.6 sol Cd Opt Gl", id="cached-output"),
        pytest.param("5.4 pp Batch inp Gl", id="two-tiers"),
    ],
)
def test_parse_meter_rejects_names_outside_the_scheme(sku_name):
    assert parse_meter(sku_name) is None


def test_colliding_meters_leave_the_whole_model_unparsed():
    records = [
        {"product": "P", "sku": name, "billing": "token"}
        for name in ("5.4 inp Gl", "5.4 Inp Glbl", "5.4 opt Gl", "5.5 inp Gl")
    ]
    records.append({"product": "P", "sku": "Provisioned Managed Global", "billing": "provisioned"})
    labels = label_meters(records)
    assert [name for (_, name), dims in labels.items() if dims] == ["5.5 inp Gl"]
    assert ("P", "Provisioned Managed Global") not in labels


def test_fixture_meters_parse_into_models():
    labels = label_meters(normalize_prices(ITEMS, collected_at="2026-10-06T00:00:00Z")["prices"].values())
    parsed = {name: dims for name, dims in labels.items() if dims}
    assert len(parsed) / len(labels) > 0.99
    for model, count in (("5.4", 30), ("5.6 sol", 32)):
        meters = [d for (product, _), d in parsed.items() if product == "Azure OpenAI GPT5" and d["model"] == model]
        assert len(meters) == count
        assert len({tuple(d[x] for x in DIMENSIONS) for d in meters}) == count


def test_future_prices_are_separate_and_current_selection_is_order_independent():
    rows = [price_row(1.0), price_row(2.0, effective="2026-10-07T00:00:00Z"),
            price_row(3.0, effective="2026-11-01T00:00:00Z"), price_row(4.0, effective="2026-12-01T00:00:00Z")]
    current = normalize_prices(rows, collected_at="2026-10-07T00:00:00Z")
    assert current == normalize_prices(reversed(rows), collected_at="2026-10-07T00:00:00Z")
    assert [record["price"] for record in current["prices"].values()] == [2.0]
    assert [record["price"] for records in current["future_prices"].values() for record in records] == [3.0, 4.0]


def test_future_only_meter_preserves_known_current_price():
    previous = normalize_prices([price_row(1.0)], collected_at="2026-10-06T00:00:00Z")
    rows = [price_row(2.0, effective="2026-11-01T00:00:00Z")]
    current = normalize_prices(rows, collected_at="2026-10-07T00:00:00Z", previous=previous)
    assert current["prices"] == previous["prices"]
    assert normalize_prices(rows, collected_at="2026-10-07T00:00:00Z")["prices"] == {}
    effective = normalize_prices(rows, collected_at="2026-11-01T00:00:00Z", previous=current)
    assert [record["price"] for record in effective["prices"].values()] == [2.0]
    assert effective["future_prices"] == {}
