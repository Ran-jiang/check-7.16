# CCitecheck

法律文书引注检查工具，支持 CLI 和 Microsoft Word 任务窗格插件。默认链路做确定性的引注溯源：

1. 解析 DOCX
2. 抽取明确的法规/条文引用
3. 查询本地 SQLite 法规库
4. 输出前端可用的核验 JSON

核验结果同时支持：

1. 法规溯源：本地法条库 + 北大法宝回退，确认法规、条文存在并取回原文。
2. 语义对比：默认调用千问，核查法源、定位、时效以及引用表述是否忠实于权威原文；不评价法律论证或结论是否成立。

## 运行条件

- Python 3.12
- `requirements.txt` 中的依赖
- 本地法规库：`data/laws.sqlite`

默认流程开启语义核查，因此 `DASHSCOPE_API_KEY` 默认必需；仅显式使用 `--no-semantic-check` 时可以不配置。服务器的统一密钥放在项目根目录 `.env`：

```env
DASHSCOPE_API_KEY=你的百炼APIKey
QWEN_MODEL=qwen3.7-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

`.env` 已被 Git 忽略，不应提交到代码库。

`PKULAW_ACCESS_TOKEN` 是可选项，用于北大法宝多路 MCP：精准法条（`/mcp-fatiao`）、法规关键词（`/mcp-law`）、法规语义（`/mcp-law-search-service`）、案号识别（`/case_number_recognition`）、案例关键词（`/mcp-case`）和案例语义检索（`/mcp-case-search-service`）。当前 42 部本地法规/司法解释已有全文，命中本地库时法条不消耗法宝调用；案例没有本地库，未配置 token 时相关核查标记为 `source_not_configured`。配置只使用 `PKULAW_ACCESS_TOKEN` 与 `PKULAW_MCP_GATEWAY`，不再读取旧版 URL/Headers 字段。

## 快速检查

```bash
python3 main.py doctor --law-db data/laws.sqlite
```

## Word for Mac 插件

插件从 Word 读取当前完整 DOCX，调用同域的 FastAPI 服务，然后在右侧任务窗格展示法规溯源和语义核查结果。API Key 只保存在服务器 `.env`，不会发送到 Word 客户端。

任务窗格支持：

- **核查全文 / 核查选中内容**：全文扫描或只核查当前选区（`POST /api/checks/selection`）。
- **定位原文**：点击结果卡片的"定位原文"，光标跳转到文档中对应片段。
- **接受 / 忽略**：对每条核查结论做人工标记，标记按文档内容哈希持久化，作为尽职履责记录。
- **导出报告**：生成自包含 HTML 核查报告（`POST /api/reports`），含摘要、逐条明细、数据来源与人工处理记录，可直接打印或另存为 PDF 随文件交付。

### 首次运行

```bash
cd ccitecheck  # 仓库根目录
python3 -m pip install -r requirements.txt
npm install
npm run certs
npm start
```

开发证书安装完成后，将 [word-addin/manifest.xml](word-addin/manifest.xml) 复制到：

```text
~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
```

重启 Word，在“开始 → 加载项”中选择“CCitecheck 法律引用核查”。

### 校验 manifest

```bash
npm run validate:manifest
```

正式部署时，将 manifest 中的 `https://localhost:3000` 统一替换为服务器 HTTPS 域名；前端与 `/api/checks` 保持同域。

## 使用

```bash
python3 main.py parse input.docx \
  --claims-out outputs/claims.json \
  --verify-out outputs/verification.json \
  --law-db data/laws.sqlite
```

开启引用忠实度核查（默认已开启）：

```bash
python3 main.py parse input.docx \
  --verify-out outputs/verification.json \
  --law-db data/laws.sqlite \
  --semantic-check
```

## 当前边界

- 已支持：明确写出的法规名称、条文号、部分承前省略条文引用。
- 已支持千问引用忠实度核查；只比较引用表述与权威文本，不评价法律论证是否成立。
- 已支持无条号法规引用：对本地已有全文的法规召回最相关的当前有效条款，再执行语义核验。
- 北大法宝按工具能力构造独立检索输入：有条号引用走精准法条并以法规关键词复核法名/时效；无条号法规先走法规语义召回具体条文，再以标题和正文关键词确认法规元数据。
- 案例核查采用三路：带案号走 `anhao_recognition` 精确识别；无案号先按案名和正文关键词查案例库，再用案例语义检索补召回。只有案名（及文书给出时的法院）能唯一匹配才自动通过，只有相关候选时转人工确认。
- 语义核查只基于引用原句、所在语义块、已查得的法条和元数据，不引入外部案件事实。
- 未支持：跨句复杂召回、完整案情事实归纳。
- 案例 MCP 返回候选但无法确定唯一同一案例时保留为 `manual_review`；当前不评价案件裁判观点或法律论证是否成立。
- Word 网页版不支持获取完整压缩 DOCX；当前插件完整文档核查面向 Word for Mac 和 Word for Windows。
