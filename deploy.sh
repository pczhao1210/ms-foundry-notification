#!/usr/bin/env bash
# 不依赖 azd / func 的部署脚本，适用于 Azure Cloud Shell（仅需 az + python3 + curl）。
# 支持 curl | bash 一键运行：源码从 GitHub 下载；在仓库 checkout 中运行且未指定 --ref 时使用本地文件。
# 与 azd 共用 infra/main.bicep（订阅级部署），代码通过 Flex Consumption 的 zip 部署 + 远程构建发布。
set -euo pipefail

REPO="${DEPLOY_REPO:-pczhao1210/ms-foundry-notification}"
ENV_NAME="${AZURE_ENV_NAME:-}"
LOCATION="${AZURE_LOCATION:-eastus2}"
SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-}"
RESOURCE_GROUP_NAME="${AZURE_RESOURCE_GROUP:-}"
RESOURCE_PREFIX="${AZURE_RESOURCE_NAME_PREFIX:-}"
SUBSCRIPTION_READER_ASSIGNMENT_NAME="${AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME:-}"
COLLECT_SCHEDULE="${DAILY_COLLECT_SCHEDULE-0 0 0 * * *}"
SCHEDULE_SPECIFIED=false
if [[ ${DAILY_COLLECT_SCHEDULE+x} ]]; then SCHEDULE_SPECIFIED=true; fi
REF=""
WHAT_IF=false
SKIP_INFRA=false
SKIP_CODE=false
ASSUME_YES=false
ENABLE_ALERTS=false

