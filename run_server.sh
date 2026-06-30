#!/usr/bin/env bash
# Job Hunter 守护/保活脚本（纯用户态，无需 sudo / systemd / cron）。
#
# 适用：systemd 不可用的受限容器/云主机。进程崩溃会自动重启。
#
# 用法：
#   ./run_server.sh start     # 后台启动（带自动重启循环）
#   ./run_server.sh stop      # 停止
#   ./run_server.sh restart   # 重启
#   ./run_server.sh status    # 查看状态
#   ./run_server.sh fg        # 前台运行（调试用，Ctrl+C 退出）
#
# 环境变量（可选）：
#   HOST      监听地址，默认 0.0.0.0（对外门户固定对外开放；仅本机用 HOST=127.0.0.1）
#   PORT      监听端口，默认 8000
#   APP_ROLE  部署角色：all(默认,单端口都开) / user(仅用户端) / admin(仅管理端)
#
# 用户/管理双端口隔离示例（互不干扰，各自有独立 PID/日志）：
#   APP_ROLE=user  HOST=0.0.0.0   PORT=8000 ./run_server.sh start   # 对外用户门户
#   APP_ROLE=admin HOST=127.0.0.1 PORT=8001 ./run_server.sh start   # 仅本机/Tailscale 管理后台

set -uo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8000}"
export APP_ROLE="${APP_ROLE:-all}"

# === 自动选择图形显示（DISPLAY），固化 VNC 桌面 ===
# 接管真实 Chrome 登录时需要一块“屏幕”画窗口；画到哪块由 DISPLAY 决定。
# SSH 的 X11 转发会塞进一个形如 localhost:10.0 的 DISPLAY，它随 SSH 会话失效，
# 一旦继承到服务里就会让 Chrome 报 “Missing X server”。这里忽略这种转发地址，
# 优先钉死常驻的 VNC(Xvnc)/本地 X 桌面；都探测不到则留空走 headless（可移植）。
pick_display() {
  # 1) 显式指定优先（想强制用某块屏：JOB_HUNTER_DISPLAY=:1 ./run_server.sh ...）
  if [ -n "${JOB_HUNTER_DISPLAY:-}" ]; then echo "$JOB_HUNTER_DISPLAY"; return; fi
  # 2) 已是本地真实 DISPLAY(形如 :0/:1，而非 SSH 的 localhost:*) 直接沿用
  case "${DISPLAY:-}" in
    :*) echo "$DISPLAY"; return ;;
  esac
  local me vnc_disp s n owner
  me="$(id -un)"
  # 3) 优先用“当前用户自己的”Xvnc 桌面：X 授权(XAUTHORITY)只对自己的显示有效，
  #    用别人(如 root)的显示会 “No protocol specified / Missing X server”起不来。
  vnc_disp="$(ps -o user= -o args= -C Xvnc 2>/dev/null \
    | awk -v u="$me" '$1==u {for(i=1;i<=NF;i++) if($i ~ /^:[0-9]+$/){print $i; exit}}')"
  if [ -n "$vnc_disp" ]; then echo "$vnc_disp"; return; fi
  # 4) 退而求其次：/tmp/.X11-unix 下“当前用户拥有”的那块本地 X
  if [ -d /tmp/.X11-unix ]; then
    for s in /tmp/.X11-unix/X*; do
      [ -e "$s" ] || continue
      owner="$(stat -c '%U' "$s" 2>/dev/null)"
      [ "$owner" = "$me" ] || continue
      n="${s##*/X}"
      echo ":$n"; return
    done
  fi
  echo ""  # 没有可用图形桌面 → headless
}

JH_DISP="$(pick_display)"
if [ -n "$JH_DISP" ]; then
  export DISPLAY="$JH_DISP"
  # X 授权文件：root 跑 Xvnc 通常在 ~/.Xauthority；已设置则不覆盖。
  if [ -z "${XAUTHORITY:-}" ] && [ -f "$HOME/.Xauthority" ]; then
    export XAUTHORITY="$HOME/.Xauthority"
  fi
  echo "图形显示: DISPLAY=$DISPLAY（浏览器登录窗口将画到此桌面，可用 VNC 查看/操作）"
else
  unset DISPLAY
  echo "图形显示: 无 → 浏览器走 headless（用看屏查看器登录）"
fi

# 实例标识：让不同角色/端口的进程使用独立的 PID/日志文件，可同时常驻。
INSTANCE="${APP_ROLE}-${PORT}"

DATA_DIR="$SCRIPT_DIR/data"
mkdir -p "$DATA_DIR"
SUPERVISOR_PID_FILE="$DATA_DIR/supervisor-${INSTANCE}.pid"
APP_PID_FILE="$DATA_DIR/server-${INSTANCE}.pid"
OUT_LOG="$SCRIPT_DIR/server_out-${INSTANCE}.log"
ERR_LOG="$SCRIPT_DIR/server_err-${INSTANCE}.log"

