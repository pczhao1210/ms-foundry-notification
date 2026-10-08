# MS Foundry 模型变更日报 — 项目规划

## 1. 可行性结论：可行 ✅

| 数据源 | 认证 | 提供信息 | 角色 |
|---|---|---|---|
| ARM Models API `GET /subscriptions/{sub}/providers/Microsoft.CognitiveServices/locations/{region}/models?api-version=2026-09-01` | Entra ID (Reader) | 每 region/SKU 的 `lifecycleStatus`(Preview/GenerallyAvailable/Legacy/Deprecating/Deprecated)、`replacementConfig`、`deprecation.inference/fineTune`、`skus[].deprecationDate`、`systemData.createdAt`、`format`(OpenAI/Microsoft/DeepSeek/xAI/...) | **主数据源**（官方文档明确："transitions visible in real time via the Models API"） |
| Retirement schedule 文档 raw markdown `raw.githubusercontent.com/MicrosoftDocs/azure-ai-docs/main/articles/foundry/openai/includes/concepts-model-retirement-schedule-content.md`（文章 `concepts/model-retirement-schedule.md` 只 include 该文件） | 无 | 各厂商表格：Lifecycle / Retirement date / Replacement；覆盖 Fireworks | **补充**：Replacement、Fireworks；Git commit 历史可回填过去 7 天 |
| Retail Prices API `prices.azure.com/api/retail/prices?$filter=serviceName eq 'Foundry Models'` | 无（公开） | 各计量表当前零售价（USD）、月粒度 effectiveStartDate | **价格变化数据源**（见 §3.3）；新计量表亦作为新模型上线的辅助信号 |
| Azure Updates RSS | 无 | GA/Preview 公告 | 可选，Phase 2 |

API 状态映射（官方）：`Preview`→Preview，`GenerallyAvailable`→GA，`Deprecating`→Deprecated(仅存量客户)，`Deprecated` 或 `deprecation.inference < today` →Retired(410)。

## 1.1 Phase 0 实测结果（2026-10-05）
- `locations/models` 最新 api-version **2026-09-01**（分页，需跟随 nextLink；eastus2 共 426 条/9 页）；37 个 region，`global` 返回 400 需跳过
- 2026-09-01 新增字段：`isMarketplaceRequired`、`replacementConfig{targetModelName,targetModelVersion,autoUpgradeStartDate,upgradeOnExpiryLeadTimeDays}`、`skus[].lifecycleStatus`、`skus[].scope`(Base/Finetune/All)、`modelCatalogAssetId`、`models`(model-router 成员)
- `lifecycleStatus` 实际取值：Preview / GenerallyAvailable / **Legacy**(文档 enum 未列) / Deprecating / Deprecated
- `kind`：OpenAI / AIServices / MaaS / MAI，同一模型多份重复 → 以 `AIServices`+`MAI` 为准，key=(format,name,version)
- format：OpenAI, Fireworks(47), Microsoft, DeepSeek, Mistral AI, Cohere, Meta, xAI, Anthropic, Black Forest Labs, MoonshotAI, OpenAI-OSS, Alibaba
- `isMarketplaceRequired`：false = 第一方 + Fireworks；true = Anthropic 及部分 Mistral/Cohere/Meta（作为字段保留，不做过滤）
- SKU 名称：GlobalStandard / DataZoneStandard / Standard / GlobalBatch / DataZoneBatch / DeveloperTier（token 计费）；*ProvisionedManaged（PTU）。部分 Fireworks 模型在某些 region 仅有 PTU
- 结论：ARM 单源即可覆盖 状态/退役日期/替代模型/自动升级日期；文档降级为交叉校验 + 首次回填

## 2. 范围
- 范围（2026-10-05 用户确认）：**所有可通过 API token 计费调用的模型**——Azure OpenAI、DeepSeek、MoonshotAI/Kimi、Microsoft/MAI、xAI、Alibaba、Fireworks、Anthropic、Meta、Cohere、Mistral、BFL、OpenAI-OSS 等
- 规则：`kind ∈ {AIServices, MAI}` 全部 format 纳入（Models API 本身不返回 HuggingFace/managed compute）；每个 SKU 标注 `billing=token|provisioned`，API 支持 `?billing=token` 过滤
- 排除：HuggingFace / managed compute 模型
- format 可配置 denylist 以备后用
- Regions：全部（动态枚举 Microsoft.CognitiveServices 支持的 location）

