from builders import arm_item, price_row, price_snapshot, sku, snapshot
from core import events as ev
from core.diff import diff_models, diff_prices

PREV_AT = "2026-10-06T00:00:00Z"
CURR_AT = "2026-10-07T00:00:00Z"


def _diff(prev_raw, curr_raw):
    prev = snapshot(prev_raw, PREV_AT)
    curr = snapshot(curr_raw, CURR_AT, previous=prev)
    return diff_models(prev, curr)


def _by_type(events):
    return {event["type"]: event for event in events}


def test_identical_snapshots_produce_no_events():
    raw = {"eastus": [arm_item()], "westus": [arm_item()]}
    assert _diff(raw, raw) == []


def test_input_order_does_not_matter():
    first = arm_item("b", skus=[sku("GlobalStandard"), sku("DataZoneStandard")])
    reordered = arm_item("b", skus=[sku("DataZoneStandard"), sku("GlobalStandard")])
    assert _diff({"eastus": [arm_item("a"), first]}, {"eastus": [reordered, arm_item("a")]}) == []


def test_same_change_in_several_regions_is_one_event():
    prev = {region: [arm_item(status="Preview")] for region in ("eastus", "westus", "swedencentral")}
    curr = {
        "eastus": [arm_item()],
        "westus": [arm_item()],
        "swedencentral": [arm_item(status="Preview")],
    }
    [event] = _diff(prev, curr)
    assert event["type"] == ev.STATUS_CHANGED
    assert (event["old"], event["new"]) == ("Preview", "GenerallyAvailable")
    assert event["regions"] == ["eastus", "westus"]
    assert (event["kind"], event["category"], event["source"]) == ("observed", "lifecycle", "arm")
    assert event["date"] == "2026-10-07"
    assert (event["observed_at"], event["baseline_at"]) == (CURR_AT, PREV_AT)
    assert event["billing"] == ["token"]


def test_different_new_values_are_separate_events():
    prev = {region: [arm_item(status="Preview")] for region in ("eastus", "westus")}
    curr = {"eastus": [arm_item()], "westus": [arm_item(status="Deprecating")]}
    events = _diff(prev, curr)
    assert sorted(event["new"] for event in events) == ["Deprecating", "GenerallyAvailable"]
    assert all(len(event["regions"]) == 1 for event in events)


def test_new_model_and_new_version():
    prev = {"eastus": [arm_item("gpt-x", "1", status="Preview")]}
    curr = {"eastus": [arm_item("gpt-x", "1", status="Preview"), arm_item("gpt-x", "2"), arm_item("brand-new")]}
    events = {e["model"]["name"] + "@" + e["model"]["version"]: e for e in _diff(prev, curr)}
    assert events["brand-new@1"]["type"] == ev.MODEL_ADDED
    new_version = events["gpt-x@2"]
    assert new_version["type"] == ev.NEW_VERSION
    assert new_version["new"] == "GenerallyAvailable"
    assert new_version["details"]["existing_versions"] == [{"version": "1", "status": "Preview"}]
    assert new_version["details"]["skus"] == ["GlobalStandard"]


def test_model_removed():
    [event] = _diff({"eastus": [arm_item(), arm_item("old")]}, {"eastus": [arm_item()]})
    assert event["type"] == ev.MODEL_REMOVED
    assert event["model"]["name"] == "old"


def test_region_added_and_removed():
    prev = {"eastus": [arm_item()], "westus": [arm_item()], "japaneast": []}
    curr = {"eastus": [arm_item()], "westus": [], "japaneast": [arm_item()]}
    events = _by_type(_diff(prev, curr))
    assert events[ev.REGION_REMOVED]["regions"] == ["westus"]
    assert events[ev.REGION_ADDED]["regions"] == ["japaneast"]


def test_sku_changes_keep_scope_and_billing():
    prev = {"eastus": [arm_item(skus=[sku(), sku(scope="Finetune"), sku("Standard")])]}
    curr = {
        "eastus": [
            arm_item(
                skus=[
                    sku(status="Deprecating", retirement="2027-01-01T00:00:00Z"),
                    sku("Standard"),
                    sku("GlobalProvisionedManaged"),
                ]
            )
        ]
    }
    events = _diff(prev, curr)
    assert {(e["type"], e["sku"]["name"], e["sku"]["scope"], e["field"]) for e in events} == {
        (ev.SKU_REMOVED, "GlobalStandard", "Finetune", None),
        (ev.SKU_ADDED, "GlobalProvisionedManaged", "Base", None),
        (ev.SKU_STATUS_CHANGED, "GlobalStandard", "Base", "status"),
        (ev.RETIREMENT_DATE_CHANGED, "GlobalStandard", "Base", "sku"),
    }
    assert _by_type(events)[ev.SKU_ADDED]["billing"] == ["provisioned"]


