"""门户 AI 助手：基于 JSON 协议的有界 ReAct 工具循环。

由于统一入口 complete() 同时支持 Cursor（子进程）与 DeepSeek，这里采用
「模型每轮只输出一个 JSON 指令」的方式：后端解析 action、执行对应工具、把结果作为
observation 回喂给模型，最多 4 步，最后产出自然语言回复。

工具（action）：
    reply                 结束并给出自然语言回复
    list_subscriptions    列出当前用户的订阅
    create_subscription   新建订阅
    update_subscription   修改订阅（按 id，校验归属）
    delete_subscription   删除订阅（要求对话里已口头确认，按 id，校验归属）
    recommend_keywords    按岗位/画像推荐关键词（静态预设优先，否则 AI 生成）

对话不落库，历史由前端在内存/本地存储维护后随请求带上。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Optional

from sqlmodel import select

from .analyzer import LLMError, analyze_job as _analyze_job_llm, complete
from .analyzer._common import _extract_json
from .db import session_scope
from .keyword_presets import all_presets, match_preset
from .models import SendSlot, Site, Subscription
from .scheduler import scheduler_service
from .services import render_profile_text

log = logging.getLogger("jobhunter.assistant")

_VALID_SITES = {s.value for s in Site}
_VALID_SLOTS = {s.value for s in SendSlot}
_DEFAULT_SLOT = SendSlot.daily_09.value
_MAX_STEPS = 4

_SLOT_NAMES = {
    "daily_09": "每天 10:00",
    "daily_21": "每天 21:00",
    "weekday_09": "工作日 10:00",
    "weekly_mon_09": "每周一 10:00",
}


# ---------------- 参数清洗 ----------------
def _coerce_int(val: Any) -> int:
    """把 "25" / "25k" / 25 / 25.0 统一成非负整数（k 单位）。"""
    if val is None or val == "":
        return 0
    if isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return max(0, int(val))
    m = re.search(r"\d+", str(val))
    return max(0, int(m.group())) if m else 0


def _as_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return ", ".join(str(x).strip() for x in val if str(x).strip())
    return str(val).strip()


def _valid_slots(slots: Any) -> list[str]:
    if isinstance(slots, str):
        slots = [s.strip() for s in slots.split(",")]
    if not isinstance(slots, (list, tuple)):
        slots = []
    out = [s for s in slots if s in _VALID_SLOTS]
    return out or [_DEFAULT_SLOT]


def _valid_sites(sites: Any) -> list[str]:
    if isinstance(sites, str):
        sites = [s.strip() for s in sites.split(",")]
    if not isinstance(sites, (list, tuple)):
        sites = []
    # 非法站点直接丢弃；留空表示「全部可用站点」
    return [s for s in sites if s in _VALID_SITES]


def _clean_sub_args(args: dict) -> dict:
    args = args or {}
    name = _as_str(args.get("name"))
    keywords = _as_str(args.get("keywords"))
    location = _as_str(args.get("location"))
    if not name:
        name = (keywords or "订阅") + (f" · {location}" if location else "")
    return {
        "name": name[:80],
        "sites": _valid_sites(args.get("sites")),
        "keywords": keywords,
        "job_type": _as_str(args.get("job_type")),
        "location": location,
        "salary_min": _coerce_int(args.get("salary_min")),
        "salary_max": _coerce_int(args.get("salary_max")),
        "send_slots": _valid_slots(args.get("send_slots")),
        "min_score": max(0, min(100, _coerce_int(args.get("min_score")))),
        "max_jobs": max(1, min(50, _coerce_int(args.get("max_jobs")) or 10)),
    }


# ---------------- 订阅快照 ----------------
def _subs_snapshot(user_id: int) -> list[dict]:
    with session_scope() as session:
        subs = list(
            session.exec(
                select(Subscription).where(Subscription.user_id == user_id)
            ).all()
        )
    out = []
    for s in subs:
        out.append(
            {
                "id": s.id,
                "name": s.name or "(未命名)",
                "keywords": s.keywords,
                "sites": [x for x in (s.sites or "").split(",") if x] or ["全部"],
                "location": s.location,
                "salary_min": s.salary_min,
                "salary_max": s.salary_max,
                "send_slots": [x for x in (s.send_slots or "").split(",") if x],
                "min_score": s.min_score,
                "max_jobs": s.max_jobs,
                "enabled": s.enabled,
            }
        )
    return out


def _load_job(job_id: Optional[int]) -> Optional[dict]:
    if not job_id:
        return None
    from .models import JobPosting

    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            return None
        return {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "salary": job.salary,
            "location": job.location,
            "experience": job.experience,
            "education": job.education,
            "tags": job.tags,
            "description": (job.description or "")[:1500],
        }


# ---------------- 关键词推荐（被工具与独立接口共用） ----------------
_RECO_PROMPT = """你是一名资深 IT 招聘顾问。请根据【求职者画像】{extra}推荐适合订阅的职位关键词。
严格输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记）：
{{
  "keywords": ["关键词1", "关键词2", "..."],
  "sites": ["从 {sites} 里挑选若干，国内岗位优先 boss/zhilian/liepin/job51"],
  "rationale": "一句话说明为什么这样推荐"
}}
keywords 控制在 3-8 个，由具体到宽泛；不要编造画像里没有的方向。

