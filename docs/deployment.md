# Deployment and Operations

**English** | [简体中文](deployment.zh-CN.md) | [Project Overview](../README.md)

## Runtime and Deployment

- Sources: Azure Resource Manager Models API (primary), the official [Model retirement schedule](https://learn.microsoft.com/azure/foundry/openai/concepts/model-retirement-schedule) (supplementary), and the [Azure Retail Prices API](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices) (retail list prices; excludes Marketplace models such as Anthropic).
- Runtime: Azure Functions Flex Consumption (Python), daily at 08:00 Asia/Shanghai.
- Deployment: run `deploy.sh` in Azure Cloud Shell, or use `azd up` (both share `infra/main.bicep`). Script help, wizard prompts, logs, errors, and the completion summary are bilingual in Chinese and English.

### Option 1: One-Command Deployment in Azure Cloud Shell

Open [Azure Cloud Shell](https://shell.azure.com), select **Bash**, and run (no tools to install):

```bash
curl -fsSL https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh | bash
```

By default, subscription selection precedes a four-step deployment wizard. Press Enter at each step to keep its default.
If several subscriptions are Enabled, the script lists their names and IDs. Enter a number, name, or ID; Enter keeps the current subscription. A single enabled subscription is selected automatically. Setting `-s/--subscription` or `AZURE_SUBSCRIPTION_ID` skips this step. Use a number or ID to distinguish subscriptions with duplicate names.

1. Select a region: choose a number or name from regions supporting Flex Consumption; default `eastus2`.
2. Select a resource group: choose a number, an existing name, or a new name in the current subscription; default `rg-foundry-notify`. Existing groups retain their location; resources deploy to the selected region.
3. Enter a resource name prefix: default `foundry-notify`.
4. Confirm the UTC schedule: enter a six-field NCRONTAB; default `0 0 0 * * *` (daily at 08:00 Asia/Shanghai). Deployment starts only after final confirmation.

`-e <env>` changes the environment name, default resource group, and wizard's default prefix; the environment defaults to `foundry-notify`. Preset individual steps with `-l`, `-g/--resource-group`, `--resource-prefix`, and `--schedule`, or their respective environment variables: `AZURE_LOCATION`, `AZURE_RESOURCE_GROUP`, `AZURE_RESOURCE_NAME_PREFIX`, and `DAILY_COLLECT_SCHEDULE`.

The script downloads this repository's `infra/` and `src/` into a temporary directory, deploys them, and cleans up automatically. Common variations (script arguments follow `bash -s --`):

```bash
URL=https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh
curl -fsSL $URL | bash -s -- -e prod --what-if            # Preview infrastructure changes only
curl -fsSL $URL | bash -s -- -e prod -g rg-prod --resource-prefix prod -y  # Noninteractive deployment
curl -fsSL $URL | bash -s -- -e prod --skip-infra -y      # Update code only after deployment
curl -fsSL $URL | bash -s -- -e prod -r <tag-or-commit>   # Deploy a specific version (default: main)
curl -fsSL $URL | bash -s -- -h                           # All options
```

Alternatively, clone the repository and run the script. Running from the checkout without `-r` uses local files, allowing you to deploy local changes:

```bash
git clone https://github.com/pczhao1210/ms-foundry-notification.git
cd ms-foundry-notification
bash deploy.sh
```

Resource names retain a uniqueness suffix, for example `func-<prefix>-<token>`. Storage account names remove `-` from the prefix, use its first nine characters, and retain the full uniqueness suffix to fit the 24-character limit. Subsequent infrastructure deployments must use the same environment name, region, resource group, and prefix. Changing the resource group or prefix creates new resources without migrating data or deleting old resources. `-y` and `--what-if` skip subscription selection and the wizard, using the specified or current subscription. Noninteractive runs without an explicit prefix retain legacy hash-based naming. To preview wizard settings, explicitly pass `-g`, `--resource-prefix`, and any custom `--schedule`. `--skip-infra` still selects a subscription when needed, but skips the four configuration steps and reads the existing environment's deployment outputs in that subscription to update code.

The script requires only `az`, `python3`, and `curl`, all included in Cloud Shell. It selects deployment options, resolves source files, deploys subscription-scoped Bicep, packages `src/`, publishes through `az functionapp deployment source config-zip --build-remote true` (Flex Consumption remote build), verifies registration of all 13 functions and 401 responses to unauthenticated REST/MCP requests, then prints endpoints and key-management commands without printing key values. Failed verification exits nonzero. You must still verify actual REST/MCP queries using client keys in your own terminal. `--what-if` neither registers providers nor deploys resources. Repeated runs with the same configuration are idempotent. Piped runs read wizard input and confirmation from `/dev/tty`; add `-y` when no terminal is available, such as in CI.

Both deployment and `--what-if` retain default Provider validation. Deployment outputs are read as JSON through `az deployment sub show`. The script queries the Function App and publishes code only if the deployment state is `Succeeded` and both `AZURE_RESOURCE_GROUP` / `AZURE_FUNCTION_APP_NAME` are valid names. Output keys are uniquely matched case-insensitively, accepting ARM responses such as `azurE_RESOURCE_GROUP` / `azurE_FUNCTION_APP_NAME` without changing resource name values. Conflicting keys differing only in case cause an error. Logs distinguish deployment, output retrieval, and target resources. Missing or invalid outputs report only field names and types, not values; TSV's `None` is never treated as a resource name. Enter preserves the wizard's displayed default. If infrastructure succeeded and only output retrieval failed, resume with the updated script and `--skip-infra` in the same subscription and environment rather than redeploying infrastructure.

The Function App hostname is also read as JSON through `az functionapp show -o json`, supporting both flattened CLI responses and ARM's `properties.defaultHostName`. The script uniquely matches case variants such as `defaultHostName` / `defaultHostname` only at the top level and inside `properties`, then validates the DNS name before publishing. It does not recursively search other objects. Missing, empty, duplicated, case-conflicting, or unreadable hostnames stop deployment; missing-field diagnostics list top-level and `properties` field names without values. The script never constructs `https:///api/...` or guesses the domain from the app name. If an older script reports `Could not resolve host: api`, or claims `defaultHostName` is missing when the response contains `properties`, inspect the response structure first. Once infrastructure has succeeded, the fixed script with `--skip-infra` can resume publication and verification without recreating infrastructure.

`RoleAssignmentUpdateNotPermitted` means deployment tried to modify immutable fields of an existing role assignment, such as its principal or scope. The old template's subscription-level Reader assignment ID omitted `principalId`, so deleting and recreating a managed identity with the same name reused the old ID. IDs now derive from the subscription, actual `principalId`, and Reader role: rerunning with the same identity keeps the ID; a new identity gets a new assignment. Old assignments are not deleted automatically. Inspect the failed resource in the selected subscription to confirm whether this assignment caused the failure:

```bash
az deployment operation sub list --name foundry-notify-foundry-notify \
  --query "[?properties.provisioningState=='Failed'].{resource:properties.targetResource.id,error:properties.statusMessage}" -o json
```

When migrating from the old template, the new naming may produce `RoleAssignmentExists` if **the same current identity** already has subscription-level Reader. After confirming that the existing assignment matches the `principalId`, subscription scope, and Reader role, set `AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME` to its **name (GUID, not the full resource ID)**. For azd, put the same GUID in `subscriptionReaderAssignmentName.value` in `infra/main.parameters.json`. Leave it empty for new environments or recreated identities; never reuse a conflicting assignment ID belonging to an old identity. Preserve this reuse setting in subsequent deployments, and do not bulk-delete subscription role assignments.

If an infrastructure failure left the deployment record in `Failed`, complete infrastructure deployment again using the fixed template and the same subscription, environment, region, resource group, and prefix before publishing code. You cannot immediately use `--skip-infra`, which requires a `Succeeded` deployment.

> `raw.githubusercontent.com` caches content for approximately five minutes, so newly pushed changes may take time to appear. Use `-r <commit>` for an exact source version.

Required permissions: subscription-level **Owner**, or Contributor + User Access Administrator (to assign subscription-level Reader to the managed identity).

### Option 2: azd

```bash
azd auth login
azd up        # Prompts for environment name / subscription / region
```

The first collection runs at the next UTC 00:00 (08:00 Asia/Shanghai) by default. Until then, `/api/models` and `/api/prices` return 503 because no snapshots exist, while change endpoints return empty results with `collection.complete=false`. The first run backfills the last 30 days of documentation changes using Git history; ARM and price changes begin on the second day.

Collection results are first saved as a pending batch. Snapshots are published only after events and schedules are written successfully. If a write is interrupted, the next run resumes the batch with its original date before collecting again. Do not manually trigger parallel collections for the same source. ARM regional failures retain each affected region's most recent successful data and mark the run degraded; other sources continue. An empty catalog is not treated as removal of all models. If valid data is saved but some regions are missing, the report records `degraded` and logs a WARNING; the portal invocation is marked successful, but REST/MCP still return `collection.complete=false`. Actual collection, commit, or status-write failures record `failed` and fail the invocation. Generated files do not prove every step succeeded; inspect `daily run report` and exceptions in Application Insights.

Latest snapshots are read through a lightweight index. Older storage layouts gain an index automatically after the next successful collection. Per-source reports are stored in the snapshots container at `status/{kind}.json.gz` and `runs/YYYY-MM-DD/{kind}.json.gz`; daily reports retain the day's last attempt.

### Collection Schedule (UTC)

Running `bash deploy.sh` prompts for the UTC schedule after the resource prefix; `--schedule` is not required. Enter keeps `0 0 0 * * *`, daily at UTC 00:00 / 08:00 Asia/Shanghai. Use a six-field NCRONTAB (second, minute, hour, day, month, weekday) without quotes in the wizard. You can preset this step with `--schedule`; quote the expression on the command line. For example, daily at UTC 01:30 / 09:30 Asia/Shanghai:

```bash
bash deploy.sh -e <env> --schedule '0 30 1 * * *'
```

One-command deployment also supports `--schedule '0 30 1 * * *'` after `bash -s --`. Alternatively, set `DAILY_COLLECT_SCHEDULE`; the command-line argument takes precedence. The script checks the six-field format, while Azure Functions validates the expression's values. For azd, edit `dailyCollectSchedule.value` in `infra/main.parameters.json` before deploying; the default is the same.

The schedule is stored in the Function App setting `DAILY_COLLECT_SCHEDULE`, referenced by the Timer as `%DAILY_COLLECT_SCHEDULE%`. Existing environments need both infrastructure and code deployment when first upgrading to this setting; `--skip-infra` alone is insufficient. Later schedule-only updates can use `--skip-code --schedule '...'`, or edit and apply the setting in the portal under Settings > Environment variables > App settings. Changing app settings normally restarts the app. An explicit schedule cannot be combined with `--skip-infra`. Infrastructure deployment without a schedule override restores the default, so pass the schedule you want to preserve on subsequent deployments.

Flex Consumption does not support `WEBSITE_TIME_ZONE` / `TZ`; do not add those settings. Schedule expressions always use UTC. Event query day boundaries still use Asia/Shanghai. Collection health alerts retain a 32-hour inactivity threshold; adjust the alert design if you configure less frequent collection.

### Manual Runs and Portal Troubleshooting

In the Azure portal, open the Function App > Functions > `daily_collect` > Code + Test > Test/Run. Select `_master` in the key dropdown, use `{"input":""}` as the request body, and run once. This calls the `/admin/functions/daily_collect` admin endpoint; ordinary host keys and `mcp_extension` do not apply. Select the key only in the portal; never copy, distribute, or log its value. HTTP 202 means only that the request was accepted. Check monitoring/invocation records for the final result, and do not trigger parallel runs.

`HTTP 0` / `Failed to fetch` in the portal means the browser did not receive a readable HTTP response. It does not by itself prove collection failed or the key was wrong. Check invocation records first to avoid resubmitting an already accepted request, then inspect browser developer tools for CORS, DNS/TLS, proxy, or access-restriction errors. The template allows only the CORS origin `https://portal.azure.com`, without wildcards and with `supportCredentials=false`; Function Key authentication remains required. For older deployments, inspect and add the portal origin in Cloud Shell under the original subscription without republishing code:

```bash
az functionapp cors show -g <resource-group> -n <function-app>
az functionapp cors add -g <resource-group> -n <function-app> \
  --allowed-origins https://portal.azure.com -o none
```

After saving, refresh the portal and test again. If errors persist, share only network diagnostics with keys and sensitive headers removed. Do not disable authentication or add a `*` origin for troubleshooting.

### Optional Collection Alerts

After the first successful collection, run `bash deploy.sh -e <env> --skip-code --enable-alerts` to enable collection health alerts. For azd, set `enableCollectionAlerts.value` to true in `infra/main.parameters.json` and provision again. The rule checks hourly for recent collection failures or more than 32 hours without a completed invocation. It is disabled by default and may incur Azure Monitor charges when enabled.

The rule does not alert on successful but degraded invocations with missing regions. Use the response's `collection` field to assess completeness; missing regions also appear in `daily arm step degraded` WARNING logs.

Attach an Action Group in the Azure portal or provide existing groups through the Bicep parameter `alertActionGroupIds`. Without one, alerts are recorded but no notifications are sent.
