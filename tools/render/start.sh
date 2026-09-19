#!/bin/bash
# Render 单实例双进程启动:EUR-Lex MCP(127.0.0.1:3010)+ FastAPI($PORT)。
# 第二个进程不另收费;MCP 起不来只降级欧盟法核查,不阻断国内法与案例核查。
set -uo pipefail

MCP_PORT="${MCP_HTTP_PORT:-3010}"
MCP_LOG="${TMPDIR:-/tmp}/eurlex-mcp.log"
MCP_PID=""

start_eurlex_mcp() {
  echo "[start] launching EUR-Lex MCP on 127.0.0.1:$MCP_PORT ..."
  MCP_TRANSPORT_TYPE=http MCP_HTTP_PORT="$MCP_PORT" \
    npx -y @cyanheads/eur-lex-mcp-server >"$MCP_LOG" 2>&1 &
  MCP_PID=$!
  for _ in $(seq 1 30); do
    # 只要端口能建立连接即视为就绪(HTTP 状态码不做要求)
    if curl -s -o /dev/null "http://127.0.0.1:$MCP_PORT/mcp" 2>/dev/null; then
      echo "[start] EUR-Lex MCP ready (pid $MCP_PID)"
      return 0
    fi
    if ! kill -0 "$MCP_PID" 2>/dev/null; then
      echo "[start] WARN: EUR-Lex MCP exited early; EU statute checks will degrade. Log: $MCP_LOG"
      MCP_PID=""
      return 0
    fi
    sleep 1
  done
  echo "[start] WARN: EUR-Lex MCP not ready in 30s; continuing anyway. Log: $MCP_LOG"
  return 0
}

start_eurlex_mcp

# exec 让 uvicorn 直接接替本进程,Render 的停止信号可直达主进程;
# MCP 子进程随实例回收一并终止。
exec python3 -m apps.api.server --host 0.0.0.0 --port "${PORT:-3000}"