# 求职者画像
{profile}
"""


async def recommend_keywords(profile_text: str, query: str = "") -> dict:
    """推荐关键词：命中静态预设则直接用，否则按画像让模型生成。

    返回 {keywords: [...], sites: [...], rationale: str, source: preset|ai}。
    """
    hint = (query or "").strip()
    preset = match_preset(hint) if hint else None
    if preset is None and profile_text:
        # 用画像里的目标职位/技能再试一次静态匹配
        preset = match_preset(profile_text)
    if preset is not None:
        return {
            "keywords": preset["keywords"],
            "sites": preset["suggest_sites"],
            "rationale": f"按「{preset['category']}」岗位大类推荐",
            "source": "preset",
        }

    extra = f"和你想了解的方向「{hint}」" if hint else ""
    prompt = _RECO_PROMPT.format(
        extra=extra,
        sites="/".join(sorted(_VALID_SITES)),
        profile=(profile_text or "（求职者未填写画像）").strip(),
    )
    content = await complete(prompt, json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能生成关键词推荐，请稍后重试") from exc
    keywords = parsed.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in re.split(r"[,，、]", keywords) if k.strip()]
    sites = _valid_sites(parsed.get("sites"))
    return {
        "keywords": [str(k).strip() for k in keywords if str(k).strip()][:8],
        "sites": sites,
        "rationale": _as_str(parsed.get("rationale")),
        "source": "ai",
    }


# ---------------- 简历/职位分析（被工具复用） ----------------
_RESUME_PROMPT = """你是一名资深 IT 职业顾问。请基于【求职者画像/简历】给出专业、可执行的分析。
严格输出一个 JSON 对象（不要任何额外文字或 Markdown 代码块标记）：
{{
  "summary": "一句话总体评价",
  "strengths": ["亮点1", "亮点2", "..."],
  "gaps": ["短板/缺口1", "..."],
  "suggestions": ["可执行的改进建议1", "..."],
  "target_roles": ["适合冲刺的目标岗位1", "..."]
}}
不要编造画像里没有的信息；信息不足时在 summary 里说明并建议补充哪些内容。

