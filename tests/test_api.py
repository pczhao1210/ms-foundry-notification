import json
from pathlib import Path

import azure.functions as func
import pytest

from api import http_routes, mcp_tools, service

SRC = Path(__file__).parent.parent / "src"
TOOLS = {
    "get_today_changes",
    "get_upcoming_changes",
    "get_past_changes",
    "search_models",
    "get_model",
    "get_model_prices",
}


def test_function_app_bindings():
    from function_app import app

    bindings = [json.loads(f.get_function_json())["bindings"] for f in app.get_functions()]
    triggers = [b[0] for b in bindings]
    assert {t["toolName"] for t in triggers if t["type"] == "mcpToolTrigger"} == TOOLS
    http = [t for t in triggers if t["type"] == "httpTrigger"]
    assert len(http) == 6
    assert all(t["authLevel"] == "FUNCTION" for t in http)
    for trigger in triggers:
        if trigger["type"] == "mcpToolTrigger":
            for prop in json.loads(trigger["toolProperties"]):
                assert set(prop) == {"propertyName", "propertyType", "description", "isRequired"}
    [timer] = [t for t in triggers if t["type"] == "timerTrigger"]
    assert timer["schedule"] == "0 0 0 * * *"


def test_host_keeps_mcp_webhook_behind_system_key():
    host = json.loads((SRC / "host.json").read_text(encoding="utf-8"))
    assert host["extensions"]["mcp"]["system"]["webhookAuthorizationLevel"] == "System"
    assert "anonymous" not in json.dumps(host).casefold()


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    def fake(name):
        def call(*args, **kwargs):
            recorded.append((name, args, kwargs))
            if kwargs.get("billing") == "free":
                raise ValueError("billing must be one of token, provisioned")
            return {"ok": name}

        return call

    for name in ("changes", "models", "model", "prices"):
        monkeypatch.setattr(service, name, fake(name))
    return recorded


def _request(url, params=None, route_params=None):
    return func.HttpRequest("GET", url, body=b"", params=params or {}, route_params=route_params or {})


def test_rest_passes_query_parameters(calls):
    response = http_routes.rest_changes_past(_request("/api/changes/past", {"days": "14", "category": "price"}))
    assert response.status_code == 200
    assert json.loads(response.get_body()) == {"ok": "changes"}
    assert calls[-1] == ("changes", ("past",), {"days": 14, "category": "price", "billing": None,
                                              "limit": 200, "cursor": None, "max_bytes": 65536})

    http_routes.rest_model(_request("/api/models/OpenAI/gpt-4o", route_params={"format": "OpenAI", "name": "gpt-4o"}))
    assert calls[-1] == ("model", (), {"name": "gpt-4o", "vendor": "OpenAI", "version": None})


@pytest.mark.parametrize(
    "params, status",
    [({"days": "x"}, 400), ({"billing": "free"}, 400)],
)
def test_rest_rejects_bad_parameters(calls, params, status):
    response = http_routes.rest_changes_upcoming(_request("/api/changes/upcoming", params))
    assert response.status_code == status
    assert "error" in json.loads(response.get_body())


def test_rest_maps_missing_data_and_unexpected_errors(monkeypatch):
    def not_ready(**_):
        raise service.NotReady("no prices snapshot has been collected yet")

    def boom(**_):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(service, "prices", not_ready)
    assert http_routes.rest_prices(_request("/api/prices", {"model": "gpt-5.4"})).status_code == 503
    monkeypatch.setattr(service, "models", boom)
    response = http_routes.rest_models(_request("/api/models"))
    assert response.status_code == 500
    assert b"secret" not in response.get_body()


