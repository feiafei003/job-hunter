#!/usr/bin/env bash
# Cursor LLM 中转服务启动脚本（部署在能直连 Cursor 的服务器上，如阿里云 ECS）。
#
# 用法：
#   1. cp .env.example .env  并填好 RELAY_TOKEN / CURSOR_API_KEY
#   2. sudo ./run.sh        （绑定 443 需要 root）
#
# 它会：建虚拟环境 → 装依赖 → 没证书就生成自签证书 → 用 TLS 跑在 0.0.0.0:443。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${RELAY_PORT:-443}"
CERT_DIR="./certs"
CERT="$CERT_DIR/relay.crt"
KEY="$CERT_DIR/relay.key"

# 读取 .env（RELAY_TOKEN / CURSOR_API_KEY / RELAY_DEFAULT_MODEL 等）
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ -z "${RELAY_TOKEN:-}" ] || [ -z "${CURSOR_API_KEY:-}" ]; then
  echo "[!] 请先在 relay/.env 里设置 RELAY_TOKEN 和 CURSOR_API_KEY" >&2
  exit 1
fi

# 虚拟环境 + 依赖（cursor-sdk 需要 Python >= 3.10）
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# 自签证书（244 端 cursor_relay_verify_tls=false 时无需可信 CA）
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  mkdir -p "$CERT_DIR"
  echo "[*] 生成自签 TLS 证书 ..."
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CERT" -subj "/CN=cursor-relay" >/dev/null 2>&1
fi

echo "[*] 启动中转服务：https://0.0.0.0:$PORT  (默认模型: ${RELAY_DEFAULT_MODEL:-composer-2.5})"
exec ./.venv/bin/uvicorn relay_server:app \
  --host 0.0.0.0 --port "$PORT" \
  --ssl-keyfile "$KEY" --ssl-certfile "$CERT"
