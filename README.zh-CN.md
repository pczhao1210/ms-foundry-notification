# ms-foundry-notification

[English](README.md) | **简体中文**

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

## 运行与部署

- 数据源：Azure Resource Manager Models API（主）+ 官方 [Model retirement schedule](https://learn.microsoft.com/azure/foundry/openai/concepts/model-retirement-schedule) 文档（辅）+ [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)（价格；零售标价，不含 Anthropic 等 Marketplace 模型）
- 运行：Azure Functions Flex Consumption（Python），每日 08:00（Asia/Shanghai）
- 部署：在 Azure Cloud Shell 中一键运行 `deploy.sh`，或 `azd up`（两者共用 `infra/main.bicep`）；脚本帮助、向导、日志、错误与完成摘要均为中英双语

### 方式一：Azure Cloud Shell 一键部署（无需安装任何工具）

打开 [Azure Cloud Shell](https://shell.azure.com)（选择 **Bash**），执行：

```bash
curl -fsSL https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh | bash
```

默认先选择订阅，再进入四步部署向导，每步直接回车保留默认：
如果有多个可用（Enabled）订阅，先显示订阅名称和 ID，可输入编号、名称或 ID；回车保留当前订阅。只有一个可用订阅时自动使用。`-s/--subscription` 或 `AZURE_SUBSCRIPTION_ID` 已指定订阅时跳过此步骤；重名订阅请用编号或 ID 区分。

1. 选择 region：显示支持 Flex Consumption 的区域，可输入编号或名称，默认 `eastus2`。
2. 选择资源组：显示当前订阅已有组，可输入编号、已有组名或新组名，默认 `rg-foundry-notify`。已有组保留其所在地，资源部署到所选 region。
3. 输入资源名称前缀：默认 `foundry-notify`。
4. 确认 UTC 触发时间：输入六字段 NCRONTAB，默认 `0 0 0 * * *`（北京时间每天 08:00）；最终确认后才开始部署。

`-e <env>` 改变环境名、默认资源组和向导默认前缀；未指定时环境名为 `foundry-notify`。`-l`、`-g/--resource-group`、`--resource-prefix`、`--schedule` 可预设各步默认值，环境变量分别为 `AZURE_LOCATION`、`AZURE_RESOURCE_GROUP`、`AZURE_RESOURCE_NAME_PREFIX`、`DAILY_COLLECT_SCHEDULE`。

脚本会从本仓库下载 `infra/` 与 `src/` 到临时目录后部署，结束后自动清理。常用变体（`bash -s --` 之后即脚本参数）：

```bash
URL=https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh
curl -fsSL $URL | bash -s -- -e prod --what-if            # 仅预览基础设施变更
curl -fsSL $URL | bash -s -- -e prod -g rg-prod --resource-prefix prod -y  # 非交互部署
curl -fsSL $URL | bash -s -- -e prod --skip-infra -y      # 之后仅更新代码
curl -fsSL $URL | bash -s -- -e prod -r <tag-or-commit>   # 部署指定版本（默认 main）
curl -fsSL $URL | bash -s -- -h                           # 全部选项
```

也可以先克隆再运行（在仓库目录中运行且未指定 `-r` 时使用本地文件，便于部署本地修改）：

```bash
git clone https://github.com/pczhao1210/ms-foundry-notification.git
cd ms-foundry-notification
bash deploy.sh
```

资源名称保留唯一性后缀，例如 `func-<prefix>-<token>`；存储账户去掉前缀中的 `-`，只使用前 9 位，再加完整唯一性后缀以满足 24 位限制。后续基础设施部署须沿用相同环境名、区域、资源组与前缀；更改资源组或前缀会创建新资源，不会迁移数据或删除旧资源。`-y`、`--what-if` 跳过订阅选择及部署向导，使用显式指定的订阅或当前订阅；非交互模式未指定前缀时保留旧版哈希命名。预览向导配置时需显式传入 `-g`、`--resource-prefix` 和自定义 `--schedule`。`--skip-infra` 仍会按需选择订阅，但跳过区域、资源组、前缀、触发时间四步，读取所选订阅内同名环境的部署输出更新代码。

脚本只依赖 `az`、`python3`、`curl`（Cloud Shell 均已内置）：选择部署选项 → 获取源码 → 订阅级 Bicep 部署 → 打包 `src/` → `az functionapp deployment source config-zip --build-remote true`（Flex Consumption 远程构建）→ 确认完整 13 个函数注册，REST/MCP 无 key 请求均返回 401 → 输出端点与取 key 命令（不打印密钥值）。验收失败会非零退出；仍需在本人终端使用调用方密钥验证实际 REST/MCP 查询。`--what-if` 不注册资源提供程序或部署资源。相同配置可重复执行（幂等）。通过管道运行时向导与部署确认均从 `/dev/tty` 读取；无终端（如 CI）时请加 `-y`。

正式部署与 `--what-if` 均保留默认 Provider 检查。部署输出通过 `az deployment sub show` 按 JSON 读取，只有状态为 `Succeeded` 且 `AZURE_RESOURCE_GROUP` / `AZURE_FUNCTION_APP_NAME` 都是有效名称时才查询 Function App、发布代码。输出键按不区分大小写的方式唯一匹配，兼容实际 ARM 返回的 `azurE_RESOURCE_GROUP` / `azurE_FUNCTION_APP_NAME`，不改写资源名称值；若出现多个大小写不同的同名键则报错停止。日志区分部署、读取输出及目标资源；缺失或无效输出时只报告字段名及类型，不打印字段值，不会将 TSV 的 `None` 当作资源名称。向导回车保留显示的默认值。若基础设施已经成功，仅在输出读取阶段失败，可使用更新后的脚本加 `--skip-infra` 在相同订阅和环境下继续，不必重新部署基础设施。

Function App 主机名同样通过 `az functionapp show -o json` 读取，兼容 CLI 扁平响应中的顶层字段，以及 ARM 原始响应的 `properties.defaultHostName`。只在顶层与 `properties` 中唯一匹配 `defaultHostName` / `defaultHostname` 等大小写变体，并验证为有效 DNS 名称后才发布代码；不递归搜索其他对象。主机名缺失、空值、跨层重复、大小写冲突或读取失败都会停止，缺失诊断分别列出顶层及 `properties` 的字段名，不打印字段值。不再拼出 `https:///api/...`，也不会根据应用名猜测域名。旧脚本若报告 `Could not resolve host: api`，或实际字段含 `properties` 却报缺少 `defaultHostName`，应先检查响应结构；基础设施部署已成功时，使用修复后的脚本加 `--skip-infra` 可继续发布代码并验收，无需重建基础设施。

`RoleAssignmentUpdateNotPermitted` 表示部署尝试修改现有角色分配的主体、作用域等不可变字段。旧模板的订阅级 Reader 分配 ID 未包含 `principalId`，同名托管身份删除重建后会复用旧 ID。现在改为按订阅、实际 `principalId` 和 Reader 角色生成 ID：同一身份重跑保持不变，新身份使用新分配；不会自动删除旧分配。可在所选订阅中查看具体失败资源，确认是否为该分配：

```bash
az deployment operation sub list --name foundry-notify-foundry-notify \
  --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.id,error:properties.statusMessage}" -o json
```

从旧模板迁移时，如果**当前同一身份**已经拥有订阅级 Reader，新命名可能报 `RoleAssignmentExists`。确认现有分配的 `principalId`、订阅作用域和 Reader 角色均一致后，可设置 `AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME` 为该分配的 **name（GUID，不是完整资源 ID）**；azd 用户在 `infra/main.parameters.json` 的 `subscriptionReaderAssignmentName.value` 填入同一 GUID。新建环境或身份已重建时保留空值，不得填入属于旧身份的冲突分配 ID。后续部署需保留所选的复用参数，不要批量删除订阅角色分配。

若基础设施失败已将同名部署记录置为 `Failed`，需用修复后的模板、相同订阅/环境/区域/资源组/前缀重新完成基础设施，再发布代码；此时不能直接使用要求部署状态为 `Succeeded` 的 `--skip-infra`。

> `raw.githubusercontent.com` 有约 5 分钟缓存，刚推送的更改可能稍后才生效；需要精确版本时用 `-r <commit>`。

所需权限：订阅级 **Owner**，或 Contributor + User Access Administrator（需为托管身份分配订阅级 Reader）。

### 方式二：azd

```bash
azd auth login
azd up        # 提示输入环境名 / 订阅 / 区域
```

首次采集在下一个 UTC 00:00（北京时间 08:00）运行；在此之前 `/api/models`、`/api/prices` 返回 503（尚无快照），变化接口返回空结果且 `collection.complete=false`。首次运行会用文档 Git 历史回填最近 30 天的文档变化；ARM 与价格变化从第二天起产生。

采集结果先保存为待提交批次，事件及计划写入成功后才发布快照。写入中断时，下次运行会先按原日期恢复批次；同一来源的采集不能并行手工触发。ARM 区域失败会保留该区域最近成功的数据并使任务报告降级，其他来源仍继续处理；空目录不会被当作全部模型下线。部分区域缺失但有效数据已保存时，报告记入 `degraded` 并输出 WARNING，门户调用标为成功，REST/MCP 仍返回 `collection.complete=false`；采集、提交或状态写入真正失败时才记入 `failed` 并使调用失败。因此文件已生成不一定代表所有步骤成功，可在 Application Insights 中检查 `daily run report` 和异常。

最新快照使用轻量索引读取，旧存储布局会在下次成功采集后自动建立索引。各来源报告保存在 snapshots 容器的 `status/{kind}.json.gz` 与 `runs/YYYY-MM-DD/{kind}.json.gz`；日报保留当天最后一次尝试。

### 采集触发时间（UTC）

直接运行 `bash deploy.sh` 即可在资源前缀之后确认 UTC 触发时间，无需加 `--schedule`；回车保留 `0 0 0 * * *`，即每天 UTC 00:00 / 北京时间 08:00。输入格式为六字段 NCRONTAB（秒、分、时、日、月、星期），交互输入不加引号。也可通过 `--schedule` 预设此步骤的默认值，命令行必须用引号包住。例如每天 UTC 01:30 / 北京时间 09:30：

```bash
bash deploy.sh -e <env> --schedule '0 30 1 * * *'
```

一键部署同样支持：在 `bash -s --` 后加 `--schedule '0 30 1 * * *'`。也可设置环境变量 `DAILY_COLLECT_SCHEDULE`，命令行参数优先。脚本检查六字段格式，具体表达式的有效取值由 Azure Functions 校验。azd 用户修改 `infra/main.parameters.json` 的 `dailyCollectSchedule.value` 后部署，默认值相同。

时间写入 Function App 应用设置 `DAILY_COLLECT_SCHEDULE`，Timer 使用 `%DAILY_COLLECT_SCHEDULE%` 引用。已有环境首次升级需同时部署基础设施和代码，不能仅用 `--skip-infra`。以后仅改时间可用 `--skip-code --schedule '...'`，或在门户「设置 → 环境变量 → 应用设置」修改该值并应用；修改应用设置默认会重启应用。显式指定时间不能与 `--skip-infra` 同用；未指定时间的基础设施部署会恢复默认值，后续部署需传入希望保留的时间。

Flex Consumption 不支持 `WEBSITE_TIME_ZONE` / `TZ`，不要添加这些设置；表达式始终按 UTC 定义。事件查询的日界仍按 Asia/Shanghai 计算。采集健康告警仍以 32 小时未完成为阈值，若设置更低的采集频率，需同步调整告警设计。

### 手动运行与门户排查

在 Azure 门户打开函数应用 → 函数 → `daily_collect` → 代码 + 测试 → 测试/运行，密钥下拉框选择 `_master`，请求体可填 `{"input":""}`，然后运行一次。此操作使用 `/admin/functions/daily_collect` 管理端点，普通 host key 与 `mcp_extension` 不适用；只在门户选择密钥，不复制、分发或记录密钥值。HTTP 202 仅表示请求已接受，最终结果在监视/调用记录中查看，不要并发触发。

门户出现 `HTTP 0` / `Failed to fetch` 表示浏览器没有拿到可读取的 HTTP 响应，不能仅凭它断定采集失败或密钥错误。先查看调用记录，避免请求已提交却再次触发；再检查浏览器开发者工具中的 CORS、DNS/TLS、代理或访问限制错误。模板仅允许 CORS 来源 `https://portal.azure.com`，不使用通配符且 `supportCredentials=false`；这不会取消 Function Key 认证。旧部署可在原订阅的 Cloud Shell 中检查并补充门户来源，无需重新发布代码：

```bash
az functionapp cors show -g <resource-group> -n <function-app>
az functionapp cors add -g <resource-group> -n <function-app> \
  --allowed-origins https://portal.azure.com -o none
```

保存后刷新门户再测试。若仍报错，只分享去除密钥和敏感请求头后的网络错误信息；不要为排查而关闭鉴权或添加 `*` 来源。

### 可选采集告警

首次采集成功后，可运行 `bash deploy.sh -e <env> --skip-code --enable-alerts` 启用采集健康告警；azd 用户在 `infra/main.parameters.json` 将 `enableCollectionAlerts.value` 改为 true 后重新 provision。规则每小时检查最近采集是否失败、是否超过 32 小时没有完成调用。默认不创建规则，启用可能产生 Azure Monitor 费用。

此规则不对成功执行但部分区域缺失的降级调用告警；数据完整性以响应中的 `collection` 为准，缺失区域也会记录在 `daily arm step degraded` WARNING 日志中。

通知接收方需在 Azure 门户绑定 Action Group，或通过 Bicep 参数 `alertActionGroupIds` 指定已有组；未配置时只有告警记录，不发送通知。真实遥测与告警投递仍需云端验证。

## 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -r src/requirements.txt -r requirements-dev.txt
python -m pytest -q          # 离线单元测试，不访问网络/Azure
```

运行时、传递依赖及测试依赖均固定为已验证的 Linux/Python 3.12 版本。升级依赖时应重新解析完整依赖集合并运行测试。GitHub Actions 在 push/PR 时自动执行依赖一致性检查、离线测试、部署脚本语法检查和 Bicep 编译；不自动部署、不需要 Azure 密钥。

本地运行 Functions 需要 Node.js、`az login`（采集 ARM 用 Azure CLI 凭据）与 `src/local.settings.json`（已被忽略，勿提交）。存储默认使用本地 Azurite 模拟器：

```json
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "DAILY_COLLECT_SCHEDULE": "0 0 0 * * *",
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