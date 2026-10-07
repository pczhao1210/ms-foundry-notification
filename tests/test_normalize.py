from builders import arm_item, load_fixture, sku, snapshot


def _fixture_snapshot():
    return snapshot({"EastUS2": load_fixture("models_eastus2_2026-09-01.json")["value"]})


def test_fixture_keeps_only_aiservices_and_mai_deduplicated():
    snap = _fixture_snapshot()
    assert snap["regions"] == ["eastus2"]
    assert len(snap["models"]) == 213
    assert "Microsoft|MAI-Thinking-1|2026-06-01" in snap["models"]


def test_same_sku_name_with_different_scope_is_kept_separately():
    skus = _fixture_snapshot()["models"]["OpenAI|gpt-4o|2024-08-06"]["regions"]["eastus2"]["skus"]
    assert {"GlobalStandard|Base", "GlobalStandard|Finetune"} <= skus.keys()


def test_region_record_dates_and_billing():
    record = _fixture_snapshot()["models"]["OpenAI|gpt-4o|2024-05-13"]["regions"]["eastus2"]
    assert record["inference_retirement"] == "2026-12-09T00:00:00Z"
    assert record["skus"]["Standard|Base"]["retirement"] == "2026-03-31T00:00:00Z"
    assert record["skus"]["ProvisionedManaged|Base"]["billing"] == "provisioned"
    assert record["skus"]["GlobalBatch|Base"]["billing"] == "token"


def test_duplicate_views_are_ignored_and_mai_skus_are_merged():
    raw = {
        "eastus": [
            arm_item(kind="OpenAI"),
            arm_item(kind="MaaS"),
            arm_item(skus=[sku("GlobalStandard")]),
            arm_item(kind="MAI", skus=[sku("DataZoneStandard")]),
        ]
    }
    model = snapshot(raw)["models"]["OpenAI|gpt-x|1"]
    assert list(model["regions"]["eastus"]["skus"]) == ["DataZoneStandard|Base", "GlobalStandard|Base"]


def test_replacement_and_dates_are_normalized_to_utc():
    replacement = {"targetModelName": "gpt-y", "targetModelVersion": "2", "autoUpgradeStartDate": "2026-11-24"}
    record = snapshot({"eastus": [arm_item(inference="2026-12-09T08:00:00+08:00", replacement=replacement)]})[
        "models"
    ]["OpenAI|gpt-x|1"]["regions"]["eastus"]
    assert record["inference_retirement"] == "2026-12-09T00:00:00Z"
    assert record["replacement"] == {"model": "gpt-y", "version": "2", "auto_upgrade_start": "2026-11-24T00:00:00Z"}


def test_failed_region_carries_forward_previous_data():
    prev = snapshot({"eastus": [arm_item()], "westus": [arm_item(), arm_item("west-only")]}, "2026-10-06T00:00:00Z")
    curr = snapshot({"eastus": [arm_item()]}, failed_regions=["WestUS"], previous=prev)
    assert curr["regions"] == ["eastus", "westus"]
    assert curr["stale_regions"] == ["westus"]
    assert curr["failed_regions"] == []
    assert curr["models"]["OpenAI|west-only|1"]["regions"] == prev["models"]["OpenAI|west-only|1"]["regions"]


def test_failed_region_without_baseline_is_reported():
    curr = snapshot({"eastus": [arm_item()]}, failed_regions=["westus"])
    assert curr["regions"] == ["eastus"]
    assert curr["stale_regions"] == []
    assert curr["failed_regions"] == ["westus"]