# 求职者画像/简历
{profile}
"""


async def analyze_resume(profile_text: str) -> dict:
    """分析求职者画像/简历，返回 {summary, strengths, gaps, suggestions, target_roles}。"""
    profile_text = (profile_text or "").strip()
    if not profile_text:
        return {
            "ok": False,
            "error": "你还没有填写画像/简历，请先在「我的画像」里补充或上传简历后再分析。",
        }
    content = await complete(_RESUME_PROMPT.format(profile=profile_text), json_mode=True)
    try:
        parsed = _extract_json(content)
    except Exception as exc:  # noqa: BLE001
        raise LLMError("未能生成简历分析，请稍后重试") from exc
    return {
        "ok": True,
        "summary": _as_str(parsed.get("summary")),
        "strengths": parsed.get("strengths") or [],
        "gaps": parsed.get("gaps") or [],
        "suggestions": parsed.get("suggestions") or [],
        "target_roles": parsed.get("target_roles") or [],
    }


async def analyze_job_for_user(job: Optional[dict], profile_text: str) -> dict:
    """对上下文职位做结构化匹配分析（match_score/summary/advice/...）。"""
    if not job:
        return {
            "ok": False,
            "error": "没有指定职位。请在职位卡片上点「问 AI」，或告诉我想分析哪条职位。",
        }
    try:
        res = await _analyze_job_llm(job, profile=profile_text or None)
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
    res = dict(res or {})
    res["ok"] = True
    res["job_title"] = job.get("title")
    res["company"] = job.get("company")
    return res


# ---------------- 工具实现（写操作均校验归属） ----------------
def _tool_create(user, args: dict) -> dict:
    data = _clean_sub_args(args)
    token = secrets.token_urlsafe(16)
    with session_scope() as session:
        sub = Subscription(
            user_id=user.id,
            email=user.email,
            name=data["name"],
            sites=",".join(data["sites"]),
            keywords=data["keywords"],
            job_type=data["job_type"],
            location=data["location"],
            salary_min=data["salary_min"],
            salary_max=data["salary_max"],
            profile_json="",  # 复用「我的画像」做分析
            send_slots=",".join(data["send_slots"]),
            min_score=data["min_score"],
            max_jobs=data["max_jobs"],
            include_analysis=True,
            enabled=True,
            manage_token=token,
        )
        session.add(sub)
        session.flush()
        sid = sub.id
    scheduler_service.reload_jobs()
    return {"ok": True, "id": sid, "created": data}


def _tool_update(user, args: dict) -> dict:
    args = args or {}
    sid = _coerce_int(args.get("id"))
    if not sid:
        return {"ok": False, "error": "缺少要修改的订阅 id"}
    with session_scope() as session:
        sub = session.get(Subscription, sid)
        if sub is None or sub.user_id != user.id:
            return {"ok": False, "error": f"订阅 {sid} 不存在或不属于你"}
        if "name" in args:
            sub.name = _as_str(args.get("name"))[:80] or sub.name
        if "keywords" in args:
            sub.keywords = _as_str(args.get("keywords"))
        if "job_type" in args:
            sub.job_type = _as_str(args.get("job_type"))
        if "location" in args:
            sub.location = _as_str(args.get("location"))
        if "sites" in args:
            sub.sites = ",".join(_valid_sites(args.get("sites")))
        if "salary_min" in args:
            sub.salary_min = _coerce_int(args.get("salary_min"))
        if "salary_max" in args:
            sub.salary_max = _coerce_int(args.get("salary_max"))
        if "send_slots" in args:
            sub.send_slots = ",".join(_valid_slots(args.get("send_slots")))
        if "min_score" in args:
            sub.min_score = max(0, min(100, _coerce_int(args.get("min_score"))))
        if "max_jobs" in args:
            sub.max_jobs = max(1, min(50, _coerce_int(args.get("max_jobs")) or 10))
        if "enabled" in args and isinstance(args.get("enabled"), bool):
            sub.enabled = args["enabled"]
        session.add(sub)
        snap = {
            "id": sub.id,
            "name": sub.name,
            "keywords": sub.keywords,
            "location": sub.location,
            "salary_min": sub.salary_min,
            "salary_max": sub.salary_max,
            "enabled": sub.enabled,
        }
    scheduler_service.reload_jobs()
    return {"ok": True, "updated": snap}


def _tool_delete(user, args: dict) -> dict:
    sid = _coerce_int((args or {}).get("id"))
    if not sid:
        return {"ok": False, "error": "缺少要删除的订阅 id"}
    with session_scope() as session:
        sub = session.get(Subscription, sid)
        if sub is None or sub.user_id != user.id:
            return {"ok": False, "error": f"订阅 {sid} 不存在或不属于你"}
        name = sub.name
        session.delete(sub)
    scheduler_service.reload_jobs()
    return {"ok": True, "deleted_id": sid, "name": name}


# ---------------- 系统提示 ----------------
def build_system_prompt(
    user, subs_summary: list[dict], profile_text: str, job: Optional[dict]
) -> str:
    sites_desc = "、".join(f"{s.value}" for s in Site)
    slots_desc = "；".join(f"{k}={v}" for k, v in _SLOT_NAMES.items())
    subs_lines = (
        "\n".join(
            f"- id={s['id']} 名称「{s['name']}」 关键词[{s['keywords'] or '不限'}] "
            f"站点{('/'.join(s['sites']))} 地点[{s['location'] or '不限'}] "
            f"{'已启用' if s['enabled'] else '已停用'}"
            for s in subs_summary
        )
        or "（暂无订阅）"
    )
    job_block = ""
    if job:
        job_block = (
            "\n# 当前正在讨论的职位（用户在该职位卡片点了「问 AI」）\n"
            f"- 职位: {job['title']}\n- 公司: {job['company']}\n"
            f"- 薪资: {job['salary'] or '未提供'}\n- 地点: {job['location'] or '未提供'}\n"
            f"- 经验: {job['experience'] or '未提供'}\n- 学历: {job['education'] or '未提供'}\n"
            f"- 技能标签: {job['tags'] or '未提供'}\n"
            f"- 描述: {job['description'] or '未提供'}\n"
        )

    return f"""你是 Job Hunter 网站内的智能求职助手，帮助已登录用户管理职位订阅、推荐关键词、并解答求职问题。
