#!/usr/bin/env bash
# 不依赖 azd / func 的部署脚本，适用于 Azure Cloud Shell（仅需 az + python3 + curl）。
# 支持 curl | bash 一键运行：源码从 GitHub 下载；在仓库 checkout 中运行且未指定 --ref 时使用本地文件。
# 与 azd 共用 infra/main.bicep（订阅级部署），代码通过 Flex Consumption 的 zip 部署 + 远程构建发布。
set -euo pipefail

REPO="${DEPLOY_REPO:-pczhao1210/ms-foundry-notification}"
ENV_NAME="${AZURE_ENV_NAME:-}"
LOCATION="${AZURE_LOCATION:-eastus2}"
SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-}"
REF=""
WHAT_IF=false
SKIP_INFRA=false
SKIP_CODE=false
ASSUME_YES=false

usage() {
  cat <<EOF
用法: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/deploy.sh | bash -s -- -e <env-name> [选项]
      bash deploy.sh -e <env-name> [选项]

  -e, --env-name NAME       环境名（资源命名前缀，3-16 位小写字母/数字/-），也可用 AZURE_ENV_NAME
  -l, --location REGION     部署区域（默认 eastus2，需支持 Flex Consumption），也可用 AZURE_LOCATION
  -s, --subscription ID     目标订阅（默认当前 az 订阅），也可用 AZURE_SUBSCRIPTION_ID
  -r, --ref REF             从 github.com/${REPO} 下载指定分支/标签/commit 的源码
                            （默认 main；在仓库 checkout 中运行且未指定时使用本地文件）
      --what-if             仅预览基础设施变更，不做任何修改
      --skip-infra          跳过 Bicep，仅发布代码（需已部署过同名环境）
      --skip-code           仅部署基础设施，不发布代码
  -y, --yes                 不询问确认
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
      -r|--ref)          REF="${2:?}"; shift 2 ;;
      --what-if)         WHAT_IF=true; shift ;;
      --skip-infra)      SKIP_INFRA=true; shift ;;
      --skip-code)       SKIP_CODE=true; shift ;;
      -y|--yes)          ASSUME_YES=true; shift ;;
      -h|--help)         usage; exit 0 ;;
      *) usage >&2; die "未知参数: $1" ;;
    esac
  done
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
  local args=(--name "$DEPLOYMENT_NAME" --location "$LOCATION" --template-file "$BICEP_FILE"
              --parameters environmentName="$ENV_NAME" location="$LOCATION")
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
  local names code
  log "等待函数注册 ..."
  for _ in $(seq 1 24); do
    names="$(az functionapp function list -g "$RESOURCE_GROUP" -n "$FUNCTION_APP_NAME" --query "[].name" -o tsv 2>/dev/null || true)"
    [[ -n "$names" ]] && break
    sleep 5
  done
  if [[ -n "$names" ]]; then
    # shellcheck disable=SC2001
    sed 's/^/    /' <<<"$names"
  else
    warn "暂未列出函数，稍后在门户确认"
  fi
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://${FUNCTION_APP_HOST}/api/changes/today" || true)"
  if [[ "$code" == "401" ]]; then
    log "鉴权检查通过：无 key 访问返回 401"
  else
    warn "无 key 访问 /api/changes/today 返回 ${code}（期望 401）"
  fi
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
  [[ -n "$ENV_NAME" ]] || { usage >&2; die "缺少 --env-name"; }
  [[ "$ENV_NAME" =~ ^[a-z0-9][a-z0-9-]{1,14}[a-z0-9]$ ]] || die "env-name 需为 3-16 位小写字母/数字/-"
  if $SKIP_INFRA && $SKIP_CODE; then die "--skip-infra 与 --skip-code 不能同时使用"; fi
  if $SKIP_INFRA && $WHAT_IF; then die "--what-if 只用于预览基础设施，不能与 --skip-infra 同时使用"; fi

  command -v az >/dev/null      || die "未找到 az CLI（Cloud Shell 已内置）"
  command -v python3 >/dev/null || die "未找到 python3（用于打包 zip）"
  command -v curl >/dev/null    || die "未找到 curl"
  az account show >/dev/null 2>&1 || die "未登录，请先运行 az login（Cloud Shell 已自动登录）"

  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "$WORK_DIR"' EXIT
  resolve_source
  BICEP_FILE="${SOURCE_DIR}/infra/main.bicep"
  SRC_DIR="${SOURCE_DIR}/src"
  [[ -f "$BICEP_FILE" ]] || die "源码中未找到 infra/main.bicep"
  $SKIP_CODE || [[ -f "${SRC_DIR}/function_app.py" ]] || die "源码中未找到 src/function_app.py"

  if [[ -n "$SUBSCRIPTION" ]]; then az account set --subscription "$SUBSCRIPTION"; fi
  SUB_ID="$(az account show --query id -o tsv)"
  SUB_NAME="$(az account show --query name -o tsv)"
  DEPLOYMENT_NAME="foundry-notify-${ENV_NAME}"
  log "订阅: ${SUB_NAME} (${SUB_ID})"
  log "环境: ${ENV_NAME}   区域: ${LOCATION}   部署名: ${DEPLOYMENT_NAME}"

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
