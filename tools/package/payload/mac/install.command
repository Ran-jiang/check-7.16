#!/bin/bash
# CCiteheck 本地完整服务安装器（macOS）
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/Applications/CCiteheck"
AGENT_DIR="$HOME/Library/LaunchAgents"
WEF_DIR="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef"
WEF_CACHE="$HOME/Library/Containers/com.microsoft.Word/Data/Library/Caches/Wef"
UID_NUM="$(id -u)"

fail() { echo ""; echo "安装失败：$1"; echo "可重新运行本安装器；已有安装不会损坏。"; read -r -p "按回车退出..."; exit 1; }

echo "CCiteheck 本地服务安装器"
echo "========================"

# 0. 解除 Gatekeeper 隔离标记（对整个解包目录）
xattr -rd com.apple.quarantine "$SRC" 2>/dev/null || true

# 1. 预检
if pgrep -x "Microsoft Word" >/dev/null 2>&1; then
  fail "Microsoft Word 正在运行，请完全退出 Word 后重新运行安装器。"
fi
for port in 3000 3010; do
  holder="$(lsof -nP -iTCP:$port -sTCP:LISTEN -Fc 2>/dev/null | sed -n 's/^c//p' | head -1)"
  if [ -n "$holder" ]; then
    if launchctl print "gui/$UID_NUM/com.ccitecheck.api" >/dev/null 2>&1 \
      || launchctl print "gui/$UID_NUM/com.ccitecheck.eurlex" >/dev/null 2>&1; then
      echo "检测到旧版 CCiteheck 服务，将自动升级。"
    else
      fail "端口 $port 已被进程「$holder」占用，请先释放该端口。"
    fi
    break
  fi
done

# 2. 停旧服务（若有）
launchctl bootout "gui/$UID_NUM/com.ccitecheck.api" 2>/dev/null || true
launchctl bootout "gui/$UID_NUM/com.ccitecheck.eurlex" 2>/dev/null || true

# 3. 拷贝 payload（升级时保留已有 .env 与日志）
echo "[1/6] 安装文件到 $INSTALL_DIR ..."
KEEP_ENV=""
if [ -f "$INSTALL_DIR/.env" ]; then
  KEEP_ENV="$(mktemp)"; cp "$INSTALL_DIR/.env" "$KEEP_ENV"
fi
mkdir -p "$INSTALL_DIR"
ditto "$SRC/payload/" "$INSTALL_DIR/" || fail "文件拷贝失败"
mkdir -p "$INSTALL_DIR/logs"
if [ -n "$KEEP_ENV" ]; then
  cp "$KEEP_ENV" "$INSTALL_DIR/.env"; rm -f "$KEEP_ENV"
elif [ ! -f "$INSTALL_DIR/.env" ]; then
  cp "$INSTALL_DIR/.env.template" "$INSTALL_DIR/.env"
  echo "提示：包内未含密钥，已用模板生成 .env——语义核查需要填入 DASHSCOPE_API_KEY。"
fi
cp "$SRC/uninstall.command" "$INSTALL_DIR/uninstall.command" 2>/dev/null || true
chmod +x "$INSTALL_DIR"/bin/*.sh "$INSTALL_DIR/uninstall.command" 2>/dev/null || true

# 4. HTTPS 开发证书（10 年有效期）
echo "[2/6] 安装并信任 localhost HTTPS 证书（可能需要输入登录密码）..."
"$INSTALL_DIR/runtime/node/bin/node" \
  "$INSTALL_DIR/vendor/certs/node_modules/office-addin-dev-certs/cli.js" \
  install --days 3650 || fail "HTTPS 证书安装未完成"

# 5. 注册并启动常驻服务
echo "[3/6] 注册开机自启服务..."
mkdir -p "$AGENT_DIR"
for svc in api eurlex; do
  sed "s|__ROOT__|$INSTALL_DIR|g" "$INSTALL_DIR/com.ccitecheck.$svc.plist.tmpl" \
    > "$AGENT_DIR/com.ccitecheck.$svc.plist"
  launchctl bootstrap "gui/$UID_NUM" "$AGENT_DIR/com.ccitecheck.$svc.plist" \
    || fail "服务 com.ccitecheck.$svc 启动失败，日志见 $INSTALL_DIR/logs/"
done

# 6. 健康检查
echo "[4/6] 等待服务就绪..."
ok_api=""
for i in $(seq 1 30); do
  sleep 1
  code="$(curl -s -o /dev/null -w "%{http_code}" -m 2 https://localhost:3000/api/health || true)"
  [ "$code" = "200" ] && ok_api=1 && break
done
[ -n "$ok_api" ] || fail "API 服务未在 30 秒内就绪，日志见 $INSTALL_DIR/logs/api.err.log"
ok_eu=""
for i in $(seq 1 15); do
  sleep 1
  code="$(curl -s -o /dev/null -w "%{http_code}" -m 2 -X POST http://127.0.0.1:3010/mcp \
    -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"installer","version":"1.0"}}}' || true)"
  [ "$code" = "200" ] && ok_eu=1 && break
done
[ -n "$ok_eu" ] || echo "警告：EUR-Lex 服务未就绪（不影响国内法规核查），日志见 $INSTALL_DIR/logs/eurlex.err.log"

# 7. Word 加载项
echo "[5/6] 安装 Word 加载项..."
mkdir -p "$WEF_DIR"
cp "$INSTALL_DIR/apps/word_addin/manifest.xml" "$WEF_DIR/ccitecheck-manifest.xml"
rm -rf "$WEF_CACHE" 2>/dev/null || true

# 8. 自检
echo "[6/6] 环境自检："
"$INSTALL_DIR/bin/run-doctor.sh" || true

echo ""
echo "安装完成！"
echo "· Word：打开 Word → 开始 → 加载项 → CCiteheck 法律引用核查"
echo "· 网页版：https://localhost:3000（即将自动打开）"
echo "· 卸载：运行 $INSTALL_DIR/uninstall.command"
open "https://localhost:3000" 2>/dev/null || true
read -r -p "按回车关闭..."
