# ms-foundry-notification

[English](README.md) | **简体中文**

每日自动追踪 Microsoft Foundry 模型的生命周期变化（Preview→GA、上线、下线/退役、替代模型、自动升级）与 **API 零售价格变化**，并通过 **REST API** 与 **MCP Server**（供 LLM / Agent 调用）提供。

## 使用

通过 REST 或六个只读 MCP 工具查询今天、过去和未来 N 天的变化、模型目录与当前价格；两种接口均使用 `x-functions-key` 请求头认证。

端点、客户端配置、密钥、分页与采集状态详见 [API 与 MCP](docs/api.zh-CN.md)。

## 快速开始

需要订阅级 **Owner**，或 Contributor + User Access Administrator 权限。

打开 [Azure Cloud Shell](https://shell.azure.com)，选择 **Bash**，执行：

```bash
curl -fsSL https://raw.githubusercontent.com/pczhao1210/ms-foundry-notification/main/deploy.sh | bash
```

中英双语向导会依次确认订阅、区域、资源组、资源前缀和 UTC 触发时间。Azure Functions 应用默认每天北京时间 08:00 采集，首次采集成功后即可查询数据。

也可克隆仓库后依次运行 `azd auth login` 和 `azd up`。

部署参数、调度、监控与排障详见 [部署与运维](docs/deployment.zh-CN.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [API 与 MCP](docs/api.zh-CN.md) | REST 端点、MCP 配置、认证、分页 |
| [部署与运维](docs/deployment.zh-CN.md) | 部署选项、调度、采集状态、排障、告警 |
| [开发指南](docs/development.zh-CN.md) | 本地环境、Azurite、测试、调试、CI |
| [项目设计](docs/plan.md) | 数据源、架构、事件语义与局限 |
| [贡献约定](AGENTS.md) | 仓库开发规范 |

## License

[MIT](LICENSE)