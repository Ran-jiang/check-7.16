# Render 部署配置

公网测试版使用 `https://cciteheck-api.onrender.com`。服务端至少配置以下秘密环境变量：

- `DASHSCOPE_API_KEY`：语义核查服务密钥。
- `PKULAW_ACCESS_TOKEN`：北大法宝 MCP 凭证。

可选配置包括 `QWEN_MODEL`、`QWEN_BASE_URL` 和 `PKULAW_MCP_GATEWAY`。不得把密钥写入 manifest、安装脚本、日志或 API 响应。

部署后访问 `/api/health`，确认 `status` 为 `ok`，并检查 `llm_configured`、`pkulaw_configured` 均为 `true`。这两个字段只表示环境变量是否存在，不验证或返回密钥内容。

测试者文书会上传至该服务处理。评审期间应明确告知测试者隐私边界、共享调用额度和 Render 冷启动等待时间。