def test_retirement_and_replacement_changes():
    replacement = {"targetModelName": "gpt-y", "targetModelVersion": "2", "autoUpgradeStartDate": "2026-11-01T00:00:00Z"}
    prev = {"eastus": [arm_item(inference="2026-12-01T00:00:00Z")]}
    curr = {"eastus": [arm_item(inference="2027-01-15T00:00:00Z", replacement=replacement)]}
    events = _by_type(_diff(prev, curr))
    retirement = events[ev.RETIREMENT_DATE_CHANGED]
    assert retirement["field"] == "inference"
    assert (retirement["old"], retirement["new"]) == ("2026-12-01T00:00:00Z", "2027-01-15T00:00:00Z")
    assert events[ev.REPLACEMENT_CHANGED]["old"] is None
    assert events[ev.REPLACEMENT_CHANGED]["new"] == {
        "model": "gpt-y",
        "version": "2",
        "auto_upgrade_start": "2026-11-01T00:00:00Z",
    }


def test_failed_region_does_not_cause_false_removal():
    day1 = snapshot({"eastus": [arm_item()], "westus": [arm_item(), arm_item("west-only")]}, "2026-10-05T00:00:00Z")
    day2 = snapshot({"eastus": [arm_item()]}, "2026-10-06T00:00:00Z", failed_regions=["westus"], previous=day1)
    assert diff_models(day1, day2) == []

    day3 = snapshot({"eastus": [arm_item()], "westus": [arm_item()]}, "2026-10-07T00:00:00Z", previous=day2)
    [event] = diff_models(day2, day3)
    assert event["type"] == ev.MODEL_REMOVED
    assert event["model"]["name"] == "west-only"
    assert event["regions"] == ["westus"]


def test_region_without_baseline_is_not_reported_as_added():
    day1 = snapshot({"eastus": [arm_item()]}, PREV_AT, failed_regions=["westus"])
    day2 = snapshot({"eastus": [arm_item()], "westus": [arm_item(), arm_item("other")]}, CURR_AT, previous=day1)
    assert diff_models(day1, day2) == []


def test_event_ids_are_deterministic_and_unique():
    prev = {"eastus": [arm_item(status="Preview"), arm_item("b")]}
    curr = {"eastus": [arm_item()], "westus": [arm_item()]}
    first, second = _diff(prev, curr), _diff(prev, curr)
    assert [e["id"] for e in first] == [e["id"] for e in second]
    assert len({e["id"] for e in first}) == len(first)


def test_event_date_uses_shanghai_day():
    prev = snapshot({"eastus": [arm_item(status="Preview")]}, "2026-10-06T00:00:00Z")
    curr = snapshot({"eastus": [arm_item()]}, "2026-10-06T17:30:00Z")
    assert diff_models(prev, curr)[0]["date"] == "2026-10-07"


def test_price_change_across_regions_is_one_event():
    prev = price_snapshot([price_row(10.0, region=r) for r in ("eastus2", "westus")], PREV_AT)
    curr = price_snapshot(
        [price_row(8.0, region=r, effective="2026-10-01T00:00:00Z") for r in ("eastus2", "westus")], CURR_AT
    )
    [event] = diff_prices(prev, curr)
    assert event["type"] == ev.PRICE_CHANGED
    assert (event["category"], event["source"], event["direction"]) == ("price", "retail_prices", "down")
    assert event["price_model"] == {"product": "Azure OpenAI GPT5", "name": "5.4"}
    assert (event["meter_count"], event["change_pct"]) == (1, -20.0)
    assert event["regions"] == ["eastus2", "westus"]
    assert event["unit"] == "USD/1M tokens"
    assert event["effective_start"] == "2026-10-01T00:00:00Z"
    assert event["changes"] == [
        {
            "sku": "5.4 opt Gl",
            "deployment": "global",
            "tier": "standard",
            "context": None,
            "token": "output",
            "old": 10.0,
            "new": 8.0,
            "change_pct": -20.0,
        }
    ]


def test_model_wide_change_is_one_event_with_a_row_per_meter():
    skus = {"5.4 Batch opt Dz": 8.25, "5.4 inp Dz": 2.75, "5.4 opt Gl": 15.0, "5.4 inp Gl": 2.5}

    def rows(factor):
        return [
            price_row(price * factor, sku_name=name, region=region)
            for name, price in skus.items()
            for region in ("eastus2", "japaneast", "swedencentral")
            if not (name.endswith("Dz") and region == "japaneast")
        ]

    [event] = diff_prices(price_snapshot(rows(1.0), PREV_AT), price_snapshot(rows(0.8), CURR_AT))
    assert (event["meter_count"], event["change_pct"]) == (4, -20.0)
    assert event["regions"] == ["eastus2", "japaneast", "swedencentral"]
    assert [(c["sku"], c["new"], c.get("regions")) for c in event["changes"]] == [
        ("5.4 inp Gl", 2.0, None),
        ("5.4 opt Gl", 12.0, None),
        ("5.4 inp Dz", 2.2, ["eastus2", "swedencentral"]),
        ("5.4 Batch opt Dz", 6.6, ["eastus2", "swedencentral"]),
    ]


