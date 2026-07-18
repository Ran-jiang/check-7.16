# CCitecheck Word for Mac 测试指南

评委或小范围公网测试者应优先使用 `tools/word-installers/mac/install-ccitecheck.command`，无需安装开发环境或配置密钥。以下内容仅适用于需要运行本地服务的开发者。

本指南用于帮助测试者从 GitHub 下载并在本机运行 CCitecheck Word 插件。

## 运行条件

请先安装：

- Word for Mac
- Git
- Python 3.12
- Node.js 18

## 1. 下载代码

打开终端，执行：

```bash
cd ~/Documents
git clone https://github.com/Ran-jiang/check-7.16.git
cd check-7.16
```

## 2. 安装 Python 环境

在项目目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## 3. 安装前端依赖和开发证书

```bash
npm install
npm run certs
```

如果 macOS 弹出证书信任或密码窗口，请确认允许。

## 4. 配置核查服务

复制环境变量模板：

```bash
cp .env.example .env
```

使用文本编辑器打开 `.env`，填写测试者自己的百炼 API Key 和北大法宝 Token：

```env
DASHSCOPE_API_KEY=真实百炼APIKey
QWEN_MODEL=qwen3.7-plus
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

PKULAW_ACCESS_TOKEN=真实北大法宝Token
PKULAW_MCP_GATEWAY=https://apim-gateway.pkulaw.com

CCITECHECK_DEBUG_CAPTURE=0
```

> 请勿将 `.env` 提交到 GitHub，也不要互相发送 API Key 或 Token。

## 5. 安装 Word 插件

执行：

```bash
mkdir -p ~/Library/Containers/com.microsoft.Word/Data/Documents/wef
cp apps/word_addin/manifest.xml ~/Library/Containers/com.microsoft.Word/Data/Documents/wef/
```

安装完成后，请完全退出并重新打开 Word。

## 6. 启动服务

回到项目目录并执行：

```bash
source .venv/bin/activate
npm start
```

终端必须保持开启。看到服务成功运行后，在 Word 中依次进入：

```text
开始 → 加载项 → CCitecheck 法律引用核查
```

打开一篇 `.docx` 文档，即可测试以下功能：

- 核查全文
- 核查选中内容
- 定位原文
- 查看和处理核查结果

## 重要限制

- 每位测试者目前都需要自己的百炼 API Key 和北大法宝 Token。
- 终端关闭后，本机核查服务也会停止。
- Word 网页版不支持读取完整 DOCX，建议使用 Word for Mac 或 Word for Windows 桌面版。
- 不要把 Render 密钥或本机 `.env` 上传到 GitHub。

## 更方便的后续方案

可以另外生成一份连接 Render 公网服务的 Word manifest。届时测试者只需安装 manifest，无需克隆代码、安装 Python 或配置 Token。

当前仓库中的 manifest 仍然连接本机服务，尚未切换到公网地址。
