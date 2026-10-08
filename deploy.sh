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
REF=""
WHAT_IF=false
SKIP_INFRA=false
SKIP_CODE=false
ASSUME_YES=false
ENABLE_ALERTS=false

usage() {
  cat <<EOF
用法: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/deploy.sh | bash -s -- [选项]
  bash deploy.sh [选项]

默认先选择订阅（多个可用订阅时），再选择区域、资源组、资源名称前缀；回车保留默认。
-s / AZURE_SUBSCRIPTION_ID 指定订阅时不再询问；-y / --what-if 跳过向导。

  -e, --env-name NAME       环境名（默认 foundry-notify，3-16 位小写字母/数字/-），也可用 AZURE_ENV_NAME
  -l, --location REGION     部署区域（默认 eastus2，需支持 Flex Consumption），也可用 AZURE_LOCATION
  -g, --resource-group NAME 资源组（默认 rg-<env-name>），也可用 AZURE_RESOURCE_GROUP
  --resource-prefix NAME 资源名称前缀（向导默认环境名，3-16 位小写字母/数字/-）
                            也可用 AZURE_RESOURCE_NAME_PREFIX；非交互未指定时保留旧命名
  -s, --subscription ID     目标订阅（默认当前 az 订阅），也可用 AZURE_SUBSCRIPTION_ID
  -r, --ref REF             从 github.com/${REPO} 下载指定分支/标签/commit 的源码
                            （默认 main；在仓库 checkout 中运行且未指定时使用本地文件）
      --what-if             仅预览基础设施变更，不做任何修改
      --skip-infra          跳过 Bicep，仅发布代码（需已部署过同名环境）
      --skip-code           仅部署基础设施，不发布代码
      --enable-alerts       启用采集失败/超过 32 小时未完成告警（建议首次采集成功后启用；可能产生 Monitor 费用）
  -y, --yes                 跳过向导及确认，使用参数/环境变量/默认值
  -h, --help                显示帮助

所需权限：订阅级 Owner，或 Contributor + User Access Administrator / RBAC Administrator
（Bicep 会在订阅级给托管身份分配 Reader，用于读取 Models API）。
EOF
}

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

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
      -y|--yes)          ASSUME_YES=true; shift ;;
      -h|--help)         usage; exit 0 ;;
      *) usage >&2; die "未知参数: $1" ;;
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
    read -r -p "${label} [${default}]（编号或名称，回车保留）: " reply </dev/tty \
      || die "无法读取部署选项"
    if [[ ${#choices[@]} -gt 0 && "$reply" =~ ^[0-9]+$ ]]; then
      if [[ ${#reply} -le 6 ]] && (( 10#$reply >= 1 && 10#$reply <= ${#choices[@]} )); then
        reply="${choices[$((10#$reply - 1))]}"
      else
        warn "请选择有效编号或输入名称"
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
    || die "无法获取订阅列表"
  rows="$(python3 -c 'import json, sys
for account in json.load(sys.stdin):
    print(account["id"] + "\t" + account["name"] + " (" + account["id"] + ")")' <<<"$accounts")" \
    || die "无法解析订阅列表"
  if [[ -n "$rows" ]]; then
    while IFS=$'\t' read -r subscription_id label; do
      subscription_ids+=("$subscription_id")
      subscription_choices+=("$label")
    done <<<"$rows"
  fi
  [[ ${#subscription_ids[@]} -gt 0 ]] || die "没有可用订阅，请检查 Azure 登录与订阅权限"
  if [[ ${#subscription_ids[@]} -eq 1 ]]; then
    SUBSCRIPTION="${subscription_ids[0]}"
    log "使用唯一可用订阅: ${subscription_choices[0]}"
  else
    current="$(az account show --query id -o tsv)"
    default="${subscription_choices[0]}"
    for choice_index in "${!subscription_ids[@]}"; do
      if [[ "${subscription_ids[$choice_index]}" == "$current" ]]; then
        default="${subscription_choices[$choice_index]}"
      fi
    done
    log "选择订阅（回车保留当前订阅，可输入编号、名称或 ID）"
    choose_value selected "订阅" "$default" "${subscription_choices[@]}"
    SUBSCRIPTION="$selected"
    for choice_index in "${!subscription_choices[@]}"; do
      if [[ "${subscription_choices[$choice_index]}" == "$selected" ]]; then
        SUBSCRIPTION="${subscription_ids[$choice_index]}"
        break
      fi
    done
  fi
  az account set --subscription "$SUBSCRIPTION" || die "无法选择订阅 ${SUBSCRIPTION}"
}

deployment_options() {
  local regions groups
  local region_choices=() group_choices=()
  if ! { : </dev/tty; } 2>/dev/null; then
    die "无法交互选择部署选项，请加 -y"
  fi
  if [[ -z "$SUBSCRIPTION" ]]; then select_subscription; fi
  if ! $SKIP_INFRA; then
    log "1/3 选择 region"
    regions="$(az functionapp list-flexconsumption-locations --query '[].name' -o tsv)" \
      || die "无法获取 Flex Consumption 区域列表"
    if [[ -n "$regions" ]]; then mapfile -t region_choices <<<"$regions"; fi
    choose_value LOCATION "区域" "$LOCATION" "${region_choices[@]}"
    log "2/3 选择资源组（也可输入新资源组名称）"
    groups="$(az group list --query '[].name' -o tsv)" || die "无法获取资源组列表"
    if [[ -n "$groups" ]]; then mapfile -t group_choices <<<"$groups"; fi
    choose_value RESOURCE_GROUP_NAME "资源组" "$RESOURCE_GROUP_NAME" "${group_choices[@]}"
    log "3/3 输入资源名称前缀"
    choose_value RESOURCE_PREFIX "资源名称前缀" "${RESOURCE_PREFIX:-$ENV_NAME}"
  fi
}

# 设置 SOURCE_DIR：脚本旁有 infra/ 且未指定 --ref 时用本地 checkout，否则下载 GitHub 源码包。
resolve_source() {
  local script="${BASH_SOURCE[0]:-}" dir
  if [[ -z "$REF" && -f "$script" ]]; then
    dir="$(cd "$(dirname "$script")" && pwd)"
    if [[ -f "${dir}/infra/main.bicep" ]]; then
      SOURCE_DIR="$dir"
      log "源码: 本地 ${SOURCE_DIR}"
      return
    fi
  fi
  REF="${REF:-main}"
  SOURCE_DIR="${WORK_DIR}/source"
  mkdir -p "$SOURCE_DIR"
  log "源码: github.com/${REPO} @ ${REF}（下载中）"
  curl -fsSL "https://github.com/${REPO}/archive/${REF}.tar.gz" | tar -xz -C "$SOURCE_DIR" --strip-components=1 2>/dev/null \
    || die "下载源码失败，请确认 ${REF} 在 github.com/${REPO} 中存在"
}

# 订阅级角色分配权限检查（仅告警：自定义角色也可能具备权限）
check_rbac_permission() {
  local oid roles
  oid="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
  [[ -n "$oid" ]] || { warn "无法识别当前用户（服务主体？），跳过权限预检"; return; }
  roles="$(az role assignment list --assignee "$oid" --scope "/subscriptions/${SUB_ID}" \
    --include-inherited --include-groups --query "[].roleDefinitionName" -o tsv 2>/dev/null || true)"
  if ! grep -qxE "Owner|User Access Administrator|Role Based Access Control Administrator" <<<"$roles"; then
    warn "当前账号在订阅上似乎没有 Owner / User Access Administrator / RBAC Administrator，订阅级 Reader 分配可能失败"
  fi
}

register_providers() {
  local ns state
  for ns in Microsoft.Web Microsoft.Storage Microsoft.Insights Microsoft.OperationalInsights \
            Microsoft.ManagedIdentity Microsoft.CognitiveServices; do
    state="$(az provider show -n "$ns" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
    if [[ "$state" != "Registered" ]]; then
      if $WHAT_IF; then
        warn "${ns} 尚未注册；预览模式不执行注册，正式部署时会注册"
        continue
      fi
      log "注册资源提供程序 ${ns} ..."
      az provider register -n "$ns" --wait -o none
    fi
  done
}

check_flex_location() {
  local ok
  ok="$(az functionapp list-flexconsumption-locations --query "[?name=='${LOCATION}'] | length(@)" -o tsv 2>/dev/null || echo unknown)"
  [[ "$ok" == "0" ]] && die "区域 ${LOCATION} 不支持 Flex Consumption（az functionapp list-flexconsumption-locations 查看可用区域）"
  return 0
}

deploy_infra() {
  local group_location
  group_location="$(az group show -n "$RESOURCE_GROUP_NAME" --query location -o tsv 2>/dev/null || true)"
  local args=(--name "$DEPLOYMENT_NAME" --location "$LOCATION" --template-file "$BICEP_FILE"
              --parameters environmentName="$ENV_NAME" location="$LOCATION" enableCollectionAlerts="$ENABLE_ALERTS"
              resourceGroupName="$RESOURCE_GROUP_NAME" resourceNamePrefix="$RESOURCE_PREFIX"
              resourceGroupLocation="${group_location:-$LOCATION}")
  if $WHAT_IF; then
    log "预览基础设施变更 (what-if) ..."
    az deployment sub what-if "${args[@]}"
    return
  fi
  log "部署基础设施 (Bicep, 订阅级) ..."
  az deployment sub create "${args[@]}" -o none
}

read_outputs() {
  local outputs
  outputs="$(az deployment sub show --name "$DEPLOYMENT_NAME" \
    --query "[properties.outputs.AZURE_RESOURCE_GROUP.value, properties.outputs.AZURE_FUNCTION_APP_NAME.value]" \
    -o tsv 2>/dev/null)" || die "找不到部署 ${DEPLOYMENT_NAME} 的输出，请先不带 --skip-infra 运行一次"
  read -r RESOURCE_GROUP FUNCTION_APP_NAME <<<"$(tr '\n' ' ' <<<"$outputs")"
  [[ -n "${RESOURCE_GROUP:-}" && -n "${FUNCTION_APP_NAME:-}" ]] || die "部署输出缺少 AZURE_RESOURCE_GROUP / AZURE_FUNCTION_APP_NAME"
  FUNCTION_APP_HOST="$(az functionapp show -g "$RESOURCE_GROUP" -n "$FUNCTION_APP_NAME" --query defaultHostName -o tsv)"
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
  log "打包 src/ ..."
  package_src "$zip_path"
  log "发布代码到 ${FUNCTION_APP_NAME}（远程构建，约 1-3 分钟）..."
  az functionapp deployment source config-zip -g "$RESOURCE_GROUP" -n "$FUNCTION_APP_NAME" \
    --src "$zip_path" --build-remote true -o none
}

verify() {
  local names short_names code expected path
  local required=(daily_collect rest_changes_today rest_changes_upcoming rest_changes_past rest_models rest_model rest_prices
                  mcp_get_today_changes mcp_get_upcoming_changes mcp_get_past_changes mcp_search_models mcp_get_model mcp_get_model_prices)
  local missing=()
  log "等待函数注册 ..."
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
  [[ ${#missing[@]} -eq 0 ]] || die "函数注册不完整，缺少: ${missing[*]}"
  sed 's/^/    /' <<<"$names"
  for path in /api/changes/today /runtime/webhooks/mcp; do
    local args=(-sS --connect-timeout 10 --max-time 30 -o /dev/null -w '%{http_code}')
    if [[ "$path" == /runtime/webhooks/mcp ]]; then
      args+=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream'
             --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"deploy-check","version":"1.0"}}}')
    fi
    code="$(curl "${args[@]}" "https://${FUNCTION_APP_HOST}${path}" || true)"
    [[ "$code" == "401" ]] || die "无 key 访问 ${path} 返回 ${code}（期望 401），部署验收失败"
    log "鉴权检查通过：${path} 无 key 访问返回 401"
  done
}

print_summary() {
  cat <<EOF

部署完成 ✅
  资源组 : ${RESOURCE_GROUP}
  REST   : https://${FUNCTION_APP_HOST}/api/changes/today
  MCP    : https://${FUNCTION_APP_HOST}/runtime/webhooks/mcp

获取/创建密钥（脚本不会打印密钥值）：
  # 为某个 REST 调用方创建独立 key
  az functionapp keys set -g ${RESOURCE_GROUP} -n ${FUNCTION_APP_NAME} --key-type functionKeys --key-name <client>
  # MCP 客户端使用的系统密钥
  az functionapp keys list -g ${RESOURCE_GROUP} -n ${FUNCTION_APP_NAME} --query systemKeys.mcp_extension -o tsv

首次采集会在下一个 UTC 00:00（北京时间 08:00）自动运行。
EOF
}

confirm() {
  local reply
  if ! { : </dev/tty; } 2>/dev/null; then
    die "无法交互确认，请加 -y"
  fi
  # curl | bash 时 stdin 是脚本本身，必须从终端读取
  read -r -p "确认部署? [y/N] " reply </dev/tty
  [[ "$reply" =~ ^[Yy]$ ]] || die "已取消"
}

main() {
  parse_args "$@"
  ENV_NAME="${ENV_NAME:-foundry-notify}"
  RESOURCE_GROUP_NAME="${RESOURCE_GROUP_NAME:-rg-${ENV_NAME}}"
  [[ "$ENV_NAME" =~ ^[a-z0-9][a-z0-9-]{1,14}[a-z0-9]$ ]] || die "env-name 需为 3-16 位小写字母/数字/-"
  if $SKIP_INFRA && $SKIP_CODE; then die "--skip-infra 与 --skip-code 不能同时使用"; fi
  if $SKIP_INFRA && $WHAT_IF; then die "--what-if 只用于预览基础设施，不能与 --skip-infra 同时使用"; fi
  if $SKIP_INFRA && $ENABLE_ALERTS; then die "--enable-alerts 需要部署基础设施，不能与 --skip-infra 同时使用"; fi

  command -v az >/dev/null      || die "未找到 az CLI（Cloud Shell 已内置）"
  command -v python3 >/dev/null || die "未找到 python3（用于打包 zip）"
  command -v curl >/dev/null    || die "未找到 curl"
  az account show >/dev/null 2>&1 || die "未登录，请先运行 az login（Cloud Shell 已自动登录）"

  if [[ -n "$SUBSCRIPTION" ]]; then az account set --subscription "$SUBSCRIPTION"; fi
  if ! $ASSUME_YES && ! $WHAT_IF; then deployment_options; fi
  [[ "$LOCATION" =~ ^[a-z0-9-]+$ ]] || die "region 需为小写字母/数字/-"
  [[ "$RESOURCE_GROUP_NAME" =~ ^[a-zA-Z0-9_.()-]{1,90}$ && "$RESOURCE_GROUP_NAME" != *. ]] \
    || die "资源组名需为 1-90 位字母/数字/下划线/括号/连字符/点，不能以点结尾"
  [[ -z "$RESOURCE_PREFIX" || "$RESOURCE_PREFIX" =~ ^[a-z0-9][a-z0-9-]{1,14}[a-z0-9]$ ]] \
    || die "资源名称前缀需为 3-16 位小写字母/数字/-，首尾为字母或数字"

  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
  resolve_source
  BICEP_FILE="${SOURCE_DIR}/infra/main.bicep"
  SRC_DIR="${SOURCE_DIR}/src"
  [[ -f "$BICEP_FILE" ]] || die "源码中未找到 infra/main.bicep"
  $SKIP_CODE || [[ -f "${SRC_DIR}/function_app.py" ]] || die "源码中未找到 src/function_app.py"

  SUB_ID="$(az account show --query id -o tsv)"
  SUB_NAME="$(az account show --query name -o tsv)"
  DEPLOYMENT_NAME="foundry-notify-${ENV_NAME}"
  log "订阅: ${SUB_NAME} (${SUB_ID})"
  log "环境: ${ENV_NAME}   区域: ${LOCATION}   部署名: ${DEPLOYMENT_NAME}"
  if ! $SKIP_INFRA; then
    log "资源组: ${RESOURCE_GROUP_NAME}   资源名称前缀: ${RESOURCE_PREFIX:-默认命名}"
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
