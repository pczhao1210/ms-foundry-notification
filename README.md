# ms-foundry-notification

每日自动追踪 Microsoft Foundry 模型的生命周期变化（Preview→GA、上线、下线/退役、替代模型、自动升级）与 **API 零售价格变化**，并通过 **REST API** 与 **MCP Server**（供 LLM / Agent 调用）提供。

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

## 运行与部署

- 数据源：Azure Resource Manager Models API（主）+ 官方 [Model retirement schedule](https://learn.microsoft.com/azure/foundry/openai/concepts/model-retirement-schedule) 文档（辅）+ [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)（价格；零售标价，不含 Anthropic 等 Marketplace 模型）
- 运行：Azure Functions Flex Consumption（Python），每日 08:00（Asia/Shanghai）
- 部署：在 Azure Cloud Shell 中一键运行 `deploy.sh`，或 `azd up`（两者共用 `infra/main.bicep`）

### 方式一：Azure Cloud Shell 一键部署（无需安装任何工具）

打开 [Azure Cloud Shell](https://shell.azure.com)（选择 **Bash**），执行：

```bash
curl -fsSL https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh | bash -s -- -e prod -l eastus2
```

脚本会从本仓库下载 `infra/` 与 `src/` 到临时目录后部署，结束后自动清理。常用变体（`bash -s --` 之后即脚本参数）：

```bash
URL=https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh
curl -fsSL $URL | bash -s -- -e prod --what-if            # 仅预览基础设施变更
curl -fsSL $URL | bash -s -- -e prod --skip-infra -y      # 之后仅更新代码
curl -fsSL $URL | bash -s -- -e prod -r <tag-or-commit>   # 部署指定版本（默认 main）
curl -fsSL $URL | bash -s -- -h                           # 全部选项
```

也可以先克隆再运行（在仓库目录中运行且未指定 `-r` 时使用本地文件，便于部署本地修改）：

```bash
git clone https://github.com/pczhao1210/ms-foundry-notification.git
cd ms-foundry-notification
bash deploy.sh -e prod -l eastus2
```

脚本只依赖 `az`、`python3`、`curl`（Cloud Shell 均已内置）：获取源码 → 订阅级 Bicep 部署 → 打包 `src/` → `az functionapp deployment source config-zip --build-remote true`（Flex Consumption 远程构建）→ 校验无 key 访问返回 401 → 输出端点与取 key 命令（不打印密钥值）。可重复执行（幂等）。通过管道运行时仍会从终端读取部署确认；无终端（如 CI）时请加 `-y`。

> `raw.githubusercontent.com` 有约 5 分钟缓存，刚推送的更改可能稍后才生效；需要精确版本时用 `-r <commit>`。

所需权限：订阅级 **Owner**，或 Contributor + User Access Administrator（需为托管身份分配订阅级 Reader）。

### 方式二：azd

```bash
azd auth login
azd up        # 提示输入环境名 / 订阅 / 区域
```

首次采集在下一个 UTC 00:00（北京时间 08:00）运行；在此之前 `/api/models`、`/api/prices` 返回 503（尚无快照），变化接口返回空结果。首次运行会用文档 Git 历史回填最近 30 天的文档变化；ARM 与价格变化从第二天起产生。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -r src/requirements.txt -r requirements-dev.txt
python -m pytest -q          # 离线单元测试，不访问网络/Azure
```

本地运行 Functions 需要 Node.js、`az login`（采集 ARM 用 Azure CLI 凭据）与 `src/local.settings.json`（已被忽略，勿提交）。存储默认使用本地 Azurite 模拟器：

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "STORAGE_USE_EMULATOR": "true",
    "AZURE_TOKEN_CREDENTIALS": "AzureCliCredential",
    "FOUNDRY_REGIONS": "eastus2,swedencentral"
  }
}
```

```bash
npm install -g azurite azure-functions-core-tools@4
azurite --silent --skipApiVersionCheck --location /tmp/azurite &      # blob 10000 / queue 10001 / table 10002
cd src && source ../.venv/bin/activate
export FOUNDRY_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"  # 不写入文件
func start
# 另开终端：手动触发一次每日采集，然后调用接口
curl -X POST -H "Content-Type: application/json" -d '{"input":""}' http://localhost:7071/admin/functions/daily_collect
curl "http://localhost:7071/api/changes/past?days=7"
```

- `STORAGE_USE_EMULATOR=true` 时使用 Azurite 公开的开发账号，并自动创建容器与表；改为连接 Azure 存储时去掉该项，设置 `STORAGE_BLOB_ENDPOINT` / `STORAGE_TABLE_ENDPOINT`（取自 `azd env get-values`，本地账号需有 Blob/Table Data Contributor，azd 会按 `AZURE_PRINCIPAL_ID` 自动授予）
- `AZURE_TOKEN_CREDENTIALS=AzureCliCredential` 固定使用 `az login` 凭据（避免在 Azure VM 上误用 VM 托管身份）；`FOUNDRY_REGIONS` 限定 region 以加快调试
- 系统缺少 libicu 时（Core Tools 报 `Couldn't find a valid ICU package`）先 `export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`
- 本地 `func start` 不校验 Function Key；修改 `function_app.py` 会触发 host 重启并中断正在运行的采集，修改 `core/` 等其他模块需手动重启 host

> 状态：代码与 IaC 已完成并离线验证，尚未在真实环境部署验证。详见 [docs/plan.md](docs/plan.md)，开发约定见 [AGENTS.md](AGENTS.md)。

## License

[MIT](LICENSE)
