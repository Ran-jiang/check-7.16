# CCiteheck 部署指南

两种场景：给同事的另一台开发机（快速照搬），和部署到服务器给团队使用（正式路径）。

## 依赖清单

| 项 | 说明 |
|---|---|
| Python 3.12 + `requirements.txt` | 后端核查服务 |
| Node.js | 开发证书工具、EUR-Lex MCP 服务（`npx` 拉起） |
| `data/laws.sqlite` | 本地法规库（随仓库分发） |
| `.env` | 全部密钥所在，**只走私密渠道分发，绝不进 git** |

`.env` 必填/可选项：

```env
DASHSCOPE_API_KEY=…            # 必填：千问语义核查
QWEN_MODEL=qwen3.7-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
PKULAW_ACCESS_TOKEN=…          # 可选：北大法宝溯源（法规回退 + 案例核查）
EURLEX_MCP_GATEWAY=http://127.0.0.1:3010/mcp   # 可选：欧盟法规核验
CCITECHECK_DEBUG_CAPTURE=0     # 生产建议关闭：调试现场含文书原文
```

---

## 场景一：另一台开发机（本机试用）

```bash
git clone <仓库> && cd ccitecheck
python3 -m pip install -r requirements.txt
npm install
npm run certs        # 安装并信任 localhost 开发证书（需要系统密码）
# 配好 .env（见上）
npm run eurlex       # 终端 1：EUR-Lex MCP 服务（首次运行会下载包）
npm start            # 终端 2：后端（HTTPS，用开发证书）
```

自检：

```bash
PYTHONPATH=src python3 -m apps.cli.main doctor --law-db data/laws.sqlite
```

四项 OK 后 sideload 插件：

- **Mac**：`cp apps/word_addin/manifest.xml ~/Library/Containers/com.microsoft.Word/Data/Documents/wef/`
- **Windows**：配置共享文件夹信任目录（Word → 文件 → 选项 → 信任中心 → 受信任的加载项目录），把 manifest 放进去

完全重启 Word，在「开始 → 加载项」选择「CCiteheck 法律引用核查」。

---

## 场景二：服务器部署（团队使用）

架构：服务器跑后端 + EUR-Lex 服务，API key 全部留在服务器；用户 Word 里装指向服务器域名的插件，零配置。

### 1. 起服务

```bash
pip install -r requirements.txt
# .env 照上文；EURLEX_MCP_GATEWAY 保持 127.0.0.1（EUR-Lex 只对内，不暴露公网）
npm run eurlex           # EUR-Lex MCP（或用 Docker：ghcr.io/cyanheads/eur-lex-mcp-server）
npm run start:http       # 后端跑纯 HTTP :3000，TLS 交给反向代理
```

用 systemd 管理（示例，两份 unit 同理）：

```ini
# /etc/systemd/system/ccitecheck.service
[Unit]
Description=CCiteheck API
After=network.target

[Service]
WorkingDirectory=/opt/ccitecheck
Environment=PYTHONPATH=src
ExecStart=/usr/bin/python3 -m apps.api.server
Restart=always

[Install]
WantedBy=multi-user.target
```

### 2. HTTPS 反向代理

Word 插件强制要求正式 HTTPS 域名。nginx + Let's Encrypt：

```nginx
server {
    listen 443 ssl;
    server_name check.example.com;
    ssl_certificate     /etc/letsencrypt/live/check.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/check.example.com/privkey.pem;
    location / { proxy_pass http://127.0.0.1:3000; }
}
```

前端静态资源与 `/api/checks` 保持同域（同域原则，免 CORS）。

### 3. 改 manifest

把 `apps/word_addin/manifest.xml` 中所有 `https://localhost:3000` 全局替换为
`https://check.example.com`，然后校验：

```bash
npm run validate:manifest
```

### 4. 分发给用户

- **组织级集中部署（推荐）**：Microsoft 365 管理中心 → 设置 → 集成应用
  （Integrated Apps）→ 上传自定义应用 → 上传 manifest → 指定用户/部门。
  用户的 Word（Mac/Windows）自动出现插件，无需任何手工操作。
- **小范围手动**：按场景一的 sideload 方式，每人复制一次 manifest（把
  localhost 版换成服务器域名版）。

### 5. 上线前检查

- [ ] 服务器上 `doctor` 四项 OK（law_db / qwen / pkulaw / eurlex）
- [ ] `curl https://check.example.com/api/health` 返回 `{"status":"ok"}`
- [ ] `CCITECHECK_DEBUG_CAPTURE=0` 已设置（多用户文书原文不落盘）
- [ ] **访问控制**：当前 API 无鉴权，任何拿到域名的人都能消耗千问/法宝额度。
      内部使用至少在 nginx 限 IP 段或加 Basic Auth；开放公网前必须给 API
      增加认证层（待办）。

## 已知边界

- Word 网页版不支持获取完整 DOCX，插件面向 Word for Mac / Windows 桌面版。
- EUR-Lex 目前为存在性/时效核验（P0）；跨语言条文语义比对是后续项。
- EUR-Lex 也可改用社区公共端点 `https://eur-lex.caseyjhand.com/mcp`（免部署，
  但法规名查询词会经过第三方服务器，正式环境建议自托管）。
