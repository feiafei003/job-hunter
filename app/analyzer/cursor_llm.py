"""基于 Cursor SDK 的分析后端。

通过 Cursor SDK 的一次性 Agent.prompt 调用 Cursor 模型（如 composer-2.5）生成分析。
Agent.prompt 为同步调用，这里用线程池包装成异步，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

from ..config import get_settings
from ._common import LLMError, parse_result
from .prompts import build_single_prompt

_settings = get_settings()
log = logging.getLogger("jobhunter.analyzer")

_WORKER = Path(__file__).with_name("cursor_worker.py")


def _api_key() -> str:
    return _settings.cursor_api_key or os.environ.get("CURSOR_API_KEY", "")


def _relay_enabled() -> bool:
    return bool((_settings.cursor_relay_url or "").strip())


def _run_via_relay(prompt: str) -> str:
    """把提示词转发到一台能直连 Cursor 的中转服务器执行。

    本机(内网/容器)直连 Cursor 会被防火墙挡死，且公司代理只能转发 HTTP/1.1、
    而 Cursor 推理端点要求 HTTP/2(会回 464)。因此改为：本机 → 公司代理 → 中转服务器
    (能直连 Cursor) → Cursor。中转服务器跑的就是 relay/relay_server.py。
    """
    import httpx

    url = (_settings.cursor_relay_url or "").strip().rstrip("/") + "/complete"
    token = (_settings.cursor_relay_token or "").strip()
    # 到中转服务器(公网 IP)的连接需要经公司代理出网；本地直连会被防火墙挡。
    proxy = (_settings.cursor_proxy_url or "").strip() or None

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(
            proxy=proxy,
            verify=_settings.cursor_relay_verify_tls,
            timeout=httpx.Timeout(240.0, connect=20.0),
            trust_env=False,
        ) as client:
            resp = client.post(
                url,
                headers=headers,
                json={"prompt": prompt, "model": _settings.cursor_model},
            )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"连接 Cursor 中转服务失败: {exc}") from exc

    if resp.status_code == 401:
        raise LLMError("Cursor 中转服务鉴权失败(token 不匹配)")
    if resp.status_code >= 400:
        raise LLMError(
            f"Cursor 中转服务返回 {resp.status_code}: {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Cursor 中转服务响应非 JSON: {resp.text[:200]}") from exc

    if data.get("status") != "ok":
        raise LLMError(
            f"Cursor 中转执行失败: {str(data.get('error', ''))[:200]} | "
            f"{str(data.get('detail', ''))[:400]}"
        )
    return data.get("text", "")


def _run_prompt(prompt: str) -> str:
    """在独立子进程里调用 Cursor SDK，规避 Windows asyncio 线程问题。

    若配置了 cursor_relay_url，则改走中转服务器（本机连不上 Cursor 云端时）。
    """
    if _relay_enabled():
        return _run_via_relay(prompt)

    workspace = _settings.data_path / "llm_workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    tmpdir = tempfile.mkdtemp(prefix="cursor_llm_")
    in_path = os.path.join(tmpdir, "in.json")
    out_path = os.path.join(tmpdir, "out.json")
    with open(in_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "prompt": prompt,
                "api_key": _api_key(),
                "model": _settings.cursor_model,
                "cwd": str(workspace),
            },
            f,
            ensure_ascii=False,
        )

    child_env = dict(os.environ)
    # httpx 不支持 socks 代理(无 socksio 时会抛 Unknown scheme)，而 Python→本地 bridge
    # 走 localhost 本就不该走代理；统一清掉 socks/all_proxy，避免本地连接被错误代理。
    for k in (
        "all_proxy", "ALL_PROXY",
        "socks_proxy", "SOCKS_PROXY",
        "socks5_proxy", "SOCKS5_PROXY",
    ):
        child_env.pop(k, None)

    if _settings.cursor_use_proxy:
        # 经系统代理访问 Cursor 云端（很多内网/容器只有走公司代理才能出网；
        # 直连会被防火墙挡死，SDK 会包装成一个没头没尾的 500）。
        # - 本地 bridge 连接(localhost)必须绕过代理；
        # - Node bridge 默认不读 HTTP_PROXY，需 NODE_USE_ENV_PROXY=1 才会用环境代理(Node>=20)。
        proxy_url = (_settings.cursor_proxy_url or "").strip()
        if proxy_url:
            child_env["HTTP_PROXY"] = child_env["http_proxy"] = proxy_url
            child_env["HTTPS_PROXY"] = child_env["https_proxy"] = proxy_url
        no_proxy = "localhost,127.0.0.1,::1"
        child_env["NO_PROXY"] = child_env["no_proxy"] = no_proxy
        child_env["NODE_USE_ENV_PROXY"] = "1"
        # 关键：SDK 默认对 https 走 HTTP/2(node:http2)，而 node:http2 不认转发代理，
        # 会直连 → 被防火墙挡死 → 超时。强制 HTTP/1.1 后才走 undici，能读环境代理。
        child_env["CURSOR_USE_HTTP1"] = "true"
    else:
        # 直连：彻底绕过系统代理（适用于代理会拦截 Cursor 的网络，如部分 Windows 环境）。
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            child_env.pop(k, None)
        child_env["NO_PROXY"] = child_env["no_proxy"] = "*"

    proc = subprocess.run(
        [sys.executable, str(_WORKER), in_path, out_path],
        capture_output=True,
        text=True,
        timeout=240,
        env=child_env,
    )
    if not os.path.exists(out_path):
        raise LLMError(
            f"Cursor worker 无输出 (returncode={proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '')[:300]}"
        )
    with open(out_path, "r", encoding="utf-8") as f:
        env = json.load(f)

    status = env.get("status")
    detail = env.get("detail", "")
    if status == "startup_error":
        raise LLMError(
            f"Cursor agent 启动失败: {env.get('error', '')[:300]} | {detail[:400]}"
        )
    if status == "error":
        raise LLMError(
            f"Cursor agent 运行失败: {env.get('text', '')[:200]} | {detail[:500]}"
        )
    return env.get("text", "")


_RETRYABLE_HINTS = ("internal", "500", "timeout", "timed out", "unavailable", "503", "502")
_MAX_ATTEMPTS = 3


def _is_retryable(msg: str) -> bool:
    m = msg.lower()
    return any(h in m for h in _RETRYABLE_HINTS)


async def _run_with_retry(prompt: str) -> str:
    """带重试地跑一段提示词，返回模型原始文本。"""
    last_err: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return await asyncio.to_thread(_run_prompt, prompt)
        except LLMError as exc:
            last_err = exc
            if attempt < _MAX_ATTEMPTS and _is_retryable(str(exc)):
                log.warning("Cursor 调用第 %d 次失败(可重试): %s", attempt, str(exc)[:160])
                await asyncio.sleep(2 * attempt)
                continue
            raise
        except Exception as exc:  # noqa: BLE001  (含 CursorAgentError)
            last_err = LLMError(f"Cursor agent 启动失败: {exc}")
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
                continue
            raise last_err from exc
    assert last_err is not None
    raise last_err


async def complete(prompt: str, json_mode: bool = False) -> str:
    """通用文本补全：返回模型原始文本（json_mode 仅为接口一致，Cursor 由提示词约束输出）。"""
    if not _relay_enabled() and not _api_key():
        raise LLMError("未配置 CURSOR_API_KEY，请在 .env 中设置")
    return await _run_with_retry(prompt)


async def analyze_job(job: dict, profile: str | None = None) -> Dict[str, Any]:
    if not _relay_enabled() and not _api_key():
        raise LLMError("未配置 CURSOR_API_KEY，请在 .env 中设置")

    profile = profile if profile is not None else _settings.candidate_profile
    prompt = build_single_prompt(profile, job)

    last_err: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            content = await asyncio.to_thread(_run_prompt, prompt)
            return parse_result(content)
        except LLMError as exc:
            last_err = exc
            if attempt < _MAX_ATTEMPTS and _is_retryable(str(exc)):
                log.warning(
                    "Cursor 分析第 %d 次失败(可重试): %s", attempt, str(exc)[:160]
                )
                await asyncio.sleep(2 * attempt)
                continue
            raise
        except Exception as exc:  # noqa: BLE001  (含 CursorAgentError)
            last_err = LLMError(f"Cursor agent 启动失败: {exc}")
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(2 * attempt)
                continue
            raise last_err from exc

    assert last_err is not None
    raise last_err
