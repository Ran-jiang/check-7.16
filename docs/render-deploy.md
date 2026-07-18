# Render 部署配置

公网测试版使用 `https://cciteheck-api.onrender.com`。根路径为零安装网页版，
Word 加载项仍使用 `/taskpane.html`，两端共用同一核验 API 与结果模型。
服务端至少配置以下秘密环境变量：

- `DASHSCOPE_API_KEY`：语义核查服务密钥。
- `PKULAW_ACCESS_TOKEN`：北大法宝 MCP 凭证。

可选配置包括 `QWEN_MODEL`、`QWEN_BASE_URL` 和 `PKULAW_MCP_GATEWAY`。不得把密钥写入 manifest、安装脚本、日志或 API 响应。

部署后访问 `/api/health`，确认 `status` 为 `ok`，并检查 `llm_configured`、`pkulaw_configured` 均为 `true`。这两个字段只表示环境变量是否存在，不验证或返回密钥内容。

测试者文书会上传至该服务处理。网页版不会把文书写入 `debug_runs`；原始
DOCX 仅保存在进程内的一小时短期会话中，到期由定时清理任务删除。网页版
同一来源十分钟最多发起 6 次核查，超限返回 HTTP 429。评审期间仍应明确告知
测试者隐私边界、共享调用额度和 Render 冷启动等待时间。
