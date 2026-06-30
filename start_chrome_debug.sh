#!/usr/bin/env bash
# 启动一个"开启远程调试端口"的 Chrome / Chromium，供本程序通过 CDP 接管。
#
# 用法：
#   1) 在终端执行：  ./start_chrome_debug.sh
#   2) 远程访问：从笔记本/手机做 SSH 隧道：
#        ssh -L 9222:localhost:9222 root@<server>
#      然后浏览器打开 http://localhost:9222 即可看到调试页面，
#      点 "inspect" 进入 DevTools 即可手动登录 / 解风控。
#   3) 这个 Chrome 进程 **保持运行**，程序会通过 9222 端口接管它。
#
# 数据目录使用 ./data/chrome-debug，因为 Chrome 禁止对"默认目录"开远程调试。
# 默认走新版 headless（无桌面环境也能跑），CDP UI 仍可远程操作。
# 想看真实窗口（在带桌面的 Linux 上），把 HEADLESS=0 传进来。

set -euo pipefail

PORT="${PORT:-9222}"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DATA_DIR="${DATA_DIR:-$SCRIPT_DIR/data/chrome-debug}"
HEADLESS="${HEADLESS:-1}"
START_URL="${START_URL:-https://www.zhipin.com/}"

mkdir -p "$DATA_DIR"

find_chrome() {
  local cands=(
    /usr/bin/google-chrome
    /usr/bin/google-chrome-stable
    /usr/bin/chromium
    /usr/bin/chromium-browser
    /snap/bin/chromium
    /opt/google/chrome/chrome
  )
  for c in "${cands[@]}"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  for n in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$n" >/dev/null 2>&1; then
      command -v "$n"
      return
    fi
  done
  # 兜底：Playwright 自带 Chromium（含本项目内置的 .ms-playwright 目录）
  shopt -s nullglob
  for root in \
    "${PLAYWRIGHT_BROWSERS_PATH:-}" \
    "$SCRIPT_DIR/.ms-playwright" \
    "$SCRIPT_DIR/.cache/ms-playwright" \
    "$HOME/.cache/ms-playwright" \
    "$HOME/.cache/rebrowser-playwright" \
    "/ms-playwright"; do
    [ -z "$root" ] && continue
    [ -d "$root" ] || continue
    matches=( "$root"/chromium-*/chrome-linux/chrome )
    if (( ${#matches[@]} > 0 )); then
      echo "${matches[-1]}"
      return
    fi
  done
}

CHROME="$(find_chrome)"
if [ -z "$CHROME" ]; then
  cat >&2 <<'EOF'
找不到任何 Chrome / Chromium 可执行文件。
请二选一：
  1) 安装系统 Chromium：sudo dnf install -y chromium  或  apt-get install -y chromium
  2) 用 Playwright 内置版（推荐，不需要 sudo）：
        python -m playwright install chromium
        或  python -m rebrowser_playwright install chromium
EOF
  exit 1
fi

echo "[chrome-debug] binary    : $CHROME"
echo "[chrome-debug] data dir  : $DATA_DIR"
echo "[chrome-debug] port      : $PORT"
echo "[chrome-debug] headless  : $HEADLESS"
echo "[chrome-debug] start url : $START_URL"
echo
echo "远程调试入口：http://localhost:$PORT  (建议走 ssh -L $PORT:localhost:$PORT 隧道)"
echo "保持本进程运行，程序会通过 CDP 接管。Ctrl+C 可停止。"
echo

ARGS=(
  "--remote-debugging-port=$PORT"
  "--remote-allow-origins=*"
  "--user-data-dir=$DATA_DIR"
  "--no-sandbox"
  "--disable-dev-shm-usage"
  "--disable-gpu"
)
if [ "$HEADLESS" = "1" ]; then
  ARGS+=("--headless=new")
fi

ARGS+=("$START_URL")

exec "$CHROME" "${ARGS[@]}"
