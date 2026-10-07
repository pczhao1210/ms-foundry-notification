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
    assert calls[-1] == ("changes", ("past",), {"days": 14, "category": "price", "billing": None})

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
    assert calls[-1] == ("changes", ("upcoming",), {"days": 3, "billing": "token"})

    mcp_tools.mcp_get_model_prices(json.dumps({"arguments": {"model": "gpt-5.4", "deployment": ""}}))
    assert calls[-1] == ("prices", (), {"model": "gpt-5.4", "region": None, "deployment": None})

    mcp_tools.mcp_get_today_changes(json.dumps({}))
    assert calls[-1] == ("changes", ("today",), {"category": None, "billing": None})


@pytest.mark.parametrize("days", ["two", 1.5])
def test_mcp_tools_return_errors_as_json(calls, days):
    result = json.loads(mcp_tools.mcp_get_past_changes(json.dumps({"arguments": {"days": days}})))
    assert result == {"error": "days must be an integer"}
