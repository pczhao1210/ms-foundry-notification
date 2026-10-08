"""REST endpoints (function-key protected) — thin adapters over `api.service`."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import azure.functions as func

from api import service
from core.query import DEFAULT_DAYS, DEFAULT_LIMIT, DEFAULT_RESPONSE_BYTES

bp = func.Blueprint()
log = logging.getLogger(__name__)
_AUTH = func.AuthLevel.FUNCTION


def _json(body: Any, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(body, ensure_ascii=False), status_code=status, mimetype="application/json")


def _handle(call: Callable[[], Any]) -> func.HttpResponse:
    try:
        return _json(call())
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except service.NotReady as exc:
        return _json({"error": str(exc)}, 503)
    except Exception:  # noqa: BLE001 - details go to logs, not to callers
        log.exception("request failed")
        return _json({"error": "internal error"}, 500)


def _param(req: func.HttpRequest, name: str) -> str | None:
    value = req.params.get(name)
    return value.strip() if value and value.strip() else None


def _days(req: func.HttpRequest) -> int:
    return _integer(req, "days", DEFAULT_DAYS)


def _integer(req: func.HttpRequest, name: str, default: int) -> int:
    value = _param(req, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _page(req: func.HttpRequest) -> dict[str, Any]:
    return {"limit": _integer(req, "limit", DEFAULT_LIMIT), "cursor": _param(req, "cursor"),
            "max_bytes": _integer(req, "max_bytes", DEFAULT_RESPONSE_BYTES)}


@bp.route(route="changes/today", methods=["GET"], auth_level=_AUTH)
def rest_changes_today(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(
        lambda: service.changes("today", category=_param(req, "category"), billing=_param(req, "billing"), **_page(req))
    )


@bp.route(route="changes/upcoming", methods=["GET"], auth_level=_AUTH)
def rest_changes_upcoming(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(lambda: service.changes("upcoming", days=_days(req), billing=_param(req, "billing"), **_page(req)))


@bp.route(route="changes/past", methods=["GET"], auth_level=_AUTH)
def rest_changes_past(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(
        lambda: service.changes(
            "past", days=_days(req), category=_param(req, "category"), billing=_param(req, "billing"), **_page(req)
        )
    )


@bp.route(route="models", methods=["GET"], auth_level=_AUTH)
def rest_models(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(
        lambda: service.models(
            **{
                name: _param(req, name)
                for name in ("vendor", "status", "region", "billing", "name_contains")
            }
        )
    )


@bp.route(route="models/{format}/{name}", methods=["GET"], auth_level=_AUTH)
def rest_model(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(
        lambda: service.model(
            name=req.route_params["name"], vendor=req.route_params["format"], version=_param(req, "version")
        )
    )


@bp.route(route="prices", methods=["GET"], auth_level=_AUTH)
def rest_prices(req: func.HttpRequest) -> func.HttpResponse:
    return _handle(
        lambda: service.prices(
            model=_param(req, "model") or "", region=_param(req, "region"), deployment=_param(req, "deployment")
        )
    )
