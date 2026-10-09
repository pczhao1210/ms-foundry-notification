# AGENTS.md

面向 AI 编码助手（Copilot / Codex / Claude 等）与贡献者的项目约定。完整设计见 [docs/plan.md](docs/plan.md)，**修改设计时同步更新 plan.md**。

## 项目目标
每日拉取 Microsoft Foundry 模型目录，生成并通过 **REST API 与 MCP Server（供 LLM 调用）** 提供三类变化：
1. 今天的变化（Preview→GA、新上线、下线/退役、退役日期变更、**API 价格变化**等）
2. 未来 7 天的计划变化（退役、SKU 下线、自动升级开始）
3. 过去 7 天的变化

## 技术栈
- Python 3.12（Azure Functions Python v2 编程模型，`function_app.py` 装饰器风格）
- Azure Functions **Flex Consumption**（Linux）：Timer 触发采集 + HTTP 触发 REST 查询 + **MCP tool trigger**（Functions MCP 扩展，Streamable HTTP `/runtime/webhooks/mcp`）
- 存储：Blob（每日快照）+ Table（events / schedule）；**identity-based 访问，禁用共享密钥**（本地可用 `STORAGE_USE_EMULATOR=true` 连 Azurite，仅限模拟器）
- 认证：`azure-identity` 的 `DefaultAzureCredential`（本地用 `az login`，云端用 User-assigned Managed Identity）
- IaC：Bicep + azd（`azure.yaml`、`infra/`）
- 测试：pytest，基于 `tests/fixtures/` 的离线数据，**单元测试不得访问网络/Azure**

## 数据源（关键事实，已实测验证）
- 主数据源：ARM `GET /subscriptions/{sub}/providers/Microsoft.CognitiveServices/locations/{region}/models?api-version=2026-09-01`
  - 结果分页，**必须跟随 `nextLink`**
  - region 列表来自 provider `Microsoft.CognitiveServices` 的 `locations/models` resourceType；跳过 `global`（返回 400）
  - 只取 `kind ∈ {AIServices, MAI}`（`OpenAI`/`MaaS` 为重复视图），去重键 `(format, name, version)`
  - `lifecycleStatus` 取值：`Preview` / `GenerallyAvailable` / `Legacy` / `Deprecating` / `Deprecated`
  - API 术语 ≠ 文档术语：`Deprecating`=文档的 Deprecated（仅存量客户）；`Deprecated` 或 `deprecation.inference < today` = Retired（410）
  - 计划事件日期来源：`deprecation.inference/fineTune`、`skus[].deprecationDate`、`replacementConfig.autoUpgradeStartDate`
- 辅助数据源：`MicrosoftDocs/azure-ai-docs` 仓库 `articles/foundry/openai/includes/concepts-model-retirement-schedule-content.md`（文章 `concepts/model-retirement-schedule.md` 只 include 该文件；raw markdown + commit 历史，用于交叉校验与首次回填）
- 价格数据源：Retail Prices API `https://prices.azure.com/api/retail/prices?$filter=serviceName eq 'Foundry Models'`（公开免认证，跟随 `NextPageLink`，约 35k 行）
  - 只有当前价、无历史、无未来价 → 价格变化 = 每日快照 diff
  - 与 ARM 模型**无 meterId 关联**（`skus[].cost` 为空），`armSkuName` 不可信；按 `productName/skuName/meterName` 解析 + `core/price_alias.py` 别名表映射，映射失败标 `mapped=false` 但不丢弃
  - 计量名解析用 `core/price_parse.py` 的规则词典（不在运行时调用 LLM）；无法确定时返回未解析、退回单计量事件（`unparsed=true`），**不得猜测维度**；新增词典词须配 fixture 测试
  - 单位 `1K`/`1M` 混用，token 价统一换算为 USD / 1M tokens
  - Anthropic（Marketplace）不在该 API 中
  - 价格采集失败不得阻断生命周期事件采集；事件带 `category`（`lifecycle` / `price`）

