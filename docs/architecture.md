# Architecture and Design

**English** | [简体中文](architecture.zh-CN.md) | [README](../README.md)

## Overview

Foundry Notifier is a snapshot-based change tracker. A scheduled job collects model catalogs, retirement documentation, and retail prices, then turns their differences and announced dates into queryable events. REST clients and MCP clients read the same stored results; a query does not trigger collection or call the upstream data sources.

The system runs in one Azure Functions Flex Consumption app using the Python v2 programming model. Azure Blob Storage holds snapshots and collection state, Azure Table Storage holds events, and a user-assigned managed identity authorizes access to ARM and storage.

## System Structure

![Foundry Notifier architecture: collection and read-only query paths, storage, managed identity, and telemetry](../assets/architecture.png)

The diagram shows logical data flow, not additional services: the collection pipeline, API service, and query logic are modules inside the same Function App. Clients access data only through REST or MCP; the API service handles storage reads and passes loaded data to the query core. There is no queue-based business pipeline or separate MCP server deployment.

| Component | Responsibility |
|---|---|
| Timer trigger | Starts collection on `DAILY_COLLECT_SCHEDULE`; defaults to UTC 00:00 / Asia/Shanghai 08:00 daily. |
| Collectors | Handle network I/O, authentication, pagination, and retries for each source. |
| Core transformations | Normalize records, compare snapshots, derive scheduled events, and interpret price meters using deterministic rules. |
| Collection pipeline | Coordinates source steps, recovery, persistence, and per-source health reporting. |
| Blob Storage | Stores compressed daily snapshots, latest-snapshot indexes, pending batches, and collection status. |
| Table Storage | Stores observed events in `events` and announced-date events in `schedule`. |
| API service and query core | Load stored data, apply shared window/filter/grouping/pagination rules, and attach collection health. |
| HTTP and MCP triggers | Authenticate and adapt requests to the same query service. Both interfaces are read-only. |
| Application Insights / Log Analytics | Record execution telemetry, failures, and degraded collection warnings; optional alerts monitor collection failures or inactivity. |

## Data Sources and Authority

| Source | Role | Important boundary |
|---|---|---|
| ARM Models API | Primary source for model versions, regional availability, SKU status, retirement dates, replacements, and auto-upgrade dates. | Subscription-scoped visibility, not a universal catalog. Regions are discovered dynamically; all result pages are followed. |
| Microsoft Foundry retirement documentation | Collects the [published Learn schedule](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule), preferring Markdown with an HTML fallback; GitHub is only for best-effort initial history backfill. | Documentation can lag or disagree with ARM. Events retain their source rather than silently choosing one date. |
| Azure Retail Prices API | Current retail token and PTU hourly prices; daily snapshots provide price-change history. | No general historical-price or future-price feed. Marketplace prices such as Anthropic are not covered. |

ARM records use `kind` values `AIServices` and `MAI` to avoid duplicate catalog views. Models are identified by `(format, name, version)` and SKU entries by `(name, scope)`. Regional differences remain visible. Marketplace-required models are not excluded from lifecycle tracking; HuggingFace / managed-compute models are outside scope.

Prices cannot be joined to ARM using a reliable shared meter ID. Token meters are parsed with a rule dictionary, normalized to USD per 1M tokens, and associated with models through aliases and conservative name matching. An ambiguous association stays `mapped=false`; an unrecognized meter stays `unparsed=true` and produces per-meter events instead of guessed dimensions. Neither case drops the data. No runtime LLM is used in collection or price parsing.

## Collection and Publication

Each run executes the ARM, docs, and prices steps in sequence with isolated failure handling. Each source follows this flow:

1. Mark the source as collecting and replay any saved pending batches before fetching new data.
2. Collect and normalize a snapshot. Compare it with the most recent published snapshot from an earlier day, not necessarily yesterday.
3. Produce `observed` events from differences and `scheduled` events from announced dates.
4. Save a pending batch containing the snapshot and its events before changing the tables.
5. Replace that source's observed events for the day and reconcile its scheduled events.
6. Publish the snapshot and update its latest index, remove the pending batch, and record source health.

The first ARM or price snapshot establishes a baseline rather than reporting the whole catalog as newly added. Docs can backfill changes from recent Git history on first collection; failure to backfill does not stop the current day's collection.

Docs must contain all five schedule columns and recognized lifecycle values. Empty results or loss of more than 20% of previously published row identities are rejected before committing; the last successful snapshot and schedules remain available. Fetch diagnostics include the source URL, HTTP status, and failure stage. Markdown that contains only an include reference is not accepted as a schedule. Current Learn content is never substituted for historical Git versions.

### Storage Layout

All paths below are inside the `snapshots` Blob container:

| Path or table | Contents |
|---|---|
| `YYYY-MM-DD/{arm,docs,prices}.json.gz` | Published daily snapshots. |
| `latest/{source}.json.gz` | References to the latest and previous-date published snapshots, avoiding full Blob enumeration for normal reads. |
| `pending/YYYY-MM-DD/{source}.json.gz` | Replayable, not-yet-completed collection batches; ordinary snapshot queries ignore them. |
| `status/{source}.json.gz` | Latest per-source collection state and timestamps. |
| `runs/YYYY-MM-DD/{source}.json.gz` | Latest attempt report for each day, not a log of every attempt. |
| Table `events` / `schedule` | Date partitions with deterministic event IDs as row keys; large event bodies are split across properties. |