usage() {
  cat <<EOF
用法 / Usage:
  curl -fsSL https://raw.githubusercontent.com/${REPO}/main/deploy.sh | bash -s -- [选项 / options]
  bash deploy.sh [选项 / options]

默认先选择订阅（多个可用订阅时），再选择区域、资源组、资源名称前缀、UTC 触发时间；回车保留默认。
Select a subscription (if several are enabled), then a region, resource group, resource name prefix, and UTC schedule; press Enter to keep defaults.
-s / AZURE_SUBSCRIPTION_ID 指定订阅时不再询问；-y / --what-if 跳过向导。
-s / AZURE_SUBSCRIPTION_ID skips subscription selection; -y / --what-if skips the wizard.

  -e, --env-name NAME       环境名（默认 foundry-notify，3-16 位小写字母/数字/-），也可用 AZURE_ENV_NAME
                           Environment name (default: foundry-notify; 3-16 lowercase letters/digits/hyphens); also AZURE_ENV_NAME.
  -l, --location REGION     部署区域（默认 eastus2，需支持 Flex Consumption），也可用 AZURE_LOCATION
                           Deployment region (default: eastus2; must support Flex Consumption); also AZURE_LOCATION.
  -g, --resource-group NAME 资源组（默认 rg-<env-name>），也可用 AZURE_RESOURCE_GROUP
                           Resource group (default: rg-<env-name>); also AZURE_RESOURCE_GROUP.
      --resource-prefix NAME 资源名称前缀（向导默认环境名，3-16 位小写字母/数字/-）
                           也可用 AZURE_RESOURCE_NAME_PREFIX；非交互未指定时保留旧命名
                           Resource name prefix (wizard default: environment name; 3-16 lowercase letters/digits/hyphens).
                           Also AZURE_RESOURCE_NAME_PREFIX; unset in noninteractive mode retains legacy naming.
  -s, --subscription ID     目标订阅（默认当前 az 订阅），也可用 AZURE_SUBSCRIPTION_ID
                           Target subscription (default: current az subscription); also AZURE_SUBSCRIPTION_ID.
  -r, --ref REF             从 github.com/${REPO} 下载指定分支/标签/commit 的源码
                           （默认 main；在仓库 checkout 中运行且未指定时使用本地文件）
                           Download a branch/tag/commit from github.com/${REPO} (default: main).
                           Uses local files when run from a checkout without an explicit ref.
      --what-if            仅预览基础设施变更，不做任何修改
                           Preview infrastructure changes without modifying anything.
      --skip-infra         跳过 Bicep，仅发布代码（需已部署过同名环境）
                           Skip Bicep and publish code only (requires an existing deployment for this environment).
      --skip-code          仅部署基础设施，不发布代码
                           Deploy infrastructure only; do not publish code.
      --enable-alerts      启用采集失败/超过 32 小时未完成告警（建议首次采集成功后启用；可能产生 Monitor 费用）
                           Enable collection failure/32-hour inactivity alerts after the first successful collection; Monitor charges may apply.
      --schedule EXPR      UTC 六字段 NCRONTAB（须加引号；默认 '0 0 0 * * *'，北京时间 08:00）
                           也可用 DAILY_COLLECT_SCHEDULE；需要部署基础设施
                           Quoted six-field UTC NCRONTAB (default: '0 0 0 * * *', 08:00 Asia/Shanghai).
                           Also DAILY_COLLECT_SCHEDULE; requires infrastructure deployment.
  -y, --yes                跳过向导及确认，使用参数/环境变量/默认值
                           Skip the wizard and confirmation; use arguments/environment variables/defaults.
  -h, --help               显示帮助 / Show help.

所需权限：订阅级 Owner，或 Contributor + User Access Administrator / RBAC Administrator
（Bicep 会在订阅级给托管身份分配 Reader，用于读取 Models API）。
Required permissions: subscription-level Owner, or Contributor + User Access Administrator / RBAC Administrator.
Bicep assigns subscription-level Reader to the managed identity to access the Models API.
迁移已有 Reader 分配可设置 AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME 为其 GUID，
仅可复用属于同一托管身份、同一订阅和 Reader 角色的分配；默认留空。
To reuse an existing Reader assignment, set AZURE_SUBSCRIPTION_READER_ASSIGNMENT_NAME to its GUID.
Only reuse an assignment for the same managed identity, subscription, and Reader role; leave empty by default.
EOF
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[警告 / Warning]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[错误 / Error]\033[0m %s\n' "$*" >&2; exit 1; }

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -e|--env-name)     ENV_NAME="${2:?}"; shift 2 ;;
      -l|--location)     LOCATION="${2:?}"; shift 2 ;;
      -s|--subscription) SUBSCRIPTION="${2:?}"; shift 2 ;;
      -g|--resource-group) RESOURCE_GROUP_NAME="${2:?}"; shift 2 ;;
      --resource-prefix) RESOURCE_PREFIX="${2:?}"; shift 2 ;;
      -r|--ref)          REF="${2:?}"; shift 2 ;;
      --what-if)         WHAT_IF=true; shift ;;
      --skip-infra)      SKIP_INFRA=true; shift ;;
      --skip-code)       SKIP_CODE=true; shift ;;
      --enable-alerts)   ENABLE_ALERTS=true; shift ;;
      --schedule)        COLLECT_SCHEDULE="${2:?}"; SCHEDULE_SPECIFIED=true; shift 2 ;;
      -y|--yes)          ASSUME_YES=true; shift ;;
      -h|--help)         usage; exit 0 ;;
      *) usage >&2; die "未知参数 / Unknown argument: $1" ;;
    esac
  done
}