你必须**每一轮只输出一个 JSON 对象**（不要输出任何 JSON 以外的文字、不要用 Markdown 代码块），格式：
{{"action": "<动作>", "message": "<给用户的简短过程提示，可空>", "args": {{...}}}}

可用动作：
1. reply —— 结束对话并回复用户。字段：{{"action":"reply","reply":"给用户的自然语言回复"}}
2. list_subscriptions —— 查看用户当前订阅（无需 args）。
3. create_subscription —— 新建订阅。args 可含：
   name(可选,会自动取名), keywords(字符串,逗号分隔), sites(数组,取值: {sites_desc}; 留空=全部),
   job_type, location, salary_min(整数,单位k), salary_max(整数,单位k),
   send_slots(数组,取值: {slots_desc}), min_score(0-100), max_jobs(1-50, 每次最多推送条数, 默认10)
4. update_subscription —— 修改订阅。args 必含 id，其余只给需要改的字段。
5. delete_subscription —— 删除订阅。args 必含 id。
6. recommend_keywords —— 推荐关键词。args 可含 query(想了解的岗位方向,可空,留空=按画像)。
7. analyze_resume —— 分析用户的画像/简历（无需 args），给出亮点、短板、改进建议、适合的目标岗位。
8. analyze_job —— 对「当前正在讨论的职位」做匹配度分析（无需 args，仅当上方有职位上下文时可用），返回匹配分、总结、投递建议、待补技能等。

规则：
- 一次只做一步。需要先查再改时，先 list_subscriptions，看到 observation 后再决定下一步。
- **删除订阅前必须在对话里先口头向用户确认**；只有当用户已明确同意（如「确认」「删吧」）时才调用 delete_subscription，否则用 reply 反问确认。
- 创建/修改订阅成功后，用 reply 简要复述你做了什么（订阅名、关键词、地点、薪资、时段）。
- 薪资单位是 k（千元/月）。用户说「25k 以上」→ salary_min=25；「30k 以内」→ salary_max=30。
- 找不到信息或工具失败时，如实用 reply 告知，不要编造。
- 回复用简洁友好的中文。

# 用户
- 昵称: {user.name or '(未填)'} / 邮箱: {user.email}

