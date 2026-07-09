"""DeepSeek 分析器：对职位做匹配度分析并给出求职建议。

使用 DeepSeek 的 OpenAI 兼容 Chat Completions 接口，并要求模型返回 JSON。
"""

from __future__ import annotations

from typing import Any, Dict

import httpx

from ..config import get_settings
from ._common import LLMError, parse_result
from .prompts import build_messages

_settings = get_settings()

# 向后兼容别名
DeepSeekError = LLMError


async def _chat(messages: list[dict], json_mode: bool = True) -> str:
    """调 DeepSeek chat completions，返回模型文本内容。"""
    if not _settings.deepseek_api_key:
        raise LLMError("未配置 DEEPSEEK_API_KEY，请在 .env 中设置")

    payload: Dict[str, Any] = {
        "model": _settings.deepseek_model,
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {_settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }

    url = _settings.deepseek_base_url.rstrip("/") + "/chat/completions"
    # trust_env=False 时忽略 HTTP_PROXY/HTTPS_PROXY，直连 DeepSeek
    async with httpx.AsyncClient(
        timeout=120, trust_env=_settings.deepseek_use_proxy
    ) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise LLMError(f"DeepSeek 接口错误 {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
    return data["choices"][0]["message"]["content"]


async def complete(prompt: str, json_mode: bool = False) -> str:
    """通用文本补全：单条 user 消息，返回模型原始文本。"""
    return await _chat([{"role": "user", "content": prompt}], json_mode=json_mode)


async def analyze_job(job: dict, profile: str | None = None) -> Dict[str, Any]:
    """返回 {match_score, summary, advice, raw}。"""
    profile = profile if profile is not None else _settings.candidate_profile
    messages = build_messages(profile, job)
    content = await _chat(messages, json_mode=True)
    return parse_result(content)
