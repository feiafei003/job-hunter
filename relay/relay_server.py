"""Cursor LLM 中转服务（部署在能直连 Cursor 云端的服务器上，如阿里云 ECS）。

背景：job-hunter 所在的内网机器(244)直连 api2.cursor.sh 被防火墙挡死；公司代理
只能转发 HTTP/1.1，而 Cursor 推理端点要求 HTTP/2(否则回 464)，所以本机用不了 Cursor。
本服务跑在一台能"直连"Cursor 的服务器上，对外暴露一个极简 HTTPS 接口：

    POST /complete   Header: Authorization: Bearer <RELAY_TOKEN>
                     Body:   {"prompt": "...", "model": "composer-2.5"}
                     Resp:   {"status":"ok","text":"..."}  或
                             {"status":"error","error":"...","detail":"..."}

job-hunter(244) 经公司代理把请求 POST 过来，本服务用本地 cursor-sdk 直连 Cursor 执行。

环境变量：
    RELAY_TOKEN          必填，鉴权 token（与 244 端 cursor_relay_token 一致）
    CURSOR_API_KEY       必填，Cursor API Key
    RELAY_DEFAULT_MODEL  可选，默认 composer-2.5（当请求 model 为空/auto 时使用）
    RELAY_CWD            可选，agent 工作目录，默认 /tmp/cursor_relay_ws
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "").strip()
CURSOR_API_KEY = os.environ.get("CURSOR_API_KEY", "").strip()
# composer-2.5 在部分账号的 SDK 本地 agent 下会返回空 error；claude-sonnet-4-5 稳定可用。
DEFAULT_MODEL = os.environ.get("RELAY_DEFAULT_MODEL", "claude-sonnet-4-5").strip() or "claude-sonnet-4-5"
RELAY_CWD = os.environ.get("RELAY_CWD", "/tmp/cursor_relay_ws").strip()

app = FastAPI(title="Cursor LLM Relay", version="1.0")


class CompleteRequest(BaseModel):
    prompt: str
    model: str | None = None


def _resolve_model(model: str | None) -> str:
    m = (model or "").strip().lower()
    if not m or m in {"auto", "default"}:
        return DEFAULT_MODEL
    return model.strip()


def _run_cursor(prompt: str, model: str) -> dict:
    """在本机用 cursor-sdk 直连 Cursor 执行一次性 prompt，返回 {status,text,...}。"""
    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    os.makedirs(RELAY_CWD, exist_ok=True)
    result = Agent.prompt(
        prompt,
        AgentOptions(
            api_key=CURSOR_API_KEY,
            model=model,
            local=LocalAgentOptions(cwd=RELAY_CWD),
        ),
    )
    status = getattr(result, "status", None) or "finished"
    text = getattr(result, "result", "") or ""
    detail = ""
    try:
        for attr in ("error", "error_message", "message", "reason"):
            v = getattr(result, attr, None)
            if v:
                detail += f"{attr}={v}; "
        detail += "repr=" + repr(result)[:600]
    except Exception:
        pass
    return {"status": status, "text": text, "detail": detail}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": DEFAULT_MODEL, "has_key": bool(CURSOR_API_KEY)}


@app.post("/complete")
def complete(req: CompleteRequest, authorization: str = Header(default="")) -> dict:
    if not RELAY_TOKEN:
        raise HTTPException(status_code=500, detail="服务端未配置 RELAY_TOKEN")
    if authorization.removeprefix("Bearer ").strip() != RELAY_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not CURSOR_API_KEY:
        raise HTTPException(status_code=500, detail="服务端未配置 CURSOR_API_KEY")
    if not (req.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt 为空")

    model = _resolve_model(req.model)
    try:
        out = _run_cursor(req.prompt, model)
    except Exception as exc:  # noqa: BLE001
        import traceback

        return {
            "status": "error",
            "error": repr(exc),
            "detail": traceback.format_exc()[:1500],
        }

    if out["status"] in ("error", "startup_error"):
        return {
            "status": "error",
            "error": out.get("text", "") or out["status"],
            "detail": out.get("detail", ""),
        }
    return {"status": "ok", "text": out["text"], "model": model}
