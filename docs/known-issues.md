# 已知问题

## 2026-07-18：Render 偶发提示未配置语义核查密钥

### 现象

使用指向 `https://cciteheck-api.onrender.com` 的 Word 测试加载项时，曾出现以下提示：

> 语义核查默认开启，需要在 .env 中配置 DASHSCOPE_API_KEY（参考 .env.example）；如仅做存在性核查可显式关闭语义核查。

随后在同一 Render 服务 `srv-d9d311qhil2s73b70t4g` 上重新测试，语义核查可以正常运行。

### 已确认

- Render 服务已经配置 token，当前语义核查可用。
- `apps/word_addin/manifest.render.xml` 指向上述 Render 公网服务。
- 旧提示来自 `QwenSemanticChecker.from_env()` 初始化异常外层的写死兜底，并不包含当次底层异常的更多诊断信息。
- 该写死兜底已删除，后续会直接返回底层 `SemanticCheckError`。
- 本地相关测试通过；尚未取得异常发生时的 Render 历史日志，因此根因未定。

### 明日排查

1. 在 Render 日志中按异常发生时间查找对应的 `POST /api/checks` 或 `POST /api/checks/selection` 请求及实例重启、部署记录。
2. 确认异常请求与成功请求是否由同一部署、同一实例处理。
3. 核对 Render 环境变量是否曾在部署期间更新，以及旧实例何时退出。
4. 若再次复现，在不记录密钥内容的前提下记录部署版本、实例标识、环境变量是否存在及 `debug_run_id`。
5. 根据日志判断是否需要为 API 响应和调试记录增加部署版本、实例标识等诊断字段。
