"""分析后端分发：按配置选择 Cursor 或 DeepSeek。"""

from __future__ import annotations

from typing import Any, Dict

from ..config import get_settings
from ._common import LLMError


async def analyze_job(job: dict, profile: str | None = None) -> Dict[str, Any]:
    provider = (get_settings().llm_provider or "cursor").lower()
    if provider == "deepseek":
        from .deepseek import analyze_job as _impl
    else:
        from .cursor_llm import analyze_job as _impl
    return await _impl(job, profile=profile)


async def complete(prompt: str, json_mode: bool = False) -> str:
    """通用文本补全：按配置选择后端，返回模型原始文本。"""
    provider = (get_settings().llm_provider or "cursor").lower()
    if provider == "deepseek":
        from .deepseek import complete as _impl
    else:
        from .cursor_llm import complete as _impl
    return await _impl(prompt, json_mode=json_mode)


def provider_status() -> dict:
    """供 /health 展示当前后端及是否配置。"""
    import os

    s = get_settings()
    provider = (s.llm_provider or "cursor").lower()
    if provider == "deepseek":
        configured = bool(s.deepseek_api_key)
    else:
        configured = bool(s.cursor_api_key or os.environ.get("CURSOR_API_KEY"))
    return {"provider": provider, "configured": configured}


__all__ = ["analyze_job", "complete", "provider_status", "LLMError"]
