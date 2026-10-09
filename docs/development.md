# Development

**English** | [简体中文](development.zh-CN.md) | [Project Overview](../README.md)

Run the following commands from the repository root using Python 3.12.

## Local Development

```bash
python -m venv .venv && . .venv/bin/activate
python -m pip install -r src/requirements.txt -r requirements-dev.txt
python -m pytest -q          # Offline unit tests; no network/Azure access
```

Runtime, transitive, and test dependencies are pinned to versions verified on Linux/Python 3.12. When upgrading, resolve the complete dependency set again and run the tests. GitHub Actions runs dependency consistency checks, offline tests, deployment script syntax checks, and Bicep compilation on pushes and pull requests. It does not deploy automatically or require Azure keys.

Running Functions locally requires Node.js, `az login` (Azure CLI credentials for ARM collection), and `src/local.settings.json` (git-ignored; do not commit). Storage defaults to the local Azurite emulator:

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
export FOUNDRY_SUBSCRIPTION_ID="$(az account show --query id -o tsv)"  # Do not write to a file
func start
# In another terminal: trigger collection once, then query the API
curl -X POST -H "Content-Type: application/json" -d '{"input":""}' http://localhost:7071/admin/functions/daily_collect
curl "http://localhost:7071/api/changes/past?days=7"
```

- `STORAGE_USE_EMULATOR=true` uses Azurite's public development account and automatically creates containers and tables. To connect to Azure Storage, remove this setting and configure `STORAGE_BLOB_ENDPOINT` / `STORAGE_TABLE_ENDPOINT` from `azd env get-values`. Your local account needs Blob/Table Data Contributor roles; azd grants them using `AZURE_PRINCIPAL_ID`.
- `AZURE_TOKEN_CREDENTIALS=AzureCliCredential` forces `az login` credentials, avoiding accidental use of an Azure VM's managed identity. Use `FOUNDRY_REGIONS` to restrict regions and speed up debugging.
- If libicu is unavailable and Core Tools reports `Couldn't find a valid ICU package`, first run `export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1`.
- Local `func start` does not validate Function Keys. Editing `function_app.py` restarts the host and interrupts running collection; changes to `core/` and other modules require a manual host restart.

> Status: code and IaC are implemented and verified offline; deployment in a real environment has not yet been validated. See [Design](plan.md) and [AGENTS.md](../AGENTS.md) for development conventions.