## 3. 架构（Azure Functions Flex Consumption, Python v2）
```
Timer (0 0 0 * * * UTC = 08:00 Asia/Shanghai)
  ├─ collect_arm()    → 遍历 regions → 聚合 key=(format,name,version)
  ├─ collect_docs()   → 解析 markdown 表格
  ├─ collect_prices() → Retail Prices API 全量（独立步骤，失败不影响模型事件）
  ├─ 待提交批次 → Blob snapshots/pending/YYYY-MM-DD/{arm,docs,prices}.json.gz
  ├─ diff(昨天, 今天) → Table `events`（observed，category=lifecycle|price）
  ├─ 计算 scheduled 事件（退役/弃用/SKU 下线/已公布的未来价格）→ Table `schedule`
  └─ 完成后发布快照 → snapshots/YYYY-MM-DD/{arm,docs,prices}.json.gz，并删除待提交批次
REST API（HTTP trigger，AuthLevel.FUNCTION，header `x-functions-key`）
  GET /api/changes/today?category=lifecycle|price&limit=200&cursor=
  GET /api/changes/past?days=7&category=&limit=200&cursor=
  GET /api/changes/upcoming?days=7&limit=200&cursor=
  GET /api/models?vendor=&status=&region=&billing=&name_contains=
  GET /api/models/{format}/{name}?version=
  GET /api/prices?model=&region=&deployment=
MCP Server（Functions MCP 扩展，Streamable HTTP，system key `mcp_extension`）
  POST /runtime/webhooks/mcp
        ↘ REST 与 MCP 共用 core/query.py（同一查询逻辑、同一返回结构）
```
事件类型：`MODEL_ADDED`、`MODEL_REMOVED`、`STATUS_CHANGED`(Preview→GA 等)、`NEW_VERSION`、`RETIREMENT_DATE_CHANGED`（模型 inference/fine_tune 或 SKU 退役日变化）、`REPLACEMENT_CHANGED`、`REGION_ADDED/REMOVED`、`SKU_ADDED/REMOVED`、`RETIRING`(计划)、`SKU_DEPRECATING`(计划)、`AUTO_UPGRADE_START`(计划，来自 replacementConfig)、`SKU_STATUS_CHANGED`；价格类：`PRICE_CHANGED`、`PRICE_ADDED`、`PRICE_REMOVED`（§3.3）。

- 今天 = 今日 diff 的 observed 事件 + 日期为今天的 scheduled 事件
- 未来 N 天 = scheduled 事件（ARM 日期 + 文档日期合并）
- 过去 N 天 = Table 历史；首次部署时用文档 Git 历史回填（§3.5），ARM 与价格无历史，从首份快照起累积
- 编排（`pipeline.py`）：ARM / 文档 / 价格三个步骤互相隔离，任一失败只记入报告 `failed`，其余照常入库；ARM 部分区域失败也标记 `status=degraded` 并列入 `failed`，有效区域仍入库。有失败时 timer 函数最终抛错，便于在 App Insights 告警。每个来源采集前先重放该来源未完成批次，保留原观测日期；恢复失败不覆盖待提交数据
- 窗口与聚合规则见 §3.4

## 3.1 认证（2026-10-06 决策：Function Key）
- 选型理由：部署账号无权创建 Entra App Registration，Easy Auth / MCP OAuth（PRM）不可行；调用方为 VS Code/GitHub Copilot 与非交互脚本，均支持自定义 header。
- **REST**：`func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)`，所有 HTTP 端点不开放匿名。每个调用方分配一个**命名 host key**（`az functionapp keys set --key-type functionKeys --key-name <client>`），便于单独吊销/轮换。
- **MCP**：保留 MCP 扩展默认 `system.webhookAuthorizationLevel = System`（不得改为 Anonymous），客户端以 `x-functions-key: <mcp_extension 系统密钥>` 访问。
- **禁止分发 master key**（`_master` 可访问所有端点与管理 API）。
- 密钥只通过 `az functionapp keys list/set` 获取，不写入仓库、不在 azd 输出中打印；文档只给出获取命令。
- 平台约束：HTTPS only、TLS ≥ 1.2、`ftpsState=Disabled`、禁用 basic publishing credentials。
- 本地 `func start` 不校验 key（Functions 本地行为），本地测试无需 key。
- 升级路径（若日后具备 Entra 权限）：开启 Easy Auth + `WEBSITE_AUTH_PRM_DEFAULT_WITH_SCOPES` 实现 MCP OAuth；或前置 APIM 做 key/OAuth/限流。

## 3.2 MCP 工具设计
实现：`api/mcp_tools.py` 中 `@bp.mcp_tool_trigger(tool_name=, description=, tool_properties=<JSON>)`（Functions MCP 扩展，仅用 Streamable HTTP；SSE 已被协议弃用且依赖 Queue，不启用）。工具返回紧凑 JSON 文本，与 REST 响应结构一致；参数错误返回 `{"error": ...}` 而非抛异常。REST 与 MCP 都只调用 `api/service.py`（读取快照/事件，快照缓存 10 分钟、采集状态缓存 15 秒）→ `core/query.py`。

| Tool | 参数 | 对应 REST |
|---|---|---|
| `get_today_changes` | `category?` `billing?` `limit?` `cursor?` `max_bytes?` | `/api/changes/today` |
| `get_upcoming_changes` | `days`(1–30，默认 7) `billing?` `limit?` `cursor?` `max_bytes?` | `/api/changes/upcoming` |
| `get_past_changes` | `days`(1–30，默认 7) `category?` `billing?` `limit?` `cursor?` `max_bytes?` | `/api/changes/past` |
| `search_models` | `vendor?` `status?` `region?` `billing?` `name_contains?` | `/api/models` |
| `get_model` | `name` `version?` `vendor?` | `/api/models/{format}/{name}` |
| `get_model_prices` | `model` `region?` `deployment?` | `/api/prices` |

