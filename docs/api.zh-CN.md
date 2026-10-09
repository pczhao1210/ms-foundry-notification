# API 与 MCP

[English](api.md) | **简体中文** | [项目简介](../README.zh-CN.md)

## REST API

所有端点需要 Function Key（header `x-functions-key`）。

| Endpoint | 说明 |
|---|---|
| `GET /api/changes/today?category=lifecycle\|price&billing=token\|provisioned` | 今天的变化 |
| `GET /api/changes/upcoming?days=7&billing=` | 未来 N 天的计划变化（N ≤ 30） |
| `GET /api/changes/past?days=7&category=&billing=` | 过去 N 天的变化（N ≤ 30） |
| `GET /api/models?vendor=&status=&region=&billing=&name_contains=` | 当前模型目录 |
| `GET /api/models/{format}/{name}?version=` | 单个模型详情 |
| `GET /api/prices?model=&region=&deployment=` | 当前零售价（USD / 1M tokens） |

三个变化接口及对应 MCP 工具均支持 `limit`（1–1000，默认 200）和 `cursor`。将响应的 `next_cursor` 原样传入下一次请求，保持相同过滤条件，直到 `next_cursor=null`。`total_events` / `summary` 是整个窗口的统计；若数据更新或跨日导致游标失效，需不带 cursor 重新开始。

变化响应默认限制为 64 KiB UTF-8 JSON 内容，可用 `max_bytes` 调整（16384–1048576；不包含 MCP 协议外层封装）。价格事件明细过大时会分片，按事件 ID 和 `changes_offset` 合并 `changes`，直到达到 `changes_total`；不可仅按事件 ID 去重。`byte_limited=true` 表示本页触及字节预算。不可拆分的单条数据仍过大时会返回错误，要求增加预算。

所有查询成功响应均有 `collection`。`complete=false` 表示相关来源尚未完成、失败、降级或数据陈旧，不能解释为“没有变化”；`sources` 提供 `last_attempt_at`、`last_success_at`、`snapshot_at`、失败及陈旧区域。采集状态最多缓存 15 秒，目录快照最多缓存 10 分钟；目录仍返回旧缓存时会标为 stale。该字段说明当前采集情况，不保证历史回填完整。

价格 API 返回未来生效价格时，变化接口会提供对应的 scheduled 事件；当前价格接口不会提前返回未来价。未来价格是否可用取决于上游是否公布，不能保证覆盖。

```bash
curl -H "x-functions-key: $FOUNDRY_API_KEY" "https://<app-host>/api/changes/upcoming?days=7"
```

## MCP Server

- 端点：`https://<app-host>/runtime/webhooks/mcp`（Streamable HTTP）
- 认证：header `x-functions-key: <mcp_extension 系统密钥>`
- 工具：`get_today_changes`、`get_upcoming_changes`、`get_past_changes`、`search_models`、`get_model`、`get_model_prices`

VS Code / GitHub Copilot（`.vscode/mcp.json`，密钥运行时输入，不落盘）：

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

## 获取密钥

```bash
# REST：为每个调用方创建独立命名 key（可单独吊销）
az functionapp keys set -g <rg> -n <app> --key-type functionKeys --key-name <client>
# MCP
az functionapp keys list -g <rg> -n <app> --query systemKeys.mcp_extension -o tsv
```
