import pytest

from collectors._http import same_host
from collectors.arm_models import ArmModelsCollector, region_names

LOCATIONS = [
    {"name": "eastus2", "displayName": "East US 2"},
    {"name": "swedencentral", "displayName": "Sweden Central"},
]


def test_region_names_map_display_names_and_skip_global():
    assert region_names(["East US 2", "sweden central", "Global", "New Region"], LOCATIONS) == [
        "eastus2",
        "newregion",
        "swedencentral",
    ]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://management.azure.com/subscriptions/x?api-version=1&$skiptoken=a", True),
        ("http://management.azure.com/subscriptions/x", False),
        ("https://management.azure.com.evil.example/x", False),
        ("https://evil.example/?https://management.azure.com", False),
    ],
)
def test_followed_links_must_stay_on_the_expected_host(url, expected):
    assert same_host(url, "management.azure.com") is expected


@pytest.mark.parametrize("page", [{}, {"value": None}, {"value": {}}])
def test_models_rejects_malformed_pages(monkeypatch, page):
    collector = ArmModelsCollector("00000000-0000-0000-0000-000000000000", "2026-09-01", None)
    monkeypatch.setattr(collector, "_get", lambda *_: page)
    with pytest.raises(ValueError, match="invalid Models API page"):
        collector.models("eastus2")
