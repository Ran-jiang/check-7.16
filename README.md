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

`PKULAW_ACCESS_TOKEN` 是可选项，用于两处北大法宝 MCP 调用：法条 fallback（本地库未命中的法条），以及案号核验（`anhao_recognition` 案号识别与溯源）。当前 42 部本地法规/司法解释已有全文，命中本地库时法条不需要它；案号核验没有本地库，未配置 token 时相关核查标记为 `source_not_configured`。

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
- 北大法宝 MCP 可在本地未命中时确认法规、时效和效力元数据；其当前无条号工具不返回匹配条号或条文全文。
- 案号核验已接入北大法宝 `anhao_recognition`：对文书中带案号的案例引用（`with_case_number`）做识别 + 标准化验证，命中则回填权威案名、法院、裁判日期和溯源链接；文书案号在亿级案例库未命中时标记为疑似有误/不存在。
- 语义核查只基于引用原句、所在语义块、已查得的法条和元数据，不引入外部案件事实。
- 未支持：跨句复杂召回、完整案情事实归纳。
- 带案号案例走北大法宝精确核验；无案号的案名/指导案例/典型案例会保留为 `manual_review`，明确提示人工检索，不再从结果中消失。
- Word 网页版不支持获取完整压缩 DOCX；当前插件完整文档核查面向 Word for Mac 和 Word for Windows。