def test_mixed_percentages_are_reported_per_meter():
    prev = price_snapshot([price_row(10.0), price_row(2.0, sku_name="5.4 inp Gl")], PREV_AT)
    curr = price_snapshot([price_row(8.0), price_row(1.0, sku_name="5.4 inp Gl")], CURR_AT)
    [event] = diff_prices(prev, curr)
    assert event["change_pct"] is None
    assert [c["change_pct"] for c in event["changes"]] == [-50.0, -20.0]


def test_same_direction_with_different_amounts_lists_region_prices():
    prev = price_snapshot([price_row(10.0, region="eastus2"), price_row(11.0, region="westus")], PREV_AT)
    curr = price_snapshot([price_row(8.0, region="eastus2"), price_row(8.8, region="westus")], CURR_AT)
    [event] = diff_prices(prev, curr)
    [change] = event["changes"]
    assert (change["old"], change["new"], change["change_pct"], event["change_pct"]) == (None, None, None, None)
    assert change["region_prices"] == [
        {"region": "eastus2", "old": 10.0, "new": 8.0},
        {"region": "westus", "old": 11.0, "new": 8.8},
    ]


def test_opposite_directions_are_separate_events():
    prev = price_snapshot([price_row(10.0, region="eastus2"), price_row(10.0, region="westus")], PREV_AT)
    curr = price_snapshot([price_row(12.0, region="eastus2"), price_row(8.0, region="westus")], CURR_AT)
    assert sorted(e["direction"] for e in diff_prices(prev, curr)) == ["down", "up"]


def test_price_added_and_removed():
    prev = price_snapshot([price_row(1.0, sku_name="old opt Gl")], PREV_AT)
    curr = price_snapshot([price_row(2.0, sku_name="new opt Gl")], CURR_AT)
    events = _by_type(diff_prices(prev, curr))
    assert events[ev.PRICE_ADDED]["price_model"]["name"] == "new"
    [added], [removed] = events[ev.PRICE_ADDED]["changes"], events[ev.PRICE_REMOVED]["changes"]
    assert (added["old"], added["new"], removed["old"], removed["new"]) == (None, 2.0, 1.0, None)


def test_unparsed_meter_falls_back_to_a_meter_event():
    prev = price_snapshot([price_row(1.0, sku_name="babbage-002-base-glbl", product="Azure OpenAI")], PREV_AT)
    curr = price_snapshot([price_row(0.8, sku_name="babbage-002-base-glbl", product="Azure OpenAI")], CURR_AT)
    [event] = diff_prices(prev, curr)
    assert "price_model" not in event
    assert event["meter"]["sku"] == "babbage-002-base-glbl"
    assert (event["old"], event["new"], event["unparsed"]) == (1.0, 0.8, True)


def test_meter_renamed_within_a_model_is_not_merged():
    prev = price_snapshot([price_row(2.5, sku_name="5.4 inp Gl"), price_row(10.0)], PREV_AT)
    curr = price_snapshot([price_row(2.5, sku_name="5.4 Inp Glbl"), price_row(8.0)], CURR_AT)
    events = diff_prices(prev, curr)
    assert sorted(e["type"] for e in events) == [ev.PRICE_ADDED, ev.PRICE_CHANGED, ev.PRICE_REMOVED]
    assert all(e["unparsed"] and "meter" in e for e in events)


def test_provisioned_prices_stay_per_meter():
    row = {"sku_name": "Provisioned Managed Global", "unit": "1 Hour", "product": "Azure OpenAI"}
    prev = price_snapshot([price_row(1.0, **row)], PREV_AT)
    curr = price_snapshot([price_row(1.2, **row)], CURR_AT)
    [event] = diff_prices(prev, curr)
    assert (event["meter"]["sku"], event["billing"], event["unit"]) == (row["sku_name"], ["provisioned"], "USD/hour")
    assert "unparsed" not in event


def test_unit_switch_with_same_per_million_price_is_not_a_change():
    prev = price_snapshot([price_row(0.00125, unit="1K")], PREV_AT)
    curr = price_snapshot([price_row(1.25, unit="1M")], CURR_AT)
    assert diff_prices(prev, curr) == []
