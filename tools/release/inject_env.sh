#!/bin/bash
# 把内部分发密钥注入无密钥安装包，产出可私密分发的最终 zip。
# 用法：tools/release/inject_env.sh [--env secrets/.env.release] <无密钥包.zip> [更多.zip...]
# 产物：dist/release/<原名>.zip
# 密钥文件不入库（secrets/ 已在 .gitignore）；最终包切勿上传公网。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="$REPO/secrets/.env.release"
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --env) ENV_FILE="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
[ ${#ARGS[@]} -gt 0 ] || { echo "用法：$0 [--env 密钥文件] <无密钥包.zip>..."; exit 1; }
[ -f "$ENV_FILE" ] || { echo "缺少密钥文件 $ENV_FILE（可从本机 .env 复制：cp .env secrets/.env.release）"; exit 1; }

grep -Eq '^DASHSCOPE_API_KEY=.{8,}' "$ENV_FILE" \
  || { echo "密钥文件中 DASHSCOPE_API_KEY 为空，拒绝注入"; exit 1; }

OUT_DIR="$REPO/dist/release"
mkdir -p "$OUT_DIR"

for ZIP in "${ARGS[@]}"; do
  [ -f "$ZIP" ] || { echo "找不到 $ZIP"; exit 1; }
  NAME="$(basename "$ZIP")"
  WORK="$(mktemp -d)"
  echo "== 注入 $NAME"
  unzip -qq "$ZIP" -d "$WORK"
  BUNDLE="$(find "$WORK" -maxdepth 1 -type d -name "CCiteheck-*" | head -1)"
  [ -n "$BUNDLE" ] || { echo "包结构异常：未找到 CCiteheck-* 根目录"; exit 1; }
  [ -d "$BUNDLE/payload" ] || { echo "包结构异常：缺少 payload/"; exit 1; }

  # 注入 .env 并强制修正 EUR-Lex 网关端点
  cp "$ENV_FILE" "$BUNDLE/payload/.env"
  if grep -q '^EURLEX_MCP_GATEWAY=' "$BUNDLE/payload/.env"; then
    sed -i '' 's|^EURLEX_MCP_GATEWAY=.*|EURLEX_MCP_GATEWAY=http://127.0.0.1:3010/mcp|' "$BUNDLE/payload/.env"
  else
    printf '\nEURLEX_MCP_GATEWAY=http://127.0.0.1:3010/mcp\n' >> "$BUNDLE/payload/.env"
  fi

  # 校验
  grep -Eq '^DASHSCOPE_API_KEY=.{8,}' "$BUNDLE/payload/.env" || { echo "注入后校验失败"; exit 1; }
  grep -q '^EURLEX_MCP_GATEWAY=http://127.0.0.1:3010/mcp$' "$BUNDLE/payload/.env" || { echo "EURLEX 网关校验失败"; exit 1; }

  OUT="$OUT_DIR/$NAME"
  rm -f "$OUT"
  (cd "$WORK" && zip -ryq "$OUT" "$(basename "$BUNDLE")")
  listing="$(unzip -l "$OUT")"
  grep -q "payload/\.env$" <<< "$listing" || { echo "最终包缺少 .env"; exit 1; }
  rm -rf "$WORK"
  echo "   产出 $OUT（含密钥，仅限私密渠道分发）"
done
echo "全部完成。提醒：最终包不得上传 GitHub/公网。"