- 工具 description 需写明状态术语映射（API `Deprecating` = 文档 Deprecated 等）、日期为 UTC、日界为 Asia/Shanghai，便于 LLM 正确解读。
- 变化查询默认每页 200 条，`limit` 范围 1–1000；返回 `truncated` 与 `next_cursor`，后者为 null 表示已取完。目录查询仍默认上限 200 条。
- REST/MCP 的三个变化查询均支持 `cursor`；保持相同查询条件传入上一页的 `next_cursor` 即可继续。同组事件可以跨页，`total_events` / `summary` 统计整个查询窗口。游标绑定窗口、过滤条件和结果内容；数据更新或跨日导致游标失效时，REST 返回 400、MCP 返回 error，调用方须不带游标重新开始。
- 变化响应额外受 `max_bytes` 约束（UTF-8 JSON 内容，默认 65536，范围 16384–1048576，不包含 MCP 协议外层封装）；预留 8 KiB 给游标和采集状态。先达到条数或字节预算即结束本页，字节不足时 `byte_limited=true`。超大价格事件的 `changes` 可跨页：同一事件 ID 会重复，`changes_offset` / `changes_total` 标明明细位置与总数，`details_truncated` 标明该事件是否仍有明细，须按 ID + offset 拼接，不能简单按 ID 去重。`returned_events` 计本页片段数，`total_events` 仍计完整事件数。单行明细或不可拆分事件仍过大时返回明确错误，要求增大预算，不静默丢弃。
- 所有查询成功响应均带 `collection`：`date` / `timezone`、`complete` 及按来源的 `status`（unknown/collecting/complete/degraded/failed/stale）、`last_attempt_at` / `last_success_at` / `snapshot_at`、`stale_regions` / `failed_regions`。状态缺失、当日未完成或缓存快照落后于已发布版本时 `complete=false`，不能解读为“没有变化”。变化查询按类别只检查对应来源；模型查询检查 ARM，当前价格查询检查 prices。此状态描述当前采集覆盖度，不保证整个历史窗口完整；docs 历史回填失败仍为 best-effort，不影响当天源采集成功。
- 只读工具；不提供任何写操作或触发采集的工具。

VS Code 客户端配置示例见 README。

## 3.3 价格变化（2026-10-06 调研，实测）
数据源：Azure Retail Prices API `GET https://prices.azure.com/api/retail/prices?$filter=serviceName eq 'Foundry Models'`（公开、免认证）

实测结果：
- 35,384 行 / 36 页（1000 行/页，跟随 `NextPageLink`），全量拉取约 50 秒；50 个 region、24 个 `productName`、仅 USD
- **只返回当前价格，无历史**；`effectiveStartDate` 为月粒度（每月 1 日）；未观察到未来生效的条目，且有发布滞后（10-06 时尚无 10-01 生效条目）
  → 价格变化主要靠每日快照 diff 获得；"未来 N 天价格变化"无稳定来源（若出现 `effectiveStartDate > collected_at` 则单独保存并生成 scheduled 事件，不提前作为当前价格）
- ARM Models API 的 `skus[].cost`（meterId）实际为空 → **价格无法与模型精确关联**
- 命名为非规范缩写（`5.4 opt Dz`、`K2.5 Thinking Inp glbl`、`FW DS-V4.1-Flash Gl Cd Inp`）；`armSkuName` 不可信（Grok 4.3 计量的 armSkuName 为 `Grok 9 Inp DZone`）
- 单位混用 `1K` / `1M`（token）、`1/Hour`（PTU/托管）等 → token 价格统一换算为 **USD / 1M tokens**
- **Anthropic（Marketplace 计费）不在 Retail Prices API 中**；Cohere / Meta / Mistral / Fireworks / xAI / DeepSeek / Kimi / MAI / Qwen 均有
- 同时包含 Managed Compute、PTU Reservation、Agent Pre-Purchase、Free Meter 等非目标产品，需过滤

