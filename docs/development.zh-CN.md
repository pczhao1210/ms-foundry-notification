# 开发指南

[English](development.md) | **简体中文** | [项目简介](../README.zh-CN.md)

以下命令在仓库根目录运行，使用 Python 3.12。

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

> 状态：代码与 IaC 已完成并离线验证，尚未在真实环境部署验证。详见 [项目设计](plan.md)，开发约定见 [AGENTS.md](../AGENTS.md)。