def test_mcp_tools_parse_arguments(calls):
    context = json.dumps({"arguments": {"days": "3", "billing": " token "}})
    assert json.loads(mcp_tools.mcp_get_upcoming_changes(context)) == {"ok": "changes"}
    assert calls[-1] == ("changes", ("upcoming",), {"days": 3, "billing": "token", "limit": 200, "cursor": None,
                                                  "max_bytes": 65536})

    mcp_tools.mcp_get_model_prices(json.dumps({"arguments": {"model": "gpt-5.4", "deployment": ""}}))
    assert calls[-1] == ("prices", (), {"model": "gpt-5.4", "region": None, "deployment": None})

    mcp_tools.mcp_get_today_changes(json.dumps({}))
    assert calls[-1] == ("changes", ("today",), {"category": None, "billing": None, "limit": 200, "cursor": None,
                                               "max_bytes": 65536})


@pytest.mark.parametrize("days", ["two", 1.5])
def test_mcp_tools_return_errors_as_json(calls, days):
    result = json.loads(mcp_tools.mcp_get_past_changes(json.dumps({"arguments": {"days": days}})))
    assert result == {"error": "days must be an integer"}


def test_rest_and_mcp_pass_pagination(calls):
    params = {"limit": "17", "cursor": "opaque-token", "max_bytes": "32768"}
    http_routes.rest_changes_today(_request("/api/changes/today", params))
    rest_call = calls[-1]
    mcp_tools.mcp_get_today_changes(json.dumps({"arguments": params}))
    assert calls[-1] == rest_call
    assert rest_call[2]["limit"] == 17
    assert rest_call[2]["cursor"] == "opaque-token"
    assert rest_call[2]["max_bytes"] == 32768


def test_pagination_validation(calls):
    response = http_routes.rest_changes_today(_request("/api/changes/today", {"limit": "1.5"}))
    assert response.status_code == 400
    result = json.loads(mcp_tools.mcp_get_today_changes(json.dumps({"arguments": {"limit": 1.5}})))
    assert result == {"error": "limit must be an integer"}


def test_changes_exposes_incomplete_collection_and_caches_status(monkeypatch):
    from unittest.mock import Mock

    store = Mock()
    store.read_events.return_value = []
    store.source_status.return_value = {"status": "failed", "last_success_at": "2026-10-06T00:00:00Z"}
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setattr(service, "_statuses", {})
    monkeypatch.setattr(service.ev, "local_today", lambda: "2026-10-07")
    rest = http_routes.rest_changes_today(_request("/api/changes/today", {"category": "price"}))
    mcp = mcp_tools.mcp_get_today_changes(json.dumps({"arguments": {"category": "price"}}))
    result = json.loads(rest.get_body())
    assert result == json.loads(mcp)
    assert result["total_events"] == 0 and result["collection"]["complete"] is False
    assert result["collection"]["sources"]["prices"]["last_success_at"] == "2026-10-06T00:00:00Z"
    store.source_status.assert_called_once_with("prices")


def test_large_rest_and_mcp_responses_respect_the_same_byte_budget(monkeypatch):
    from unittest.mock import Mock

    event = {"id": "large", "date": "2026-10-07", "kind": "observed", "category": "price",
             "source": "retail_prices", "type": "PRICE_CHANGED", "billing": ["token"],
             "price_model": {"product": "Azure OpenAI GPT5", "name": "5.4"},
             "changes": [{"sku": str(index), "note": "\u6a21\u578b" * 100} for index in range(100)]}
    store = Mock()
    store.read_events.return_value = [event]
    store.source_status.return_value = None
    monkeypatch.setattr(service, "_store", store)
    monkeypatch.setattr(service, "_statuses", {})
    monkeypatch.setattr(service.ev, "local_today", lambda: "2026-10-07")
    params = {"category": "price", "max_bytes": "16384"}
    rest = http_routes.rest_changes_today(_request("/api/changes/today", params))
    mcp = mcp_tools.mcp_get_today_changes(json.dumps({"arguments": params}))
    assert rest.status_code == 200
    assert len(rest.get_body()) <= 16384 and len(mcp.encode("utf-8")) <= 16384
    result = json.loads(rest.get_body())
    assert result == json.loads(mcp)
    assert result["byte_limited"] is True and result["next_cursor"] is not None