设计：
- 快照：`snapshots/YYYY-MM-DD/prices.json.gz`；主键 `(meterId, armRegionName, type, reservationTerm, tierMinimumUnits)`。`prices` 保存采集时已生效的最新版本；`future_prices[key]` 保存按生效时间排序的未来版本列表，避免同一计量的当前/未来价相互覆盖。
- 未来价格：若 API 对已有计量仅返回未来价，沿用最近快照中已知的当前价；全新计量只有未来价时不进入当前价格查询。按生效时间逐批应用未来版本，复用价格 diff 生成 `kind=scheduled` 的 `PRICE_ADDED` / `PRICE_CHANGED`，同一计量的多个未来日期分别保留。未来计划消失时按 schedule 规则对账撤回；实际生效后仍保留独立的 observed 事实。
- 解析（纯函数 `core/price_parse.py`，2026-10-07 实现）：**规则词典**，不在运行时调用 LLM（输出不确定、无法离线测试、出错不可见）
  - token 计量的 `skuName` 按词切分，词典词归入维度：`token`(input/cached_input/cache_write/output)、`deployment`(global/datazone/regional)、`tier`(standard/priority/flex/batch)、`context`(short/long)；维度词之前的部分为模型名（如 `5.6 sol`）
  - 维度词之间允许（2026-10-07 补充）：模态词 `aud/audio/txt/text/img/image`（保留在模型名中，每种模态是独立价格模型，如 `gpt-image-1-inp-cached-img-dzone` → `gpt-image-1 img`）；末尾的日期戳 MMDD/MMDDYYYY（并入模型名，如 `gpt rt aud mn cd in DZ 1215` → `gpt rt aud mn 1215`）；末尾的 `L` = long context（Grok `4.3 Inp DZ L`，价格恰为基础计量 2 倍）
  - 名称含 `embed` 且无输入/输出词时视为 input（embedding 只按输入计费）
  - 拒绝规则（返回未解析，不猜测）：没有模型名、维度词之间或之后出现词典外的词（如 `gpt-35-trb16K-Batch-125-Inp-glbl` 的 `125`）、`L`/日期戳不在末尾、维度或模态重复、组合非法（如缓存+输出）、无 token 类型（如 `babbage-002-base-glbl`）
  - 校验：同一 `(productName, 模型名)` 下两个计量项解析出相同维度组合（如改名）→ 该模型整体视为未解析
  - fixture 覆盖率 99%（1377/1389）；全量（实测 2026-10-07）1473/1492，剩余为 babbage/davinci/gpt-35 等旧模型、驼峰粘连（`realtimePrvwAudInp`）、`BatchOutp`、grader；PTU 计量不解析，保持按计量输出
- 关联模型（`core/price_alias.py`）：best-effort 映射到 ARM `(format, name)`
  - 先查别名表（如 `V3.2 SP` → `DeepSeek-V3.2-Speciale`、`MM3.5` → `mistral-medium-3-5`、`gpt rt aud mn 1215` → `gpt-realtime-mini`），新增别名须在 ARM fixture 中存在（测试校验）
  - 再按规则生成候选：忽略大小写与空格/`.`/`_`/`-`，按 productName 补厂商前缀（`gpt-`/`DeepSeek-`/`grok-`/`Kimi-`/`Mistral-`/`Cohere-`/`MAI-`），依次去掉一个模态词（aud/txt/img）与一个日期戳（MMDD/MMDDYYYY），不递归；每个候选同时尝试缩写展开形式（`aud`→audio、`img`→image、`rt`→realtime、`mn`→mini、`trscb`/`tcrb`→transcribe）；从最具体到最宽松逐级尝试，首个有命中的级别必须唯一，否则不映射（如 `gpt-4o-aud-0603` 不会被映射为 `gpt-4o`）
  - fixture 覆盖 162/243 个价格模型（全量实测 176/255，计量项 1134/1473）；未映射的多为 ARM 中已无的旧模型（gpt-35/gpt-4-32K/Phi-3/o1-preview 等）；价格事件补充 `model{format,name}` 与 `mapped`，映射失败的保留 `price_model` 并标 `mapped=false`，仍然产生事件（不丢数据）；采集日志输出新出现的未解析计量名
- 事件：`PRICE_CHANGED`（old/new/变化百分比）、`PRICE_ADDED`（常伴随新模型/新部署类型上线）、`PRICE_REMOVED`；先把同一计量多区域的同向变化合并，再按解析出的模型 `(productName, 模型名)` + 类型 + 方向合并为**一条事件**，各计量项作为 `changes` 明细行；未解析的 token 计量退回单计量事件并标 `unparsed=true`
- 范围（2026-10-06 用户确认）：`type=Consumption` 中的 **token 按量价**（Global/DataZone/Regional/Batch/Priority）+ **PTU 小时价**（`billing=provisioned`）；排除 Reservation、Fine-tune 训练/托管价（meter 名含 `FT`/`ft`、`Deployment Hosting`、productName `Azure OpenAI PP FT*`）、`Managed Compute`、`Microsoft Agent Pre-Purchase Plan`、`Foundry Local*`、`*Free Meter`；Anthropic 价格暂不覆盖（Phase 4）；多 region 同向变化聚合为一条事件
- 事件统一带 `category`：`lifecycle` / `price`
- 价格为**零售标价**，不反映 EA/MCA 折扣与实际账单

## 3.4 变化检测与事件聚合（2026-10-07 实现，`core/`）
**快照**（`normalize.py` / `price_parse.py`，纯函数）
- 模型快照：`models["{format}|{name}|{version}"].regions[region] = {status, inference_retirement, fine_tune_retirement, replacement, skus}`；SKU 键为 `"{name}|{scope}"`（同名 SKU 会以 Base/Finetune 两个 scope 出现）；`AIServices` 与 `MAI` 重复项合并 SKU；日期统一为 UTC `YYYY-MM-DDTHH:MM:SSZ`
- 区域采集失败：沿用**最近成功发布快照**中的该区域数据并记入 `stale_regions`，包括今天此前成功采集的结果，避免同日失败重跑撤回已发布变化；diff 基线仍为早于今天的最近快照。无可用数据则记入 `failed_regions`，下次 diff 跳过该区域。缺少合法 `value` 数组、空目录或不含纳入范围模型的区域不视为成功；全部区域无效则不发布新快照，避免误报全部下线。
- 价格快照：仅 `Consumption` 中 token 价（`1K`/`1M` 且计量名以 `Tokens` 结尾，换算为 USD/1M tokens）与 PTU 小时价（名称含 `Provisioned`）；排除规则见 §3.3

