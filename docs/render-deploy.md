# CCiteheck 公网服务部署与升档清单(Render)

评委/测试者安装入口:`https://cciteheck-api.onrender.com/install.html`
(页面由本服务托管,随部署自动更新)

## Render 面板配置

| 项目 | 值 |
|---|---|
| Region | **Singapore**(国内访问延迟最低) |
| Instance Type | **Starter**($7/月,不休眠;免费档冷启动约 30 秒,评委体验差) |
| Build Command | `pip install -e .` |
| Start Command | `sh tools/render/start.sh` |
| Health Check Path | `/api/health` |

`start.sh` 会在同一实例内先后拉起 EUR-Lex MCP(127.0.0.1:3010)与 FastAPI,
不另收第二个实例的费用。MCP 启动失败仅降级欧盟法核查,服务整体照常可用。

## 环境变量(面板里配置,不要写入代码库)

| 变量 | 说明 |
|---|---|
| `DASHSCOPE_API_KEY` | 百炼(通义)语义核查密钥 |
| `PKULAW_ACCESS_TOKEN` | 北大法宝 MCP 访问令牌(案例与法规数据源)。**只填令牌本身,不要带 `Bearer ` 前缀**(代码发请求时自动补,带了会鉴权失败) |
| `EURLEX_MCP_GATEWAY` | `http://127.0.0.1:3010/mcp`(由 start.sh 拉起的本地网关) |
| `ANSVAR_MCP_GATEWAY` | `https://gateway.ansvar.eu/mcp`(Ansvar 多法域法规源;不配则该源降级为"待核实",不影响其他源) |
| `ANSVAR_TOKENS_B64` | 本机执行 `base64 -i ~/.ccitecheck/ansvar_tokens.json` 的输出,让无浏览器的服务器复用本机 OAuth 登录态;start.sh 启动时解码写回,首次调用自动续期。与 `ANSVAR_MCP_GATEWAY` 成对配置。refresh token 失效后在本机重新授权并更新此值 |
| `CCITECHECK_ALLOWED_ORIGINS` | 可选;加载项与 API 同源,默认无需配置 |

注意:`data/laws.sqlite` 已提交在 git,每次部署自动检出,法规语料不会丢;
`data/pkulaw_cache.sqlite` 是缓存,实例重建后自动重建,无需处理。

## 发布新版本

1. 本地 `npm run test:js` 通过后 push 到 Render 关联的分支
2. Render 自动部署(约 2-5 分钟);`/api/health` 返回 200 即就绪
3. 加载项前端(assets/taskpane.*)有 `Cache-Control: no-cache`,Word WebView 下次打开自动取新版

## 故障速查

- **免费档冷启动(已缓解)**:`.github/workflows/keep-render-warm.yml` 每 10 分钟探活 `/api/health`,免费实例不再休眠;注意 GitHub 定时任务在高负载时可能延迟触发,仓库 60 天无提交会暂停定时任务(会邮件提醒,Actions 页可一键恢复)。要根治请升 Starter
- **首次连接慢**:升 Starter 后不应再出现;若仍偶发,多为 Render 平台重启,等 1 分钟自愈
- **欧盟法提示“数据源未配置”**:查看实例日志中 `[start]` 行,确认 EUR-Lex MCP 是否就绪(`npx` 首次下载包可能超时,重新部署即可)
- **海外法域提示“网关未配置”**:确认 `ANSVAR_MCP_GATEWAY` 与 `ANSVAR_TOKENS_B64` 均已在面板配置;部署日志应出现 `[start] ansvar tokens restored`,若为 WARN 说明 base64 值贴错或已过期(本机重新授权后更新该值)
- **Word 里加载项消失**:多为 manifest 被清理,重跑安装器或重新上传 `/manifest.xml`
- **评委反馈“上传我的加载项”找不到入口**:部分旧版 Word 无此按钮,改用共享文件夹方式(`tools/word-installers/windows/`)