# 用户画像（用于个性化推荐与答疑）
{profile_text or '（用户尚未填写画像）'}

# 用户当前订阅
{subs_lines}
{job_block}"""


def _format_conversation(messages: list[dict]) -> str:
    lines = []
    for m in messages[-12:]:
        role = m.get("role")
        content = _as_str(m.get("content"))
        if not content:
            continue
        who = "用户" if role == "user" else "助手"
        lines.append(f"{who}: {content[:1500]}")
    return "\n".join(lines) or "用户: 你好"


# ---------------- 主循环 ----------------
async def run_assistant(
    user, messages: list[dict], job_id: Optional[int] = None
) -> dict:
    """跑有界工具循环，返回 {reply, refresh:{subscriptions, jobs}}。"""
    profile_text = render_profile_text(user.profile_json or "")
    subs_summary = _subs_snapshot(user.id)
    job = _load_job(job_id)
    system = build_system_prompt(user, subs_summary, profile_text, job)
    convo = _format_conversation(messages or [])

    refresh = {"subscriptions": False, "jobs": False}
    observations: list[str] = []
    reply: Optional[str] = None

    for step in range(_MAX_STEPS):
        obs_block = (
            "\n# 工具执行结果（observation）\n" + "\n".join(observations)
            if observations
            else ""
        )
        prompt = (
            f"{system}\n\n# 当前对话\n{convo}{obs_block}\n\n"
            "请输出下一步的 JSON 指令（只输出一个 JSON 对象）。"
        )
        try:
            content = await complete(prompt, json_mode=True)
        except LLMError as exc:
            reply = f"抱歉，AI 暂时不可用：{exc}"
            break
        except Exception as exc:  # noqa: BLE001
            log.exception("assistant complete 失败: %s", exc)
            reply = "抱歉，AI 处理出错了，请稍后重试。"
            break

        try:
            cmd = _extract_json(content)
        except Exception:  # noqa: BLE001
            # 模型没按协议输出，就把它当作自然语言回复
            reply = content.strip()[:2000]
            break
        if not isinstance(cmd, dict):
            reply = str(cmd)[:2000]
            break

        action = str(cmd.get("action") or "reply").strip()
        args = cmd.get("args") if isinstance(cmd.get("args"), dict) else {}

        if action == "reply":
            reply = _as_str(cmd.get("reply")) or _as_str(cmd.get("message")) or "好的。"
            break
        elif action == "list_subscriptions":
            obs = {"subscriptions": _subs_snapshot(user.id)}
        elif action == "create_subscription":
            obs = _tool_create(user, args)
            if obs.get("ok"):
                refresh["subscriptions"] = True
                refresh["jobs"] = True
        elif action == "update_subscription":
            obs = _tool_update(user, args)
            if obs.get("ok"):
                refresh["subscriptions"] = True
                refresh["jobs"] = True
        elif action == "delete_subscription":
            obs = _tool_delete(user, args)
            if obs.get("ok"):
                refresh["subscriptions"] = True
                refresh["jobs"] = True
        elif action == "recommend_keywords":
            try:
                obs = await recommend_keywords(
                    profile_text, _as_str((args or {}).get("query"))
                )
            except LLMError as exc:
                obs = {"ok": False, "error": str(exc)}
        elif action == "analyze_resume":
            try:
                obs = await analyze_resume(profile_text)
            except LLMError as exc:
                obs = {"ok": False, "error": str(exc)}
        elif action == "analyze_job":
            try:
                obs = await analyze_job_for_user(job, profile_text)
            except LLMError as exc:
                obs = {"ok": False, "error": str(exc)}
        else:
            obs = {"ok": False, "error": f"未知 action: {action}"}

        observations.append(
            f"[第{step + 1}步] action={action} 结果="
            + json.dumps(obs, ensure_ascii=False)
        )

    if reply is None:
        reply = "我已经处理了你的请求。如还有需要可以继续告诉我。"
    return {"reply": reply, "refresh": refresh}
