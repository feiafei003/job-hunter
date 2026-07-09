"""分析后端共用：解析模型返回为统一结构。"""

from __future__ import annotations

import json
from typing import Any, Dict


class LLMError(RuntimeError):
    pass


def _extract_json(content: str) -> Dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


def _as_text(val: Any) -> str:
    """模型有时把分点内容返回成数组/对象，统一拍平成多行文本。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, (list, tuple)):
        return "\n".join(f"- {_as_text(x)}" if not str(x).startswith("-") else str(x) for x in val if x)
    if isinstance(val, dict):
        return "\n".join(f"- {k}: {_as_text(v)}" for k, v in val.items() if v)
    return str(val)


def parse_result(content: str) -> Dict[str, Any]:
    """把模型文本解析为 {match_score, summary, advice, skills_to_learn, resume_tips, raw}。"""
    try:
        parsed = _extract_json(content)
    except Exception:
        return {
            "match_score": 0,
            "summary": "模型返回无法解析为 JSON",
            "advice": content[:2000],
            "skills_to_learn": "",
            "resume_tips": "",
            "raw": content,
        }

    score = parsed.get("match_score", 0)
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = 0

    return {
        "match_score": score,
        "summary": _as_text(parsed.get("summary", ""))[:1000],
        "advice": _as_text(parsed.get("advice", ""))[:4000],
        "skills_to_learn": _as_text(parsed.get("skills_to_learn", ""))[:3000],
        "resume_tips": _as_text(parsed.get("resume_tips", ""))[:3000],
        "raw": content,
    }