**diff**（`diff.py`）：按键做结构化比较，不做文本 diff
- 逐区域比较字段，再把 `(type, model, sku, field, old, new)` 相同的区域变化**合并为一条事件**并附 `regions`；不同区域新值不同则分成多条
- 键新增：同 `(format, name)` 已有其他版本 → `NEW_VERSION`（附 `existing_versions` 及其状态，用于识别 Preview→GA），否则 `MODEL_ADDED`
- 价格按 `price_key` 比较换算后单价（单位从 1K 改为 1M 但折算后相同不算变化）；先按计量合并多区域同向变化（金额不一致时给出 `region_prices`），再按解析出的模型合并：事件含 `price_model`、`meter_count`、`regions`（并集）、`change_pct`（各行一致时才给出）与 `changes[]`（每行：`sku`、四个维度、`old`/`new`/`change_pct`，区域与事件不同时附 `regions`）。实测：`gpt-5.4` 30 个计量 × 8 区域降价，输出从 30 组（约 19.6 KB）降为 1 组（约 7 KB）

**计划事件**（`schedule.py`）：从最新快照的日期字段生成 `RETIRING` / `SKU_DEPRECATING` / `AUTO_UPGRADE_START`；SKU 退役日与模型退役日相同时不重复生成；入库对账规则：日期 ≤ 今天的条目视为历史冻结，未来条目若消失（日期变更或计划撤回）则删除

**事件公共字段**：`id`（对身份字段做 SHA-256 的确定性 ID，重跑幂等）、`kind`、`category`、`source`（`arm` / `docs` / `retail_prices`）、`type`、`date`（Asia/Shanghai 日期）、`model` / `price_model` / `meter`（三选一）、`sku`、`field`、`old`/`new`、`regions`、`billing`；observed 另含 `observed_at`/`baseline_at`，scheduled 另含 `effective_at`

**查询**（`query.py`，REST 与 MCP 共用）
- 窗口按 Asia/Shanghai 日界、互不重叠：today = `[D, D]`；past N = `[D-N, D-1]`；upcoming N = `[D+1, D+N]`；N ∈ 1–30
- **列出事件流水，不做净变化抵消**（如价格 1.00→1.20→1.10→1.00 返回 3 条）；按模型分组（价格事件已映射的按 ARM 模型 `{category: price, model}`，组内事件保留 `price_model`；未映射的按 `price_model`，未解析的按计量），组内按时间升序，同日计划事件排在观测事件前；past/today 组按最近事件倒序，upcoming 组按最早事件正序
- 计划退役（`RETIRING` inference / `SKU_DEPRECATING`）与 7 天内观测到的同模型同 SKU 下线或变为已退役（ARM 事件为 `Deprecated`，文档事件为 `Retired`）互相写入 `related_event_ids`，两条事实都保留
- 过滤：`category`、`billing`（2026-10-07 取消 `vendor`：价格事件无法可靠关联厂商，保留会导致按厂商查询时价格事件静默缺失）；默认每页 200 条，超出时 `truncated=true` 并给出 `next_cursor`，可完整取回所有事件；`total_events` 与 `summary`（按类型计数）不受分页影响；`unparsed_price_events` 给出未解析价格事件数，用于发现命名变化

- 目录查询：`search_models`（vendor/status/region/billing/name_contains 过滤，返回主状态、region 数、最早退役日）、`get_model`（region 记录相同的合并为 `availability` 组；未找到时给出 `suggestions`）、`get_prices`（先按价格→ARM 映射匹配，失败退回 `price_model` 名称包含匹配，`match` 字段说明匹配方式）；`limit` ≤ 1000

