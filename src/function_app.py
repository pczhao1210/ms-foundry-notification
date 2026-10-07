"""Azure Functions entry point: daily collection timer, REST endpoints and MCP tools."""
from __future__ import annotations

import logging

import azure.functions as func

import pipeline
from api.http_routes import bp as http_routes
from api.mcp_tools import bp as mcp_tools

# Azure SDK logs every storage/ARM request and response at INFO.
logging.getLogger("azure").setLevel(logging.WARNING)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
app.register_functions(http_routes)
app.register_functions(mcp_tools)


# 00:00 UTC = 08:00 Asia/Shanghai (Flex Consumption has no WEBSITE_TIME_ZONE).
@app.timer_trigger(arg_name="timer", schedule="0 0 0 * * *", run_on_startup=False, use_monitor=True)
def daily_collect(timer: func.TimerRequest) -> None:
    store, sources = pipeline.live()
    report = pipeline.run_daily(store, sources)
    if report["failed"]:
        raise RuntimeError(f"daily collection steps failed: {', '.join(report['failed'])}")
