"""独立子进程：调用 Cursor 模型生成分析。

实现方式：直接调用 cursor-agent 命令行（CLI），而非 Python cursor_sdk。
原因：经"中国出口代理"访问 Cursor 时，Python SDK 的本地 bridge 走 httpx 会遇到
SOCKS 代理变量、HTTP/2 隧道、bridge 超时等一连串问题，且非交互下常以
status=error 返回空结果；而 cursor-agent CLI 经同一代理稳定可用（-f 跳过工作区
信任）。CLI 的网络出口由父进程预设的 HTTP(S)_PROXY / NODE_USE_ENV_PROXY 控制。

用法：python cursor_worker.py <input_json_path> <output_json_path>
输入 JSON：{prompt, api_key, model, cwd}
输出 JSON：{status, text, error?, detail?}
"""

import json
import os
import shutil
import subprocess
import sys


def _find_cursor_agent() -> str | None:
    """定位 cursor-agent 可执行文件。"""
    p = shutil.which("cursor-agent")
    if p:
        return p
    for cand in (
        os.path.expanduser("~/.local/bin/cursor-agent"),
        "/usr/local/bin/cursor-agent",
        "/root/.local/bin/cursor-agent",
    ):
        if os.path.exists(cand):
            return cand
    return None


def main() -> None:
    in_path, out_path = sys.argv[1], sys.argv[2]
    with open(in_path, "r", encoding="utf-8") as f:
        inp = json.load(f)

    env = {"status": "startup_error", "text": "", "error": "", "detail": ""}
    try:
        agent = _find_cursor_agent()
        if not agent:
            raise RuntimeError(
                "未找到 cursor-agent 命令。请先安装：curl -fsS https://cursor.com/install | bash"
            )

        cwd = inp.get("cwd") or os.getcwd()
        os.makedirs(cwd, exist_ok=True)

        cmd = [
            agent,
            "--api-key", inp["api_key"],
            "--model", inp["model"],
            "-f",                       # 等价 --yolo/--trust，跳过工作区信任/命令批准
            "--output-format", "text",
            "-p", inp["prompt"],        # -p 为开关；prompt 作为位置参数
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=220,
            cwd=cwd,
            env=os.environ,  # 继承父进程已设好的代理等环境变量
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        # 地区/模型不可用等错误通常出现在输出里
        low = (out + "\n" + err).lower()
        if proc.returncode != 0 or not out:
            if "not supported in your region" in low or "model provider" in low:
                env = {
                    "status": "error",
                    "text": "",
                    "error": "模型在当前出口地区不可用（Claude 在中国区被禁）。请改用 "
                    "composer-2.5 / gpt-5.5，或让出网走非中国区。",
                    "detail": (out + " | " + err)[:600],
                }
            else:
                env = {
                    "status": "error",
                    "text": out,
                    "error": f"cursor-agent 退出码 {proc.returncode}",
                    "detail": (err or out)[:800],
                }
        else:
            env = {"status": "finished", "text": out, "error": "", "detail": ""}
    except Exception as exc:  # noqa: BLE001
        import traceback

        env = {
            "status": "startup_error",
            "text": "",
            "error": repr(exc),
            "detail": traceback.format_exc()[:1500],
        }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