**存储**（`store.py`）
- Blob `snapshots/YYYY-MM-DD/{arm,docs,prices}.json.gz`（gzip mtime=0，同内容字节一致）；diff 基线取早于今天的最近一份同类快照，首份快照不产生 observed 事件
- `latest/{kind}.json.gz` 保存最新和前一日期的已发布快照路径；常规目录读取、采集基线读取无需枚举全部 Blob。同日重跑不推进前一日指针；旧布局无索引时回退枚举，首次成功发布后建立索引；查询更早历史仍允许回退枚举。
- 提交日志：先在 snapshots 容器的 `pending/YYYY-MM-DD/{kind}.json.gz` 保存快照、observed 事件和计划集合，再幂等写入事件/对账计划，最后发布快照并删除 pending 文件。任何阶段中断时，下次该来源运行先按日期重放；即使上游此时不可用，也先恢复已保存批次。普通快照查询忽略 pending 文件。
- 采集状态：每个来源开始前写 collecting，结束后写 complete/degraded/failed；`status/{kind}.json.gz` 保存最近状态，`runs/YYYY-MM-DD/{kind}.json.gz` 保存每天最近一次尝试的报告（同日重跑覆盖，不是逐次审计日志）。失败保留最后成功时间；恢复待提交批次后同步已发布快照时间。状态写入失败计入任务失败，其余来源继续。
- Blob/Table 不具备跨服务事务：提交中事件表可能短暂部分可见，但未完成的批次不会提前推进快照基线，且可以重放恢复。依赖 Timer 单写者，不能并发手工运行同一来源的采集。
- Table `events`（observed）/ `schedule`（scheduled）：PartitionKey = 本地日期，RowKey = 事件 `id`；索引列 `source`/`kind`/`category`/`type`，事件 JSON 按 ≤60 KB 分片存入 `body0..`（最多 16 片，Table 单属性 64 KB 限制）
- 查询将 category 过滤下推到 Table，并且 upcoming 仅读 schedule 表，减少网络传输与解压。Table 只有 PartitionKey/RowKey 索引，category 过滤本身不增加索引。计划对账从 `min(明天, 当前计算计划的最早日期)` 起查询，避免读取无关的更老历史，同时保留已有历史条目的冻结语义；如果当前快照仍引用很早的日期，仍需读取该日期之后的历史。
- 同一天重跑：用新结果替换该日该来源的 observed 事件（删除不再出现的行）；按分区每批 100 条事务提交

## 3.5 文档源（`core/docs.py` + `collectors/docs_schedule.py`）
- 解析：只解析同时含模型、Lifecycle、Retirement date 列的表格（跳过 fine-tuned 表）；清理 `<sup>`、链接、强调符号与括号注释；行键 `section|vendor|model|version`（重复行加 `#n`）；fixture 解析 183 行
- 关联 ARM：模型名（忽略大小写）+ 版本（文档给出时）匹配；唯一匹配 → 使用 ARM 的 `model` 引用与 `billing`；多个或无匹配 → `format=null`
- observed（source=docs）：只比较前后两个版本都存在的行的 lifecycle / 退役日 / 替代模型（`STATUS_CHANGED` / `RETIREMENT_DATE_CHANGED` / `REPLACEMENT_CHANGED`）；行增删不产生事件（模型增删以 ARM 为准，避免文档改版噪声）
- scheduled（source=docs）：文档退役日生成 `RETIRING`；ARM 已有同一本地日期的退役计划则跳过；不一致时保留并附 `arm_retirement_dates` 便于对照（fixture 中 13 个模型两源日期不一致）
- 首次部署回填：无 docs 快照时通过 GitHub commits API 取最近 30 天提交（含窗口起点前的基线版本），逐版本 diff 写入 observed 事件；失败只跳过回填，不影响当日采集

## 4. 目录结构
```
azure.yaml
.github/workflows/validate.yml               # Python 3.12 离线测试、依赖检查、脚本语法、Bicep 编译；无部署权限
deploy.sh                                   # Cloud Shell 部署脚本（az + python3），与 azd 共用 infra/
infra/ main.bicep, main.parameters.json, modules/{functionapp,storage,monitoring,identity,rbac}.bicep
src/ function_app.py, host.json, requirements.txt, pipeline.py   # pipeline：每日采集编排
     collectors/{_http.py, arm_models.py, docs_schedule.py, retail_prices.py}   # I/O：重试、分页、nextLink 主机校验
     core/{events.py, normalize.py, diff.py, schedule.py, docs.py, price_parse.py, price_alias.py, store.py, query.py, config.py}
     api/{service.py, http_routes.py, mcp_tools.py}   # Blueprint，薄适配层 → core/query.py
tests/ (fixtures + pytest，离线运行)
  fixtures/models_eastus2_2026-09-01.json   # Phase 0 真实响应（订阅 ID 已脱敏）
  fixtures/retail_prices_foundry_eastus2_2026-10-06.json   # Retail Prices 真实响应（eastus2 子集，1770 行）
  fixtures/model_retirement_schedule_2026-10-05.md         # 文档 include 文件（CC BY 4.0，首行注明出处）
```
IaC：Flex Consumption（FC1，Python 3.12）+ Storage（`allowSharedKeyAccess=false`，容器 `app-package`/`snapshots`，表 `events`/`schedule`）+ Log Analytics + App Insights（`DisableLocalAuth`，Entra 上报）+ User-assigned MI；MI 在订阅级授予 Reader，在存储上授予 Blob Data Owner / Queue / Table Data Contributor，在 App Insights 上授予 Monitoring Metrics Publisher；可选 `principalId` 为本地开发者授予 Blob/Table Data Contributor。

配置（`core/config.py`，环境变量；云端由 Bicep 写入 app settings，凭据为 UAMI `AZURE_CLIENT_ID`）：

