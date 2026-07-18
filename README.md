# CCiteheck

法律文书引注检查工具，支持 CLI 和 Microsoft Word 任务窗格插件。默认链路做确定性的引注溯源：

1. 解析 DOCX
2. 抽取明确的法规/条文引用
3. 查询本地 SQLite 法规库
4. 输出前端可用的核验 JSON

核验结果同时支持：

1. 法规溯源：本地法条库 + 北大法宝回退，确认法规、条文存在并取回原文。
2. 语义对比：默认调用千问，核查法源、定位、时效以及引用表述是否忠实于权威原文；不评价法律论证或结论是否成立。

## 项目结构

核心代码按“解析、识别、溯源、判定、输出”组织，Word 和飞书只作为外围应用调用同一条流水线：

```text
src/ccitecheck/
├── domain/          # 平台无关的数据模型
├── parsing/         # DOCX、飞书快照解析与结构校验
├── recognition/     # 法规、条款和案例引用识别
├── tracing/         # 本地法规库与北大法宝溯源
├── judgment/        # 确定性、语义和案例判定
├── output/          # 摘要与 HTML 报告输出
├── application/     # 文档核查用例编排
└── infrastructure/  # 配置、数据库与运行检查

apps/
├── api/             # FastAPI 服务
├── cli/             # 命令行应用
├── word_addin/      # Microsoft Word 插件
└── feishu/          # 飞书文档插件
```

核心代码统一从 `ccitecheck` 导入，各运行入口统一位于 `apps`。

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

本机测试默认会把完整调试现场保存到 `debug_runs/<时间-编号>/`，包括原始
DOCX、Blocks/Anchors、Claims、核查响应和 Word 定位事件。该目录已被 Git
忽略，但内容可能包含敏感原文；如需关闭，设置
`CCITECHECK_DEBUG_CAPTURE=0`。

`PKULAW_ACCESS_TOKEN` 是可选项，用于北大法宝 MCP：法规关键词（`/mcp-law`）、法规语义与精确查条（`/mcp-law-search-service`）、案例精准检索（`/mcp-case` 的 `get_case_list`）和案例语义检索（`/mcp-case-search-service` 的 `search_case`）。当前 42 部本地法规/司法解释已有全文，命中本地库时法条不消耗法宝调用；案例没有本地库，未配置 token 时相关核查标记为 `source_not_configured`。北大法宝配置只读取 `PKULAW_ACCESS_TOKEN` 与 `PKULAW_MCP_GATEWAY`。

## 快速检查

```bash
PYTHONPATH=src python3 -m apps.cli.main doctor --law-db data/laws.sqlite
```

## Word for Mac 插件

插件从 Word 读取当前完整 DOCX，调用同域的 FastAPI 服务，然后在右侧任务窗格展示法规溯源和语义核查结果。API Key 只保存在服务器 `.env`，不会发送到 Word 客户端。

任务窗格支持：

- **核查全文 / 核查选中内容**：全文扫描或只核查当前选区（`POST /api/checks/selection`）。
- **定位原文**：点击结果卡片的"定位原文"，光标跳转到文档中对应片段。
- **接受 / 忽略**：对每条核查结论做人工标记，标记按文档内容哈希持久化，作为尽职履责记录。

### 首次运行

```bash
cd ccitecheck  # 仓库根目录
python3 -m pip install -r requirements.txt
npm install
npm run certs
npm start
```

开发证书安装完成后，将 [apps/word_addin/manifest.xml](apps/word_addin/manifest.xml) 复制到：

```text
~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
```

重启 Word，在“开始 → 加载项”中选择“CCiteheck 法律引用核查”。

### 校验 manifest

```bash
npm run validate:manifest
```

正式部署时，将 manifest 中的 `https://localhost:3000` 统一替换为服务器 HTTPS 域名；前端与 `/api/checks` 保持同域。

## 使用

```bash
PYTHONPATH=src python3 -m apps.cli.main parse input.docx \
  --claims-out outputs/claims.json \
  --verify-out outputs/verification.json \
  --law-db data/laws.sqlite
```

开启引用忠实度核查（默认已开启）：

```bash
PYTHONPATH=src python3 -m apps.cli.main parse input.docx \
  --verify-out outputs/verification.json \
  --law-db data/laws.sqlite \
  --semantic-check
```

## 当前边界

- 已支持：明确写出的法规名称、条文号、部分承前省略条文引用。
- 已支持千问引用忠实度核查；只比较引用表述与权威文本，不评价法律论证是否成立。
- 已支持无条号法规引用：对本地已有全文的法规召回最相关的当前有效条款，再执行语义核验。
- 北大法宝按工具能力构造独立检索输入：有条号引用走精准法条并以法规关键词复核法名/时效；无条号法规先走法规语义召回具体条文，再以标题和正文关键词确认法规元数据。
- 案例核查先调用 `get_case_list` 精准检索；无法确认案例身份时再调用 `search_case` 语义检索。仅在案号、完整案名或完整案名与明确法院别名能够唯一确认时通过；只有相关候选时转人工确认。
- 语义核查只基于引用原句、所在语义块、已查得的法条和元数据，不引入外部案件事实。
- 未支持：跨句复杂召回、完整案情事实归纳。
- 案例 MCP 返回候选但无法确定唯一同一案例时保留为 `manual_review`。案例身份确认后，仅核查文书转述是否属于该案裁判观点、是否直接曲解裁判观点；不评价遗漏前提或将个案结论扩张为一般规则。
- Word 网页版不支持获取完整压缩 DOCX；当前插件完整文档核查面向 Word for Mac 和 Word for Windows。