### Reliability Boundaries

- A docs or prices failure does not stop the other sources from committing their data. The Timer reports failure after all steps finish if any source or status write failed.
- For failed ARM regions, reuse the latest published regional data when available and mark it stale. This prevents a transient regional outage from looking like mass model removal. If no region returns a valid catalog, do not publish a new ARM snapshot.
- Partial regional coverage is `degraded`, not a failed invocation. Clients still receive `collection.complete=false` with the affected regions.
- Deterministic event IDs and source/day replacement make reruns idempotent. Future plans that disappear are removed; plans dated today or earlier remain as history.
- Blob and Table updates are not a distributed transaction. Events can be partially visible during a commit; pending-batch replay repairs interrupted publication without prematurely advancing the snapshot baseline.
- Collection assumes a single writer. Do not run overlapping manual collections alongside the Timer.

## Event and Query Model

Every event records `source` (`arm`, `docs`, or `retail_prices`), `kind` (`observed` or `scheduled`), and `category` (`lifecycle` or `price`).

`observed` means a change was found between snapshots, not that it happened at the collection instant. Examples include new versions, Preview-to-GA transitions, regional or SKU changes, retirement-date changes, and price changes. `scheduled` means a source announced an effective date, such as retirement or auto-upgrade start; it does not prove that the change has happened.

Timestamps are stored and compared in UTC. Query day boundaries use Asia/Shanghai:

| Query | Window and content |
|---|---|
| Today | Today's observed events and scheduled events dated today. |
| Past N days | The N days before today, excluding today; includes both event kinds. |
| Upcoming N days | The N days after today, excluding today; scheduled events only. |
| Models / prices | Latest published snapshots, not live upstream responses. |

Changes are grouped by model and equivalent regional changes are aggregated. The event history is not reduced to a net difference: a price increase followed by a decrease remains two facts. Scheduled retirements and matching observed retirements may be linked, but both are retained.

Both interfaces share filters and response structures through the query core. Change responses have count and byte limits, a `truncated` flag, and a continuation cursor. Large price-detail lists can span pages. See [API and MCP](api.md) for the complete pagination contract.

Snapshot reads are cached in-process for 10 minutes and collection status for 15 seconds. Successful queries include `collection` metadata so clients can distinguish no changes from missing, stale, or incomplete collection. That metadata describes current collection coverage, not guaranteed completeness of every day in a historical window.

## Security and Deployment

Client authentication and backend authorization are separate:

| Boundary | Mechanism |
|---|---|
| Client -> REST | Function-level authentication with a named host key per caller in `x-functions-key`. |
| Client -> MCP | Streamable HTTP at `/runtime/webhooks/mcp`, using the `mcp_extension` system key and System-level webhook authorization. SSE is not enabled. |
| Function App -> ARM | `DefaultAzureCredential` with the user-assigned managed identity; subscription Reader permission. |
| Function App -> Blob / Table | Identity-based data access with storage RBAC. Storage shared-key access is disabled. |
| Local development -> Azure | `DefaultAzureCredential` using developer credentials; Azurite is an explicit local-only alternative. |

Neither public interface is anonymous, and neither exposes writes or collection triggers. Never distribute the master key. Function keys are shared secrets rather than user identity; REST keys can be rotated per caller, while rotating the MCP system key affects all MCP clients.

Both `azd` and the Cloud Shell-friendly deployment script use the same Bicep infrastructure: a resource group, Function App and Flex plan, user-assigned identity, Storage account, Application Insights, Log Analytics, and RBAC assignments. Optional collection alerts check execution failure and inactivity, not regional completeness. There is no built-in application rate limiter; the Flex instance cap constrains scaling, not request rate.

## Design Choices and Limits

- Daily snapshots trade real-time notification for reproducible comparisons and inexpensive read-only queries. ARM and price history accumulate only after the first baseline.
- Upcoming changes depend on announced dates. The service cannot predict unreleased models or prices; future price events are possible only if the source actually publishes future-effective entries.
- Source provenance matters: ARM visibility, documentation discrepancies, and retail-price publication delays can affect interpretation. Retail prices are list prices, not negotiated discounts or actual billing.
- Keeping network I/O separate from pure transformations allows offline fixture tests. Sharing the query core prevents REST and MCP from developing different event semantics.

## Implementation Map

| Code | Ownership |
|---|---|
| [function_app.py](../src/function_app.py) / [pipeline.py](../src/pipeline.py) | Trigger registration and collection orchestration. |
| [collectors](../src/collectors/) | Upstream network access. |
| [core](../src/core/) | Normalization, lifecycle and price diffs, schedule derivation, documentation parsing, and configuration. |
| [store.py](../src/core/store.py) | Blob/Table persistence, publication, recovery, and storage queries. |
| [query.py](../src/core/query.py) | Shared event and catalog query semantics. |
| [api](../src/api/) | Storage-backed query service and REST/MCP adapters. |
| [infra](../infra/) | Shared Bicep deployment. |

For usage, see [API and MCP](api.md). For operational procedures, see [Deployment and Operations](deployment.md). For local setup and tests, see [Development](development.md). Detailed design notes and source investigations remain in the [project plan](plan.md) (Chinese).