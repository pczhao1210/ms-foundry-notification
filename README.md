# ms-foundry-notification

**English** | [简体中文](README.zh-CN.md)

Automatically track daily Microsoft Foundry model lifecycle changes (Preview -> GA, new models, removal/retirement, replacements, and automatic upgrades) and **API retail price changes**, available through a **REST API** and an **MCP Server** for LLMs and agents.

## Usage

Query today's changes, the past or next N days, the model catalog, and current prices through REST or six read-only MCP tools. Both interfaces use the `x-functions-key` header.

See [API and MCP](docs/api.md) for endpoints, client configuration, keys, pagination, and collection status.

## Quick Start

Requires subscription-level **Owner**, or Contributor + User Access Administrator.

Open [Azure Cloud Shell](https://shell.azure.com), select **Bash**, and run:

```bash
curl -fsSL https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh | bash
```

The bilingual wizard prompts for subscription, region, resource group, resource prefix, and UTC schedule. The Azure Functions app collects daily at 08:00 Asia/Shanghai by default; data becomes available after the first successful collection.

Alternatively, clone the repository and run `azd auth login` followed by `azd up`.

See [Deployment and Operations](docs/deployment.md) for deployment options, scheduling, monitoring, and troubleshooting.

## Documentation

| Guide | Contents |
|---|---|
| [API and MCP](docs/api.md) | REST endpoints, MCP configuration, authentication, pagination |
| [Deployment and Operations](docs/deployment.md) | Deployment options, schedules, collection health, troubleshooting, alerts |
| [Development](docs/development.md) | Local setup, Azurite, testing, debugging, CI |
| [Design](docs/plan.md) | Data sources, architecture, event semantics, limitations |
| [Contributing](AGENTS.md) | Repository conventions |

> Status: code and IaC are implemented and verified offline; deployment in a real environment has not yet been validated.

## License

[MIT](LICENSE)