pick_python() {
  if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "$SCRIPT_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo ""
  fi
}

is_running() {
  local f="$1"
  [ -f "$f" ] || return 1
  local pid
  pid="$(cat "$f" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start() {
  if is_running "$SUPERVISOR_PID_FILE"; then
    echo "已在运行（supervisor PID $(cat "$SUPERVISOR_PID_FILE")）。如需重启用：$0 restart"
    return 0
  fi
  local py; py="$(pick_python)"
  if [ -z "$py" ]; then
    echo "找不到 python。请先建好 venv：python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt" >&2
    exit 1
  fi
  echo "使用 Python: $py"
  echo "角色: $APP_ROLE  监听: $HOST:$PORT"
  # 启动后台 supervisor 循环：崩溃后退避重启
  (
    echo $BASHPID > "$SUPERVISOR_PID_FILE"
    backoff=2
    while true; do
      echo "[$(date '+%F %T')] starting uvicorn ($HOST:$PORT)..." >> "$OUT_LOG"
      "$py" -m app.main >> "$OUT_LOG" 2>> "$ERR_LOG" &
      app_pid=$!
      echo "$app_pid" > "$APP_PID_FILE"

      # 健康看门狗：进程还活着、但端口对 /api/health 不再响应（事件循环被卡死，
      # supervisor 的 wait 检测不到），连续探测失败则强杀该进程，让外层循环重启它。
      (
        sleep 45  # 给启动/迁移留足时间，避免误杀
        fails=0
        while kill -0 "$app_pid" 2>/dev/null; do
          if curl -fs -m 8 -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
            fails=0
          else
            fails=$(( fails + 1 ))
            echo "[$(date '+%F %T')] [watchdog] /api/health 探测失败 ($fails/3)" >> "$ERR_LOG"
            if [ "$fails" -ge 3 ]; then
              echo "[$(date '+%F %T')] [watchdog] 判定卡死，强杀 app PID $app_pid 触发重启" >> "$ERR_LOG"
              kill -9 "$app_pid" 2>/dev/null
              break
            fi
          fi
          sleep 20
        done
      ) &
      wd_pid=$!

      wait "$app_pid"
      code=$?
      kill "$wd_pid" 2>/dev/null  # 进程已退出，收掉看门狗
      echo "[$(date '+%F %T')] uvicorn exited code=$code, restarting in ${backoff}s" >> "$ERR_LOG"
      # 正常停止（SIGTERM=143 / 0）则退出循环
      if [ "$code" = "0" ] || [ "$code" = "143" ]; then
        echo "[$(date '+%F %T')] clean exit, supervisor stopping" >> "$OUT_LOG"
        break
      fi
      sleep "$backoff"
      backoff=$(( backoff < 60 ? backoff * 2 : 60 ))
    done
    rm -f "$SUPERVISOR_PID_FILE" "$APP_PID_FILE"
  ) &
  disown
  sleep 2
  if is_running "$SUPERVISOR_PID_FILE"; then
    echo "已后台启动。supervisor PID $(cat "$SUPERVISOR_PID_FILE")，app PID $(cat "$APP_PID_FILE" 2>/dev/null)"
    echo "日志: $OUT_LOG / $ERR_LOG"
  else
    echo "启动失败，请看 $ERR_LOG"; tail -n 20 "$ERR_LOG" 2>/dev/null
    exit 1
  fi
}

stop() {
  # 先杀 supervisor（防止它又把 app 拉起来），再杀 app
  if is_running "$SUPERVISOR_PID_FILE"; then
    kill "$(cat "$SUPERVISOR_PID_FILE")" 2>/dev/null
  fi
  if is_running "$APP_PID_FILE"; then
    kill "$(cat "$APP_PID_FILE")" 2>/dev/null
    sleep 1
    is_running "$APP_PID_FILE" && kill -9 "$(cat "$APP_PID_FILE")" 2>/dev/null
  fi
  rm -f "$SUPERVISOR_PID_FILE" "$APP_PID_FILE"
  echo "已停止。"
}

status() {
  if is_running "$SUPERVISOR_PID_FILE"; then
    echo "supervisor: 运行中 (PID $(cat "$SUPERVISOR_PID_FILE"))"
  else
    echo "supervisor: 未运行"
  fi
  if is_running "$APP_PID_FILE"; then
    echo "app:        运行中 (PID $(cat "$APP_PID_FILE"))  $HOST:$PORT"
  else
    echo "app:        未运行"
  fi
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  fg)
    py="$(pick_python)"
    [ -z "$py" ] && { echo "找不到 python"; exit 1; }
    exec "$py" -m app.main ;;
  *)
    echo "用法: $0 {start|stop|restart|status|fg}"
    exit 1 ;;
esac