choose_value() {
  local variable="$1" label="$2" default="$3" reply choice_index
  shift 3
  local choices=("$@")
  for choice_index in "${!choices[@]}"; do
    printf '  %d) %s\n' "$((choice_index + 1))" "${choices[$choice_index]}"
  done
  while true; do
    read -r -p "${label} [${default}]（编号或名称，回车保留） / number or name; Enter to keep: " reply </dev/tty \
      || die "无法读取部署选项 / Unable to read deployment options"
    if [[ ${#choices[@]} -gt 0 && "$reply" =~ ^[0-9]+$ ]]; then
      if [[ ${#reply} -le 6 ]] && (( 10#$reply >= 1 && 10#$reply <= ${#choices[@]} )); then
        reply="${choices[$((10#$reply - 1))]}"
      else
        warn "请选择有效编号或输入名称 / Select a valid number or enter a name"
        continue
      fi
    fi
    printf -v "$variable" '%s' "${reply:-$default}"
    return
  done
}

select_subscription() {
  local accounts rows current selected default subscription_id label choice_index
  local subscription_ids=() subscription_choices=()
  accounts="$(az account list --query "[?state=='Enabled'].{name:name,id:id}" -o json)" \
    || die "无法获取订阅列表 / Unable to list subscriptions"
  rows="$(python3 -c 'import json, sys
for account in json.load(sys.stdin):
    print(account["id"] + "\t" + account["name"] + " (" + account["id"] + ")")' <<<"$accounts")" \
    || die "无法解析订阅列表 / Unable to parse the subscription list"
  if [[ -n "$rows" ]]; then
    while IFS=$'\t' read -r subscription_id label; do
      subscription_ids+=("$subscription_id")
      subscription_choices+=("$label")
    done <<<"$rows"
  fi
  [[ ${#subscription_ids[@]} -gt 0 ]] || die "没有可用订阅，请检查 Azure 登录与订阅权限 / No enabled subscriptions; check Azure login and subscription permissions"
  if [[ ${#subscription_ids[@]} -eq 1 ]]; then
    SUBSCRIPTION="${subscription_ids[0]}"
    log "使用唯一可用订阅 / Using the only enabled subscription: ${subscription_choices[0]}"
  else
    current="$(az account show --query id -o tsv)"
    default="${subscription_choices[0]}"
    for choice_index in "${!subscription_ids[@]}"; do
      if [[ "${subscription_ids[$choice_index]}" == "$current" ]]; then
        default="${subscription_choices[$choice_index]}"
      fi
    done
    log "选择订阅（回车保留当前订阅，可输入编号、名称或 ID） / Select a subscription (Enter keeps the current one; enter a number, name, or ID)"
    choose_value selected "订阅 / Subscription" "$default" "${subscription_choices[@]}"
    SUBSCRIPTION="$selected"
    for choice_index in "${!subscription_choices[@]}"; do
      if [[ "${subscription_choices[$choice_index]}" == "$selected" ]]; then
        SUBSCRIPTION="${subscription_ids[$choice_index]}"
        break
      fi
    done
  fi
  az account set --subscription "$SUBSCRIPTION" || die "无法选择订阅 / Unable to select subscription: ${SUBSCRIPTION}"
}

deployment_options() {
  local regions groups
  local region_choices=() group_choices=()
  if ! { : </dev/tty; } 2>/dev/null; then
    die "无法交互选择部署选项，请加 -y / Cannot run the deployment wizard without a terminal; add -y"
  fi
  if [[ -z "$SUBSCRIPTION" ]]; then select_subscription; fi
  if ! $SKIP_INFRA; then
    log "1/4 选择区域 / Select a region"
    regions="$(az functionapp list-flexconsumption-locations --query '[].name' -o tsv)" \
      || die "无法获取 Flex Consumption 区域列表 / Unable to list Flex Consumption regions"
    if [[ -n "$regions" ]]; then mapfile -t region_choices <<<"$regions"; fi
    choose_value LOCATION "区域 / Region" "$LOCATION" "${region_choices[@]}"
    log "2/4 选择资源组（也可输入新资源组名称） / Select a resource group (or enter a new name)"
    groups="$(az group list --query '[].name' -o tsv)" || die "无法获取资源组列表 / Unable to list resource groups"
    if [[ -n "$groups" ]]; then mapfile -t group_choices <<<"$groups"; fi
    choose_value RESOURCE_GROUP_NAME "资源组 / Resource group" "$RESOURCE_GROUP_NAME" "${group_choices[@]}"
    log "3/4 输入资源名称前缀 / Enter a resource name prefix"
    choose_value RESOURCE_PREFIX "资源名称前缀 / Resource name prefix" "${RESOURCE_PREFIX:-$ENV_NAME}"
    log "4/4 确认 UTC 触发时间（六字段：秒 分 时 日 月 星期；0 0 0 * * * = 北京时间每天 08:00） / Confirm the UTC schedule (second minute hour day month weekday; 0 0 0 * * * = daily at 08:00 Asia/Shanghai)"
    choose_value COLLECT_SCHEDULE "触发时间 (UTC) / Schedule (UTC)" "$COLLECT_SCHEDULE"
  fi
}

# 设置 SOURCE_DIR：脚本旁有 infra/ 且未指定 --ref 时用本地 checkout，否则下载 GitHub 源码包。
resolve_source() {
  local script="${BASH_SOURCE[0]:-}" dir
  if [[ -z "$REF" && -f "$script" ]]; then
    dir="$(cd "$(dirname "$script")" && pwd)"
    if [[ -f "${dir}/infra/main.bicep" ]]; then
      SOURCE_DIR="$dir"
      log "源码: 本地 / Source: local ${SOURCE_DIR}"
      return
    fi
  fi
  REF="${REF:-main}"
  SOURCE_DIR="${WORK_DIR}/source"
  mkdir -p "$SOURCE_DIR"
  log "源码 / Source: github.com/${REPO} @ ${REF}（下载中 / downloading）"
  curl -fsSL "https://github.com/${REPO}/archive/${REF}.tar.gz" | tar -xz -C "$SOURCE_DIR" --strip-components=1 2>/dev/null \
    || die "下载源码失败，请确认 ${REF} 在 github.com/${REPO} 中存在 / Source download failed; check that ${REF} exists in github.com/${REPO}"
}

# 订阅级角色分配权限检查（仅告警：自定义角色也可能具备权限）
check_rbac_permission() {
  local oid roles
  oid="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  [[ -n "$oid" ]] || { warn "无法识别当前用户（服务主体？），跳过权限预检 / Unable to identify the current user (service principal?); skipping permission precheck"; return; }
  roles="$(az role assignment list --assignee "$oid" --scope "/subscriptions/${SUB_ID}" \
    --include-inherited --include-groups --query "[].roleDefinitionName" -o tsv 2>/dev/null || true)"
  if ! grep -qxE "Owner|User Access Administrator|Role Based Access Control Administrator" <<<"$roles"; then
    warn "当前账号在订阅上似乎没有 Owner / User Access Administrator / RBAC Administrator，订阅级 Reader 分配可能失败 / No matching subscription-level role found; assigning Reader may fail"
  fi
}

register_providers() {
  local ns state
  for ns in Microsoft.Web Microsoft.Storage Microsoft.Insights Microsoft.OperationalInsights \
            Microsoft.ManagedIdentity Microsoft.CognitiveServices; do
    state="$(az provider show -n "$ns" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
    if [[ "$state" != "Registered" ]]; then
      if $WHAT_IF; then
        warn "${ns} 尚未注册；预览模式不执行注册，正式部署时会注册 / ${ns} is not registered; preview skips registration, deployment will register it"
        continue
      fi
      log "注册资源提供程序 / Registering resource provider: ${ns} ..."
      az provider register -n "$ns" --wait -o none
    fi
  done
}

check_flex_location() {
  local ok
  ok="$(az functionapp list-flexconsumption-locations --query "[?name=='${LOCATION}'] | length(@)" -o tsv 2>/dev/null || echo unknown)"
  [[ "$ok" == "0" ]] && die "区域 ${LOCATION} 不支持 Flex Consumption（az functionapp list-flexconsumption-locations 查看可用区域） / Region ${LOCATION} does not support Flex Consumption; run az functionapp list-flexconsumption-locations for available regions"
  return 0
}

deploy_infra() {
  local group_location
  group_location="$(az group show -n "$RESOURCE_GROUP_NAME" --query location -o tsv 2>/dev/null || true)"
  local args=(--name "$DEPLOYMENT_NAME" --location "$LOCATION" --template-file "$BICEP_FILE"
              --parameters environmentName="$ENV_NAME" location="$LOCATION" enableCollectionAlerts="$ENABLE_ALERTS"
              resourceGroupName="$RESOURCE_GROUP_NAME" resourceNamePrefix="$RESOURCE_PREFIX"
              subscriptionReaderAssignmentName="$SUBSCRIPTION_READER_ASSIGNMENT_NAME"
              dailyCollectSchedule="$COLLECT_SCHEDULE"
              resourceGroupLocation="${group_location:-$LOCATION}")
  if $WHAT_IF; then
    log "预览基础设施变更 / Previewing infrastructure changes (what-if) ..."
    az deployment sub what-if "${args[@]}"
    return
  fi
  log "部署基础设施 (Bicep, 订阅级) / Deploying infrastructure (Bicep, subscription scope) ..."
  az deployment sub create "${args[@]}" -o none
}

read_outputs() {
  local outputs names app
  log "读取部署状态与输出 / Reading deployment state and outputs: ${DEPLOYMENT_NAME} ..."
  outputs="$(az deployment sub show --name "$DEPLOYMENT_NAME" \
    --query '{state:properties.provisioningState,outputs:properties.outputs}' \
    -o json)" || die "无法读取部署 ${DEPLOYMENT_NAME}，请检查所选订阅；首次部署不要带 --skip-infra / Unable to read deployment ${DEPLOYMENT_NAME}; check the selected subscription and omit --skip-infra for the first deployment"
  names="$(python3 -c '
import json, sys
try:
    deployment = json.load(sys.stdin)
except ValueError:
  sys.exit("部署输出不是有效 JSON / Deployment output is not valid JSON")
if not isinstance(deployment, dict):
  sys.exit("部署输出不是 JSON 对象 / Deployment output is not a JSON object")
state = deployment.get("state")
if state != "Succeeded":
  sys.exit(f"部署状态为 {state!r}，不是 Succeeded / Deployment state is {state!r}, not Succeeded")
outputs = deployment.get("outputs")
if not isinstance(outputs, dict):
  sys.exit("部署 outputs 缺失或不是 JSON 对象 / Deployment outputs are missing or not a JSON object")
names = []
for key in ("AZURE_RESOURCE_GROUP", "AZURE_FUNCTION_APP_NAME"):
    matches = [output for name, output in outputs.items() if name.casefold() == key.casefold()]
    if not matches:
        fields = json.dumps(sorted(outputs), ensure_ascii=False)
        sys.exit(f"部署输出缺少 {key}；ARM 实际输出字段: {fields} / Missing deployment output {key}; actual ARM output fields: {fields}")
    if len(matches) != 1:
        sys.exit(f"部署输出 {key} 存在大小写冲突 / Deployment output {key} has conflicting key casing")
    output = matches[0]
    value = output.get("value") if isinstance(output, dict) else None
    if (not isinstance(value, str) or not value or value in ("None", "null")
            or any(character.isspace() for character in value)):
        fields = json.dumps(sorted(output), ensure_ascii=False) if isinstance(output, dict) else "[]"
        sys.exit(f"部署输出 {key} 不是有效资源名称；项目字段: {fields}，value 类型: {type(value).__name__} / Deployment output {key} is not a valid resource name; entry fields: {fields}, value type: {type(value).__name__}")
    names.append(value)
print("\t".join(names))
' <<<"$outputs")" || die "部署输出无效，已停止发布；请检查部署 ${DEPLOYMENT_NAME} 的状态与 Outputs / Invalid deployment outputs; publishing stopped. Check deployment ${DEPLOYMENT_NAME} state and Outputs"
  read -r RESOURCE_GROUP FUNCTION_APP_NAME <<<"$names"
  log "部署输出 / Deployment outputs: 资源组 / Resource group=${RESOURCE_GROUP}   Function App=${FUNCTION_APP_NAME}"
  app="$(az functionapp show -g "$RESOURCE_GROUP" -n "$FUNCTION_APP_NAME" -o json)" \
    || die "无法读取 Function App ${FUNCTION_APP_NAME} 的主机名 / Unable to read the hostname of Function App ${FUNCTION_APP_NAME}"
  FUNCTION_APP_HOST="$(python3 -c '
import json, re, sys
try:
    app = json.load(sys.stdin)
except ValueError:
  sys.exit("Function App 主机名响应不是有效 JSON / Function App hostname response is not valid JSON")
if not isinstance(app, dict):
  sys.exit("Function App 主机名响应不是 JSON 对象 / Function App hostname response is not a JSON object")
containers = {"topLevel": app}
if isinstance(app.get("properties"), dict):
  containers["properties"] = app["properties"]
matches = [value for container in containers.values() for key, value in container.items()
       if key.casefold() == "defaulthostname"]
if not matches:
    fields = {path: sorted(container) for path, container in containers.items()}
    sys.exit("Function App 缺少 defaultHostName 主机名字段 / Function App is missing the defaultHostName field；实际字段 / Actual fields: "
             + json.dumps(fields, ensure_ascii=False))
if len(matches) != 1:
  sys.exit("Function App 主机名字段 defaultHostName 存在跨层重复或大小写冲突 / Function App defaultHostName has cross-level duplicates or conflicting key casing")
host = matches[0]
if not isinstance(host, str) or not host or len(host) > 253:
    sys.exit("Function App 主机名不是有效 DNS 名称 / Function App hostname is not a valid DNS name")
labels = host.split(".")
if len(labels) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels):
    sys.exit("Function App 主机名不是有效 DNS 名称 / Function App hostname is not a valid DNS name")
print(host)
' <<<"$app")" || die "Function App 主机名无效，已停止发布与验收 / Invalid Function App hostname; publishing and verification stopped"
  log "Function App 主机名 / Hostname: ${FUNCTION_APP_HOST}"
}

package_src() {
  local zip_path="$1"
  python3 - "$SRC_DIR" "$zip_path" <<'PY'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
skip_dirs = {".venv", "venv", "__pycache__", ".python_packages", ".pytest_cache", ".vscode", "tests"}
skip_files = {"local.settings.json"}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            if f in skip_files or f.endswith((".pyc", ".zip")):
                continue
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, src))
PY
}

deploy_code() {
  local zip_path="${WORK_DIR}/app.zip"
  log "打包 / Packaging src/ ..."
  package_src "$zip_path"
  log "发布代码到 ${FUNCTION_APP_NAME}（远程构建，约 1-3 分钟） / Publishing code to ${FUNCTION_APP_NAME} (remote build, about 1-3 minutes) ..."
  az functionapp deployment source config-zip -g "$RESOURCE_GROUP" -n "$FUNCTION_APP_NAME" \
    --src "$zip_path" --build-remote true -o none
}

verify() {
  local names short_names code expected path
  local required=(daily_collect rest_changes_today rest_changes_upcoming rest_changes_past rest_models rest_model rest_prices
                  mcp_get_today_changes mcp_get_upcoming_changes mcp_get_past_changes mcp_search_models mcp_get_model mcp_get_model_prices)
  local missing=()
  log "等待函数注册 / Waiting for function registration ..."
  for _ in $(seq 1 24); do
    names="$(az functionapp function list -g "$RESOURCE_GROUP" -n "$FUNCTION_APP_NAME" --query "[].name" -o tsv 2>/dev/null || true)"
    short_names="$(sed 's|.*/||' <<<"$names")"
    missing=()
    for expected in "${required[@]}"; do
      if ! grep -Fxq "$expected" <<<"$short_names"; then missing+=("$expected"); fi
    done
    [[ ${#missing[@]} -eq 0 ]] && break
    sleep 5
  done
  [[ ${#missing[@]} -eq 0 ]] || die "函数注册不完整，缺少 / Function registration incomplete; missing: ${missing[*]}"
  sed 's/^/    /' <<<"$names"
  for path in /api/changes/today /runtime/webhooks/mcp; do
    local args=(-sS --connect-timeout 10 --max-time 30 -o /dev/null -w '%{http_code}')
    if [[ "$path" == /runtime/webhooks/mcp ]]; then
      args+=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream'
             --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1.0"}}}')
    fi
    code="$(curl "${args[@]}" "https://${FUNCTION_APP_HOST}${path}" || true)"
    [[ "$code" == "401" ]] || die "无 key 访问 ${path} 返回 ${code}（期望 401），部署验收失败 / Unauthenticated request to ${path} returned ${code} (expected 401); deployment verification failed"
    log "鉴权检查通过：${path} 无 key 访问返回 401 / Authentication check passed: ${path} returns 401 without a key"
  done
}

print_summary() {
  cat <<EOF

部署完成 / Deployment complete
  资源组 / Resource group : ${RESOURCE_GROUP}
  REST   : https://${FUNCTION_APP_HOST}/api/changes/today
  MCP    : https://${FUNCTION_APP_HOST}/runtime/webhooks/mcp

获取/创建密钥（脚本不会打印密钥值） / Get/create keys (the script does not print key values):
  # 为某个 REST 调用方创建独立 key / Create a separate key for each REST client
  az functionapp keys set -g ${RESOURCE_GROUP} -n ${FUNCTION_APP_NAME} --key-type functionKeys --key-name <client>
  # MCP 客户端使用的系统密钥 / System key for MCP clients
  az functionapp keys list -g ${RESOURCE_GROUP} -n ${FUNCTION_APP_NAME} --query systemKeys.mcp_extension -o tsv

首次采集会在 Timer 的下一个计划时间自动运行；时间按 UTC 定义。
The first collection runs automatically at the next scheduled Timer invocation; the schedule uses UTC.
EOF
}

confirm() {
  local reply
  if ! { : </dev/tty; } 2>/dev/null; then
    die "无法交互确认，请加 -y / Cannot confirm without a terminal; add -y"
  fi
  # curl | bash 时 stdin 是脚本本身，必须从终端读取
  read -r -p "确认部署? / Confirm deployment? [y/N] " reply </dev/tty
  [[ "$reply" =~ ^[Yy]$ ]] || die "已取消 / Cancelled"
}

main() {
  parse_args "$@"
  ENV_NAME="${ENV_NAME:-foundry-notify}"
  RESOURCE_GROUP_NAME="${RESOURCE_GROUP_NAME:-rg-${ENV_NAME}}"
  [[ "$ENV_NAME" =~ ^[a-z0-9][a-z0-9-]{1,14}[a-z0-9]$ ]] || die "env-name 需为 3-16 位小写字母/数字/- / env-name must be 3-16 lowercase letters/digits/hyphens"
  if $SKIP_INFRA && $SKIP_CODE; then die "--skip-infra 与 --skip-code 不能同时使用 / --skip-infra and --skip-code cannot be used together"; fi
  if $SKIP_INFRA && $WHAT_IF; then die "--what-if 只用于预览基础设施，不能与 --skip-infra 同时使用 / --what-if previews infrastructure and cannot be combined with --skip-infra"; fi
  if $SKIP_INFRA && $ENABLE_ALERTS; then die "--enable-alerts 需要部署基础设施，不能与 --skip-infra 同时使用 / --enable-alerts requires infrastructure deployment and cannot be combined with --skip-infra"; fi
  if $SKIP_INFRA && $SCHEDULE_SPECIFIED; then die "--schedule / DAILY_COLLECT_SCHEDULE 需要部署基础设施，不能与 --skip-infra 同时使用 / --schedule / DAILY_COLLECT_SCHEDULE requires infrastructure deployment and cannot be combined with --skip-infra"; fi
  command -v az >/dev/null      || die "未找到 az CLI（Cloud Shell 已内置） / az CLI not found (included in Cloud Shell)"
  command -v python3 >/dev/null || die "未找到 python3（用于打包 zip） / python3 not found (required to create the deployment zip)"
  command -v curl >/dev/null    || die "未找到 curl / curl not found"
  az account show >/dev/null 2>&1 || die "未登录，请先运行 az login（Cloud Shell 已自动登录） / Not signed in; run az login (Cloud Shell signs in automatically)"

  if [[ -n "$SUBSCRIPTION" ]]; then az account set --subscription "$SUBSCRIPTION"; fi
  if ! $ASSUME_YES && ! $WHAT_IF; then deployment_options; fi
  local schedule_fields=()
  read -r -a schedule_fields <<<"$COLLECT_SCHEDULE"
  [[ ${#schedule_fields[@]} -eq 6 && "$COLLECT_SCHEDULE" != *$'\n'* && "$COLLECT_SCHEDULE" != *$'\r'* ]] \
    || die "schedule 需为 UTC 六字段 NCRONTAB：秒 分 时 日 月 星期 / schedule must be a six-field UTC NCRONTAB: second minute hour day month weekday"
  [[ "$LOCATION" =~ ^[a-z0-9-]+$ ]] || die "region 需为小写字母/数字/- / region must contain only lowercase letters/digits/hyphens"
  [[ "$RESOURCE_GROUP_NAME" =~ ^[a-zA-Z0-9_.()-]{1,90}$ && "$RESOURCE_GROUP_NAME" != *. ]] \
    || die "资源组名需为 1-90 位字母/数字/下划线/括号/连字符/点，不能以点结尾 / Resource group name must be 1-90 letters/digits/underscores/parentheses/hyphens/periods and must not end with a period"
  [[ -z "$RESOURCE_PREFIX" || "$RESOURCE_PREFIX" =~ ^[a-z0-9][a-z0-9-]{1,14}[a-z0-9]$ ]] \
    || die "资源名称前缀需为 3-16 位小写字母/数字/-，首尾为字母或数字 / Resource name prefix must be 3-16 lowercase letters/digits/hyphens, starting and ending with a letter or digit"

  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
  resolve_source
  BICEP_FILE="${SOURCE_DIR}/infra/main.bicep"
  SRC_DIR="${SOURCE_DIR}/src"
  [[ -f "$BICEP_FILE" ]] || die "源码中未找到 infra/main.bicep / infra/main.bicep not found in the source"
  $SKIP_CODE || [[ -f "${SRC_DIR}/function_app.py" ]] || die "源码中未找到 src/function_app.py / src/function_app.py not found in the source"

  SUB_ID="$(az account show --query id -o tsv)"
  SUB_NAME="$(az account show --query name -o tsv)"
  DEPLOYMENT_NAME="foundry-notify-${ENV_NAME}"
  log "订阅 / Subscription: ${SUB_NAME} (${SUB_ID})"
  log "环境 / Environment: ${ENV_NAME}   区域 / Region: ${LOCATION}   部署名 / Deployment: ${DEPLOYMENT_NAME}"
  if ! $SKIP_INFRA; then
    log "资源组 / Resource group: ${RESOURCE_GROUP_NAME}   资源名称前缀 / Resource name prefix: ${RESOURCE_PREFIX:-默认命名 / legacy naming}"
    log "采集计划 (UTC) / Collection schedule (UTC): ${COLLECT_SCHEDULE}"
  fi

  if ! $ASSUME_YES && ! $WHAT_IF; then confirm; fi

  if ! $SKIP_INFRA; then
    check_rbac_permission
    register_providers
    check_flex_location
    deploy_infra
    if $WHAT_IF; then return; fi
  fi

  read_outputs
  if ! $SKIP_CODE; then
    deploy_code
    verify
  fi
  print_summary
}

# 整个脚本下载并解析完毕后才执行，curl | bash 中途断网不会跑半截脚本
main "$@"