| 变量 | 说明 |
|---|---|
| `FOUNDRY_SUBSCRIPTION_ID` | 采集 ARM 模型所用订阅（云端 = 部署订阅） |
| `STORAGE_BLOB_ENDPOINT` / `STORAGE_TABLE_ENDPOINT` | 快照与事件存储 |
| `MODELS_API_VERSION` | 可选，默认 `2026-09-01` |
| `FOUNDRY_REGIONS` | 可选，逗号分隔；默认枚举全部 region |
| `STORAGE_USE_EMULATOR` | 仅本地：`true` 时使用 Azurite 公开开发账号并自动建容器/表；云端不设置 |

### 4.1 部署方式（2026-10-06 新增 deploy.sh）
两种方式共用同一份 `infra/main.bicep`（`targetScope = 'subscription'`，在模板内创建资源组，便于订阅级 Reader 分配）：

| 方式 | 依赖 | 适用 |
|---|---|---|
| `azd up` | azd（+ az） | 本地开发、日常迭代 |
| `bash deploy.sh -e <env> -l <region>`（或 `curl -fsSL <raw>/main/deploy.sh \| bash -s -- -e <env>`） | 仅 `az` + `python3` + `curl`（Cloud Shell 内置） | Azure Cloud Shell 一键部署、无法安装 azd/func 的环境 |

Bicep 契约（两种方式都依赖，修改时须同步）：
- 参数：`environmentName`、`location`（azd 经 `infra/main.parameters.json` 映射 `${AZURE_ENV_NAME}`/`${AZURE_LOCATION}`）
- 输出：`AZURE_RESOURCE_GROUP`、`AZURE_FUNCTION_APP_NAME`（另有 `AZURE_FUNCTION_APP_HOST`、`STORAGE_BLOB_ENDPOINT`、`STORAGE_TABLE_ENDPOINT` 供本地开发）
- 可选参数：`principalId`（azd 映射 `${AZURE_PRINCIPAL_ID}`）、`maximumInstanceCount`（默认 40）、`instanceMemoryMB`（默认 2048）、`enableCollectionAlerts`（默认 false）、`alertActionGroupIds`（默认 []）
- 可选命名参数：`resourceGroupName`（默认空，使用 `rg-<environmentName>`）、`resourceNamePrefix`（默认空，保留旧命名）、`resourceGroupLocation`（默认 `location`，脚本选择已有组时读取其所在地，避免修改不可变的资源组所在地）。azd 无需新增必填参数，默认命名保持兼容。自定义前缀加在各资源类型前缀和唯一 token 之间；存储账户前缀去掉连字符并截取前 9 位，保留完整 token，长度不超过 24。自定义组名参与 token 计算；订阅 Reader 分配 ID 随自定义组/前缀变化，避免替换托管身份时修改不可变的角色分配。
- Flex 部署存储使用托管身份认证（`functionAppConfig.deployment.storage.authentication`），不依赖共享密钥

部署向导（2026-10-08）：不带参数可运行，环境名默认 `foundry-notify`。选定订阅后，从 `/dev/tty` 按顺序询问 region（支持 Flex 的区域编号或名称，默认 `eastus2`）、资源组（已有组编号/名称或新名称，默认 `rg-<env>`）、资源名称前缀（默认环境名）；回车保留参数/环境变量/默认值，确认后部署。命令行可用 `-g/--resource-group`、`--resource-prefix`，对应 `AZURE_RESOURCE_GROUP`、`AZURE_RESOURCE_NAME_PREFIX`；`-y` 与 `--what-if` 跳过向导且默认保留旧版哈希命名。`--skip-infra` 不询问命名选项，直接读取同名环境的部署输出。更改组名或前缀会创建新资源，不迁移数据；重复部署须沿用相同配置，预览自定义配置需显式传入对应参数。

`deploy.sh` 流程：参数校验 → 获取源码（脚本位于仓库 checkout 且未指定 `-r/--ref` 时用本地文件；否则（如 `curl | bash`）下载 `github.com/<DEPLOY_REPO>/archive/<ref>.tar.gz` 到临时目录，ref 默认 `main`）→ 订阅 RBAC 权限预检（仅告警）→ 注册资源提供程序（`--what-if` 只检查状态，不注册）→ 校验区域支持 Flex（`az functionapp list-flexconsumption-locations`）→ `az deployment sub create`（`--what-if` 仅预览）→ 读取部署输出 → 打包 `src/`（排除 `.venv`/`__pycache__`/`local.settings.json`/`tests`）→ `az functionapp deployment source config-zip --build-remote true`（Python 在 Flex 上必须远程构建）→ 等待完整 13 个函数注册 → 无 key 的 REST GET 与 MCP initialize POST 均须返回 401 → 打印端点与取 key 命令（**不打印密钥值**）。注册超时、任何端点鉴权不符或请求失败均非零退出，不输出部署完成。HTTP 探测设置连接及总时限；不获取任何密钥。部署名固定为 `foundry-notify-<env>`，重复执行幂等；`--skip-infra` 仅发布代码。脚本主流程包在 `main()` 中、末行调用（管道下载中断不会执行半截脚本）；确认提示从 `/dev/tty` 读取，无终端时须加 `-y`。

