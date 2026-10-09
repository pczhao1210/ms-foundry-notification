# API and MCP

**English** | [简体中文](api.zh-CN.md) | [Project Overview](../README.md)

## REST API

All endpoints require a Function Key in the `x-functions-key` header.

| Endpoint | Description |
|---|---|
| `GET /api/changes/today?category=lifecycle\|price&billing=token\|provisioned` | Today's changes |
| `GET /api/changes/upcoming?days=7&billing=` | Scheduled changes in the next N days (N <= 30) |
| `GET /api/changes/past?days=7&category=&billing=` | Changes in the past N days (N <= 30) |
| `GET /api/models?vendor=&status=&region=&billing=&name_contains=` | Current model catalog |
| `GET /api/models/{format}/{name}?version=` | Details for a single model |
| `GET /api/prices?model=&region=&deployment=` | Current retail prices (USD / 1M tokens) |

All three change endpoints and their corresponding MCP tools support `limit` (1-1000, default 200) and `cursor`. Pass the response's `next_cursor` unchanged into the next request, keeping the same filters, until `next_cursor=null`. `total_events` / `summary` describe the entire window. If a data update or date rollover invalidates the cursor, restart without a cursor.

Change responses default to a 64 KiB UTF-8 JSON budget, configurable with `max_bytes` (16384-1048576; excludes the MCP protocol envelope). Large price event details are split into fragments: merge `changes` by event ID and `changes_offset` until you reach `changes_total`. Do not deduplicate by event ID alone. `byte_limited=true` indicates that the page reached its byte budget. If an indivisible item still exceeds the budget, the request returns an error asking you to increase it.

Every successful query response includes `collection`. `complete=false` means a relevant source is not yet collected, failed, degraded, or stale; it must not be interpreted as "no changes." `sources` includes `last_attempt_at`, `last_success_at`, `snapshot_at`, and failed or stale regions. Collection status is cached for up to 15 seconds and catalog snapshots for up to 10 minutes; a catalog served from an outdated cache is marked stale. This field describes current collection health, not the completeness of historical backfills.

When the pricing API publishes prices with future effective dates, the change endpoints expose corresponding scheduled events. The current-price endpoint does not return those prices before they take effect. Future-price coverage depends on upstream publication and is not guaranteed.

```bash
curl -H "x-functions-key: $FOUNDRY_API_KEY" "https://<app-host>/api/changes/upcoming?days=7"
```

## MCP Server

- Endpoint: `https://<app-host>/runtime/webhooks/mcp` (Streamable HTTP)
- Authentication: header `x-functions-key: <mcp_extension system key>`
- Tools: `get_today_changes`, `get_upcoming_changes`, `get_past_changes`, `search_models`, `get_model`, `get_model_prices`

VS Code / GitHub Copilot (`.vscode/mcp.json`; enter the key at runtime rather than storing it on disk):

```json
{
  "inputs": [
    { "type": "promptString", "id": "foundry-host", "description": "Function App host name, e.g. xxx.azurewebsites.net" },
    { "type": "promptString", "id": "foundry-mcp-key", "description": "mcp_extension system key", "password": true }
  ],
  "servers": {
    "foundry-models": {
      "type": "http",
      "url": "https://${input:foundry-host}/runtime/webhooks/mcp",
      "headers": { "x-functions-key": "${input:foundry-mcp-key}" }
    }
  }
}
```

## Get Keys

```bash
# REST: create a separately named key for each client (individually revocable)
az functionapp keys set -g <rg> -n <app> --key-type functionKeys --key-name <client>
# MCP
az functionapp keys list -g <rg> -n <app> --query systemKeys.mcp_extension -o tsv
```