## 范围
- 纳入所有可通过 API token 计费调用的模型（含 Anthropic/Meta/Cohere/Mistral/Fireworks 等，不按 `isMarketplaceRequired` 过滤）
- SKU 计费类型：`*ProvisionedManaged` → `provisioned`；其余（GlobalStandard、DataZoneStandard、Standard、*Batch、DeveloperTier）→ `token`
- 排除 HuggingFace / managed compute

## 认证与对外接口（见 plan.md §3.1–3.2）
- 认证方式：**Function Key**（部署账号无 Entra App Registration 权限，不使用 Easy Auth/OAuth）
- REST：`http_auth_level=func.AuthLevel.FUNCTION`，**不得新增 `ANONYMOUS` 端点**；调用方用 `x-functions-key` header，每个调用方一个命名 host key
- MCP：保持 `host.json` 中 `extensions.mcp.system.webhookAuthorizationLevel` 为默认 `System`，**不得设为 `Anonymous`**；不启用 SSE 传输
- 不得分发或在代码/文档/日志中输出 master key 及任何 key 值
- REST 与 MCP 只是薄适配层（`src/api/`），业务逻辑统一放 `core/query.py`；新增查询能力时**同时**暴露 REST 端点与 MCP 工具，保持返回结构一致
- MCP 工具只读；description 需写清参数、状态术语映射与时区约定；结果需有条数上限与 `truncated` 标记

## 约定
- 时间：存储与比较一律 UTC ISO 8601；“今天/过去/未来 N 天”按 `Asia/Shanghai` 日界计算；Timer 引用应用设置 `DAILY_COLLECT_SCHEDULE`，部署参数 `dailyCollectSchedule` / `deploy.sh --schedule`，默认 `0 0 0 * * *`（UTC 00:00 = 北京 08:00，Flex Consumption 不支持 `WEBSITE_TIME_ZONE` / `TZ`）
- 事件需带 `source`（`arm` / `docs` / `retail_prices`）与 `kind`（`observed` / `scheduled`），事件 ID 需确定性生成以保证重跑幂等
- 采集逻辑（I/O）与 normalize/diff/schedule（纯函数）分离，纯函数必须有单元测试
- 配置走环境变量（`core/config.py` 统一读取），不得硬编码订阅 ID、存储账户名
- **不得提交**：订阅/租户 ID、密钥、连接字符串、`local.settings.json`；新增 fixture 前将订阅 ID 替换为 `00000000-0000-0000-0000-000000000000`
- 只在需要解释的地方写注释

## 常用命令
```bash
# 安装依赖
python -m pip install -r src/requirements.txt -r requirements-dev.txt
# 单元测试
python -m pytest -q
# 本地运行 Functions（需 Azure Functions Core Tools v4）
cd src && func start
# 部署（二选一，共用 infra/main.bicep）
azd up
bash deploy.sh -e <env> -l eastus2          # Cloud Shell 友好，仅依赖 az + python3 + curl；--what-if / --skip-infra / --skip-code / -r <ref>
curl -fsSL https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh | bash -s -- -e <env>   # 一键部署（从 GitHub 取源码）
# 获取密钥（仅本人终端查看，勿提交/粘贴到 issue）
az functionapp keys set -g <rg> -n <app> --key-type functionKeys --key-name <client>   # REST 调用方
az functionapp keys list -g <rg> -n <app> --query systemKeys.mcp_extension -o tsv        # MCP
```
> 本机 Azure CLI 通过 `uv tool install azure-cli` 安装于 `~/.local/bin/az`。
> 修改 Bicep 参数/输出时须同时保证 azd 与 `deploy.sh` 可用：参数 `environmentName`、`location`；输出 `AZURE_RESOURCE_GROUP`、`AZURE_FUNCTION_APP_NAME`（`deploy.sh` 依赖）。
> `deploy.sh` 须保持 `curl | bash` 可用：主流程放在 `main()` 并于末行调用；交互输入读 `/dev/tty`；源码路径只能来自 `resolve_source`（本地 checkout 或 GitHub tarball），不得假设脚本所在目录。
> 仓库：https://github.com/pczhao1210/ms-foundry-notification（MIT）。
> 以上命令在对应文件（requirements、azure.yaml 等）创建后生效；目录结构见 docs/plan.md §4。