采集告警（显式启用）：建议首次采集成功后设置 `enableCollectionAlerts=true`；脚本支持 `--enable-alerts`（不能与 `--skip-infra` 同用），azd 可在 `infra/main.parameters.json` 中设置布尔值。每小时查询专用 Log Analytics workspace 的 AppRequests，最近一次 daily_collect 失败或 32 小时内没有完成调用时触发 severity 2 告警；成功后自动恢复。Request 遥测未采样，窗口 48 小时。规则可能产生 Azure Monitor 费用，默认不创建；通知需在门户配置 Action Group 或通过 `alertActionGroupIds` 指定已有组，无接收组时只生成告警记录。云端须验证实际遥测表、函数名称及通知投递。

仓库：`https://github.com/pczhao1210/ms-foundry-notification`（MIT）。CI 已提供 push/PR/手动触发的 Python 3.12 验证：安装固定版本依赖、pip check、pytest（含部署脚本模拟）、bash -n、Bicep 0.48.1 编译，仅授予 contents:read，不访问 Azure 订阅、不读取部署密钥。运行时及传递依赖固定在 `src/requirements.txt`，测试依赖固定在 `requirements-dev.txt`，与已验证的 Linux/Python 3.12 环境一致；升级时需整体解析依赖并重跑测试，固定版本不替代安全更新。自动部署（Functions Action + OIDC）仍为可选后续事项。

## 5. 风险 / 局限
1. 无法"预测"未来新模型上线——API 不暴露未发布模型；未来 7 天只含已知日期事件。
2. Models API 是订阅视角：受限/门控模型、或已 Deprecated 对新订阅不可见的模型可能缺失 → 用文档交叉校验。
3. Preview→GA 通常以"新 version"形式出现而非同版本状态翻转 → 增加按 model name 的 `NEW_VERSION`/GA 识别。
4. ~~Fireworks 覆盖待验证~~ 已确认覆盖。快照基于单一订阅视角，建议部署订阅即监控订阅。
5. 文档与 API 可能有滞后/不一致 → 两源都保留，事件带 `source` 字段。
6. Function Key 是共享密钥：泄露即可访问 → 每调用方独立命名 key、定期轮换；MCP 只能共用 `mcp_extension` 一个系统密钥，轮换需通知所有 MCP 客户端。数据本身为公开目录信息，风险可接受。
7. Functions 无内置限流 → 依赖 Flex 实例上限（`maximumInstanceCount`）控制成本；需要时再前置 APIM。
8. 本地已验证 `azure-functions` 1.25 可注册 `mcpToolTrigger`（Python 3.12）；extension bundle `[4.0.0, 5.0.0)` 是否包含 MCP 扩展、Flex 上 MCP 端点行为待首次部署验证（不满足时改用 Preview bundle）。
9. 价格计量名为非规范缩写且随模型代际变化（如 `5.4` → `5.6 sol` 新增 `ShortCo`/`Std`/`Cd Wr`）：解析失败只会退回逐计量输出（`unparsed=true`），不会给出错误维度；出现新的未解析名称时补词典 + fixture 回归测试。价格→ARM 模型映射依赖别名表，新模型上线初期可能 `mapped=false`。
10. Anthropic 等 Marketplace 计费模型无公开价格 API，价格变化不覆盖（模型生命周期变化仍覆盖）。
11. Retail Prices 发布滞后且月粒度，价格变化的"观测日"可能晚于实际生效日；事件同时记录 `observed_at` 与 `effectiveStartDate`。
12. 文档 Git 历史回填使用未认证的 GitHub API（60 次/小时/IP），共享出口 IP 可能被限流；回填为 best-effort，失败只缺少首次部署前的文档历史。
13. 部署关闭了 SCM basic auth：`deploy.sh` / azd 发布代码依赖 Entra 令牌部署（需较新版本 az CLI / azd）。

## 6. 阶段
- Phase 0 ✅：用真实订阅跑发现脚本，确认 `kind/format` 取值、region 数量、Fireworks 覆盖
- Phase 1 ✅：collectors + normalize + diff + 文档解析 + 价格解析/映射 + 单元测试（本地可跑）
- Phase 2 ✅（代码）：Function app（timer + 6 个 REST + 6 个 MCP 工具）+ Blob/Table 存储 + 文档 Git 历史回填；离线单元测试与故障注入回归覆盖提交恢复、同日重跑、空响应、分页及未来价格
- Phase 3：Bicep/azd + `deploy.sh`
  - 已完成：Bicep 模块（`bicep build` 无告警）、`azure.yaml`、`main.parameters.json`
  - 已完成：部署脚本离线模拟测试（预览无写入、完整函数注册、REST/MCP 鉴权失败时非零退出）
  - 待完成：Cloud Shell 实测一次部署；部署验证（无 key → 401；REST/MCP 端到端调用；首次 timer 运行与回填）
- Phase 4（可选）：Azure Updates RSS 信号、推送通知、Anthropic 价格（解析文档）
