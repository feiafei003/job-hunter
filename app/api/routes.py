"""REST 接口：搜索配置 CRUD、抓取/分析触发、职位列表、登录、画像。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel
from sqlmodel import select

from ..analyzer import LLMError
from ..analyzer.assist import extract_profile_from_text, job_assistant
from ..assistant import recommend_keywords, run_assistant
from ..auth import hash_password, require_admin, require_user, verify_password
from ..config import get_settings
from ..keyword_presets import all_groups, all_presets
from ..db import session_scope
from ..matching import match_job_ids, referrals_for_user
from ..resume import extract_text
from ..services import render_profile_text
from ..models import (
    Analysis,
    CrawlStatus,
    CrawlTask,
    DateRange,
    Delivery,
    EmailSetting,
    JobFavorite,
    JobPosting,
    Referral,
    ScheduleUnit,
    SearchConfig,
    SendSlot,
    Site,
    Subscription,
    User,
)
from ..notifier import EmailError, send_email, smtp_configured
from ..scrapers import SCRAPERS, get_scraper
from ..services import (
    analyze_job_by_id,
    analyze_pending,
    analyze_pending_loop,
    count_pending,
    fetch_job_detail,
    get_email_setting,
    manage_url,
    push_jobs_email,
    run_all_and_analyze,
    run_and_analyze,
    run_subscription_digest,
    send_subscription_recovery,
)
from ..scheduler import _SLOT_CRON, _shift_cron, scheduler_service

router = APIRouter()
_settings = get_settings()

# 管理类接口统一加这个依赖，未登录管理员直接 401
ADMIN_DEP = Depends(require_admin)


# ---------- 订阅公共模型/工具（被用户域与 token 自助两处复用，故前置定义） ----------
class SubscriptionIn(BaseModel):
    email: str = ""
    name: str = ""
    sites: list[str] = []
    keywords: str = ""
    job_type: str = ""
    location: str = ""
    salary_min: int = 0
    salary_max: int = 0
    profile: dict = {}
    send_slots: list[str] = [SendSlot.daily_09.value]
    min_score: int = 0
    include_analysis: bool = True
    max_jobs: int = 10
    notify_empty: bool = False
    enabled: bool = True


def _valid_slots(slots: list[str]) -> list[str]:
    allowed = {s.value for s in SendSlot}
    out = [s for s in slots if s in allowed]
    return out or [SendSlot.daily_09.value]


def _clamp_max_jobs(n: int) -> int:
    """每次最多推送条数：1–50，缺省 10。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 10
    return max(1, min(50, n))


def _sub_to_dict(sub: Subscription) -> dict:
    try:
        profile = json.loads(sub.profile_json) if sub.profile_json else {}
    except Exception:  # noqa: BLE001
        profile = {}
    return {
        "id": sub.id,
        "email": sub.email,
        "name": sub.name,
        "sites": [s for s in (sub.sites or "").split(",") if s],
        "keywords": sub.keywords,
        "job_type": sub.job_type,
        "location": sub.location,
        "salary_min": sub.salary_min,
        "salary_max": sub.salary_max,
        "profile": profile,
        "send_slots": [s for s in (sub.send_slots or "").split(",") if s],
        "min_score": sub.min_score,
        "include_analysis": sub.include_analysis,
        "max_jobs": sub.max_jobs,
        "notify_empty": sub.notify_empty,
        "enabled": sub.enabled,
        "manage_token": sub.manage_token,
        "last_sent_at": sub.last_sent_at,
    }

# 时段中文名
_SLOT_NAMES = {
    "daily_09": "每天 10:00",
    "daily_21": "每天 21:00",
    "weekday_09": "工作日 10:00",
    "weekly_mon_09": "每周一 10:00",
}


# ---------- Schemas ----------
class SearchConfigIn(BaseModel):
    name: str
    site: Site
    keyword: str
    city: str = ""
    salary: str = ""
    date_range: DateRange = DateRange.any
    interval: int = 6
    unit: ScheduleUnit = ScheduleUnit.hours
    enabled: bool = True


class ProfileIn(BaseModel):
    profile: str


class EmailSettingIn(BaseModel):
    recipients: str = ""
    enabled: bool = False
    include_analysis: bool = True
    min_score: int = 0


class EmailPushIn(BaseModel):
    include_analysis: Optional[bool] = None
    min_score: Optional[int] = None
    job_ids: Optional[list[int]] = None


# ---------- 鉴权：用户 ----------
class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


def _user_public(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name}


@router.post("/auth/register")
def auth_register(body: RegisterIn, request: Request):
    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(400, "邮箱格式不正确")
    if len(body.password or "") < 6:
        raise HTTPException(400, "密码至少 6 位")
    with session_scope() as session:
        exists = session.exec(select(User).where(User.email == email)).first()
        if exists:
            raise HTTPException(400, "该邮箱已注册，请直接登录")
        user = User(
            email=email,
            password_hash=hash_password(body.password),
            name=(body.name or "").strip(),
        )
        session.add(user)
        session.flush()
        # 把同邮箱的历史订阅归到该用户名下
        for sub in session.exec(
            select(Subscription).where(Subscription.email == email)
        ).all():
            if sub.user_id is None:
                sub.user_id = user.id
                session.add(sub)
        uid = user.id
        pub = _user_public(user)
    request.session["user_id"] = uid
    return {"ok": True, "user": pub}


@router.post("/auth/login")
def auth_login(body: LoginIn, request: Request):
    email = (body.email or "").strip().lower()
    with session_scope() as session:
        user = session.exec(select(User).where(User.email == email)).first()
        if user is None or not verify_password(body.password, user.password_hash):
            raise HTTPException(401, "邮箱或密码不正确")
        user.last_login_at = datetime.utcnow()
        # 登录时顺带 backfill 历史订阅
        for sub in session.exec(
            select(Subscription).where(Subscription.email == email)
        ).all():
            if sub.user_id is None:
                sub.user_id = user.id
                session.add(sub)
        session.add(user)
        uid = user.id
        pub = _user_public(user)
    request.session["user_id"] = uid
    return {"ok": True, "user": pub}


@router.post("/auth/logout")
def auth_logout(request: Request):
    request.session.pop("user_id", None)
    return {"ok": True}


@router.get("/auth/me")
def auth_me(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        return {"authenticated": False}
    with session_scope() as session:
        user = session.get(User, uid)
        if user is None:
            request.session.pop("user_id", None)
            return {"authenticated": False}
        return {"authenticated": True, "user": _user_public(user)}


# ---------- 鉴权：管理员（单一口令） ----------
class AdminLoginIn(BaseModel):
    password: str


@router.post("/admin/login")
def admin_login(body: AdminLoginIn, request: Request):
    if (body.password or "") != _settings.admin_password:
        raise HTTPException(401, "管理员口令不正确")
    request.session["is_admin"] = True
    return {"ok": True}


@router.post("/admin/logout")
def admin_logout(request: Request):
    request.session.pop("is_admin", None)
    return {"ok": True}


@router.get("/admin/me")
def admin_me(request: Request):
    return {"is_admin": bool(request.session.get("is_admin"))}


# ---------- 用户域：我的订阅 / 自画像 / 今日匹配 ----------
class MeProfileIn(BaseModel):
    profile: dict = {}


@router.get("/me/profile")
def me_get_profile(user: User = Depends(require_user)):
    try:
        profile = json.loads(user.profile_json) if user.profile_json else {}
    except Exception:  # noqa: BLE001
        profile = {}
    return {"name": user.name, "email": user.email, "profile": profile}


@router.put("/me/profile")
def me_set_profile(body: MeProfileIn, user: User = Depends(require_user)):
    with session_scope() as session:
        u = session.get(User, user.id)
        if u is None:
            raise HTTPException(404, "用户不存在")
        u.profile_json = json.dumps(body.profile, ensure_ascii=False)
        session.add(u)
    return {"ok": True}


@router.post("/me/profile/from-resume")
async def me_profile_from_resume(
    user: User = Depends(require_user),
    file: UploadFile | None = File(default=None),
    text: str = Form(default=""),
):
    """上传简历文件或粘贴文本，AI 解析为画像字段返回（画像不自动保存，前端回填后由用户确认；
    但简历原文会即时入库，供后续诊断/按岗位优化/经历改写复用，无需重传）。"""
    resume_text = (text or "").strip()
    filename = ""
    if file is not None:
        try:
            data = await file.read()
            resume_text = extract_text(file.filename or "", data)
            filename = (file.filename or "").strip()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    if not resume_text:
        raise HTTPException(400, "请上传简历文件或粘贴简历文本")
    # 即时持久化简历原文（即使后续画像解析失败也已存档）
    with session_scope() as session:
        u = session.get(User, user.id)
        if u is not None:
            u.resume_text = resume_text
            u.resume_filename = filename or "粘贴文本"
            u.resume_updated_at = datetime.utcnow()
            session.add(u)
    try:
        profile = await extract_profile_from_text(resume_text)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "profile": profile}


@router.get("/me/resume")
def me_get_resume(user: User = Depends(require_user)):
    """返回当前用户已存简历的元信息（是否已上传、文件名、更新时间），供前端启用优化功能。"""
    with session_scope() as session:
        u = session.get(User, user.id)
        text_val = (u.resume_text if u else "") or ""
        return {
            "has_resume": bool(text_val.strip()),
            "filename": (u.resume_filename if u else "") or "",
            "updated_at": (
                u.resume_updated_at.isoformat()
                if u and u.resume_updated_at
                else None
            ),
            "length": len(text_val),
        }


class ResumeRewriteIn(BaseModel):
    text: str = ""


@router.post("/me/resume/diagnose")
async def me_resume_diagnose(user: User = Depends(require_user)):
    """对库里最近一次上传的简历做整体诊断打分。"""
    from ..analyzer.assist import diagnose_resume

    with session_scope() as session:
        u = session.get(User, user.id)
        resume_text = (u.resume_text if u else "") or ""
        profile_src = u.profile_json if u else None
    if not resume_text.strip():
        raise HTTPException(400, "尚未上传简历，请先在『画像』里上传简历")
    profile_text = render_profile_text(profile_src)
    try:
        result = await diagnose_resume(resume_text, profile_text)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


@router.post("/me/resume/rewrite")
async def me_resume_rewrite(
    body: ResumeRewriteIn, user: User = Depends(require_user)
):
    """把经历描述逐条改写（STAR/动词+量化）。不传 text 时用库里简历原文。"""
    from ..analyzer.assist import rewrite_bullets

    with session_scope() as session:
        u = session.get(User, user.id)
        stored = (u.resume_text if u else "") or ""
        profile = {}
        try:
            profile = json.loads(u.profile_json) if (u and u.profile_json) else {}
        except Exception:  # noqa: BLE001
            profile = {}
    text_in = (body.text or "").strip() or stored
    if not text_in.strip():
        raise HTTPException(400, "没有可改写的内容，请粘贴经历或先上传简历")
    target_role = str(profile.get("target_role", "") or "")
    try:
        result = await rewrite_bullets(text_in, target_role)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


@router.get("/me/skills/suggest")
def me_suggest_skills(role: str = "", user: User = Depends(require_user)):
    """按目标岗位建议"通用技能"（同岗位普遍要求），用预设角色库匹配。"""
    from ..keyword_presets import all_presets

    q = (role or "").strip().lower()
    best = None
    if q:
        for r in all_presets():
            name = (r.get("role") or "").lower()
            kws = [str(k).lower() for k in (r.get("keywords") or [])]
            if (name and (q in name or name in q)) or any(q in k or k in q for k in kws):
                best = r
                break
    skills = list((best or {}).get("skills", []) or [])
    return {"role": (best or {}).get("role", ""), "skills": skills}


@router.get("/me/compare")
def me_compare_sites(
    keyword: str = "", city: str = "", days: int = 0, user: User = Depends(require_user)
):
    """招聘网站横向对比：某关键词(+城市)在各站的在招数/薪资/匹配分等聚合。"""
    from ..services import compare_sites

    return compare_sites(keyword=keyword, city=city, days=days)


@router.get("/me/company")
async def me_company_score(
    name: str, refresh: bool = False, user: User = Depends(require_user)
):
    """公司多维评分（主营/晋升/待遇/人文关怀）。命中缓存即返回，否则 AI 生成并缓存。"""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "缺少公司名")
    from ..services import get_company_score

    try:
        data = await get_company_score(name, refresh=refresh)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "company": data}


class ContactSyncIn(BaseModel):
    limit: int = 50


@router.post("/me/contacts/sync")
async def me_contacts_sync(body: ContactSyncIn, user: User = Depends(require_user)):
    """从已登录的领英「我的人脉」慢速抓取联系人入库（手动触发，上限默认 50）。"""
    from ..contacts import sync_linkedin_connections

    try:
        res = await sync_linkedin_connections(user.id, limit=body.limit or 50)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"同步失败：{exc}")
    return {"ok": True, **res}


@router.get("/me/contacts")
def me_contacts_list(q: str = "", company: str = "", user: User = Depends(require_user)):
    """人脉列表；传 company 则只返回当前公司匹配该公司的人脉。"""
    from ..contacts import contacts_for_company, list_contacts

    if (company or "").strip():
        return {"contacts": contacts_for_company(user.id, company)}
    return {"contacts": list_contacts(user.id, q=q)}


class ContactImportIn(BaseModel):
    items: list[dict] = []


@router.post("/me/contacts/import")
def me_contacts_import(body: ContactImportIn, user: User = Depends(require_user)):
    """CSV/手动导入人脉兜底（item 至少含 name）。"""
    from ..contacts import import_contacts

    if not body.items:
        raise HTTPException(400, "items 为空")
    return {"ok": True, **import_contacts(user.id, body.items)}


@router.get("/me/interview/questions")
def me_interview_list(
    company: str = "", role: str = "", qtype: str = "", q: str = "", limit: int = 200,
    user: User = Depends(require_user),
):
    """面试题库查询：按公司/岗位/类型/关键词筛选。"""
    from ..services import list_interview_questions

    return {"questions": list_interview_questions(company, role, qtype, q, limit)}


@router.get("/me/interview/companies")
def me_interview_companies(user: User = Depends(require_user)):
    from ..services import interview_companies

    return {"companies": interview_companies()}


class InterviewGenIn(BaseModel):
    company: str = ""
    role: str = ""
    count: int = 12


@router.post("/me/interview/generate")
async def me_interview_generate(body: InterviewGenIn, user: User = Depends(require_user)):
    """AI 按公司+岗位生成面试/笔试题并入库。"""
    from ..services import generate_interview_questions

    try:
        res = await generate_interview_questions(body.company, body.role, body.count)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **res}


class InterviewImportIn(BaseModel):
    items: list[dict] = []
    source: str = "import"


@router.post("/me/interview/import")
def me_interview_import(body: InterviewImportIn, user: User = Depends(require_user)):
    """从外部题源批量导入题目（item 至少含 question）。"""
    from ..services import import_interview_questions

    if not body.items:
        raise HTTPException(400, "items 为空")
    added = import_interview_questions(body.items, source=body.source or "import")
    return {"ok": True, "added": added}


@router.get("/trends/overview")
def trends_overview(
    user: User = Depends(require_user), days: int = 30, city: str = ""
):
    """就业趋势总览：基于每日 baseline 抓取的全市场时间序列。

    days ∈ {7,30,90}（非法值回退 30）；city 留空=全部热门城市。
    """
    from ..trends import compute_overview

    if days not in (7, 30, 90):
        days = 30
    return compute_overview(days=days, city=(city or "").strip())


@router.get("/me/subscriptions")
def me_list_subscriptions(user: User = Depends(require_user)):
    with session_scope() as session:
        subs = list(
            session.exec(
                select(Subscription).where(Subscription.user_id == user.id)
            ).all()
        )
    return [_sub_to_dict(s) for s in subs]


@router.post("/me/subscriptions")
def me_create_subscription(body: SubscriptionIn, user: User = Depends(require_user)):
    token = secrets.token_urlsafe(16)
    with session_scope() as session:
        sub = Subscription(
            user_id=user.id,
            email=user.email,
            name=body.name.strip(),
            sites=",".join(body.sites),
            keywords=body.keywords.strip(),
            job_type=body.job_type.strip(),
            location=body.location.strip(),
            salary_min=max(0, body.salary_min),
            salary_max=max(0, body.salary_max),
            profile_json=json.dumps(body.profile, ensure_ascii=False),
            send_slots=",".join(_valid_slots(body.send_slots)),
            min_score=max(0, body.min_score),
            include_analysis=body.include_analysis,
            max_jobs=_clamp_max_jobs(body.max_jobs),
            notify_empty=body.notify_empty,
            enabled=body.enabled,
            manage_token=token,
        )
        session.add(sub)
        session.flush()
        sid = sub.id
    scheduler_service.reload_jobs()
    return {"ok": True, "id": sid, "token": token}


def _owned_sub(session, sub_id: int, user_id: int) -> Subscription:
    sub = session.get(Subscription, sub_id)
    if sub is None or sub.user_id != user_id:
        raise HTTPException(404, "订阅不存在")
    return sub


@router.put("/me/subscriptions/{sub_id}")
def me_update_subscription(
    sub_id: int, body: SubscriptionIn, user: User = Depends(require_user)
):
    with session_scope() as session:
        sub = _owned_sub(session, sub_id, user.id)
        sub.name = body.name.strip()
        sub.sites = ",".join(body.sites)
        sub.keywords = body.keywords.strip()
        sub.job_type = body.job_type.strip()
        sub.location = body.location.strip()
        sub.salary_min = max(0, body.salary_min)
        sub.salary_max = max(0, body.salary_max)
        sub.profile_json = json.dumps(body.profile, ensure_ascii=False)
        sub.send_slots = ",".join(_valid_slots(body.send_slots))
        sub.min_score = max(0, body.min_score)
        sub.include_analysis = body.include_analysis
        sub.max_jobs = _clamp_max_jobs(body.max_jobs)
        sub.notify_empty = body.notify_empty
        sub.enabled = body.enabled
        session.add(sub)
    scheduler_service.reload_jobs()
    return {"ok": True}


@router.delete("/me/subscriptions/{sub_id}")
def me_delete_subscription(sub_id: int, user: User = Depends(require_user)):
    with session_scope() as session:
        sub = _owned_sub(session, sub_id, user.id)
        session.delete(sub)
    scheduler_service.reload_jobs()
    return {"ok": True}


@router.post("/me/subscriptions/{sub_id}/test")
async def me_test_subscription(sub_id: int, user: User = Depends(require_user)):
    with session_scope() as session:
        sub = _owned_sub(session, sub_id, user.id)
        sid = sub.id
    if not smtp_configured():
        raise HTTPException(400, "服务端未配置 SMTP，无法发送")
    try:
        result = await run_subscription_digest(sid)
    except EmailError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


@router.get("/me/jobs")
def me_jobs(user: User = Depends(require_user), limit: int = 100):
    """聚合本人所有启用订阅匹配到的职位（不排除已发送），附最新分析供展示。"""
    with session_scope() as session:
        subs = list(
            session.exec(
                select(Subscription)
                .where(Subscription.user_id == user.id)
                .where(Subscription.enabled == True)  # noqa: E712
            ).all()
        )
    seen: set[int] = set()
    ordered: list[int] = []
    for sub in subs:
        for jid in match_job_ids(sub, limit=limit, exclude_delivered=False):
            if jid not in seen:
                seen.add(jid)
                ordered.append(jid)
    ordered = ordered[:limit]
    with session_scope() as session:
        fav_ids = {
            f.job_id
            for f in session.exec(
                select(JobFavorite).where(JobFavorite.user_id == user.id)
            ).all()
        }
        result = []
        for jid in ordered:
            job = session.get(JobPosting, jid)
            if job is None:
                continue
            analysis = session.exec(
                select(Analysis)
                .where(Analysis.job_id == jid)
                .order_by(Analysis.created_at.desc())
            ).first()
            result.append(
                {"job": job, "analysis": analysis, "favorited": jid in fav_ids}
            )
    # 按 AI 匹配分从高到低排（未分析的记 -1 排后面），让最相关的浮到最前
    result.sort(
        key=lambda r: (r["analysis"].match_score if r["analysis"] else -1),
        reverse=True,
    )
    return result


class FavoriteIn(BaseModel):
    note: str = ""


@router.get("/me/favorites")
def me_list_favorites(user: User = Depends(require_user), limit: int = 200):
    """本人收藏的职位，最近收藏在前；附最新分析与收藏备注。"""
    with session_scope() as session:
        favs = list(
            session.exec(
                select(JobFavorite)
                .where(JobFavorite.user_id == user.id)
                .order_by(JobFavorite.created_at.desc())
            ).all()
        )
        result = []
        for fav in favs[:limit]:
            job = session.get(JobPosting, fav.job_id)
            if job is None:
                continue
            analysis = session.exec(
                select(Analysis)
                .where(Analysis.job_id == fav.job_id)
                .order_by(Analysis.created_at.desc())
            ).first()
            result.append(
                {
                    "job": job,
                    "analysis": analysis,
                    "favorited": True,
                    "note": fav.note,
                    "favorited_at": fav.created_at,
                }
            )
    return result


@router.post("/me/jobs/{job_id}/favorite")
def me_add_favorite(
    job_id: int, body: FavoriteIn | None = None, user: User = Depends(require_user)
):
    """收藏一条职位（幂等：已收藏则更新备注）。"""
    note = (body.note if body else "") or ""
    with session_scope() as session:
        if session.get(JobPosting, job_id) is None:
            raise HTTPException(404, "职位不存在")
        existing = session.exec(
            select(JobFavorite)
            .where(JobFavorite.user_id == user.id)
            .where(JobFavorite.job_id == job_id)
        ).first()
        if existing is not None:
            if note:
                existing.note = note
                session.add(existing)
            return {"ok": True, "favorited": True}
        session.add(JobFavorite(user_id=user.id, job_id=job_id, note=note))
    return {"ok": True, "favorited": True}


@router.delete("/me/jobs/{job_id}/favorite")
def me_remove_favorite(job_id: int, user: User = Depends(require_user)):
    """取消收藏。"""
    with session_scope() as session:
        existing = session.exec(
            select(JobFavorite)
            .where(JobFavorite.user_id == user.id)
            .where(JobFavorite.job_id == job_id)
        ).first()
        if existing is not None:
            session.delete(existing)
    return {"ok": True, "favorited": False}


@router.post("/me/jobs/{job_id}/detail")
async def me_job_detail(job_id: int, user: User = Depends(require_user)):
    """按需：抓取该职位完整 JD（缺则补）+ 用本人画像做 AI 分析，返回 {job, analysis}。

    用于「今日匹配」列表里点单条「获取详情 & AI 分析」，列表态先快后细。
    """
    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise HTTPException(404, "职位不存在")
        u = session.get(User, user.id)
        profile_src = u.profile_json if u else None

    # 缺完整 JD 则按需抓取详情页回写
    try:
        await fetch_job_detail(job_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"抓取详情失败：{exc}")

    profile_text = render_profile_text(profile_src)
    analysis = None
    try:
        analysis = await analyze_job_by_id(job_id, profile=profile_text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"AI 分析失败：{exc}")

    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        return {"job": job, "analysis": analysis}


class AssistIn(BaseModel):
    kind: str  # cover_letter | interview | skill_gap | resume_tailor


@router.post("/me/jobs/{job_id}/assist")
async def me_job_assist(
    job_id: int, body: AssistIn, user: User = Depends(require_user)
):
    """针对单个职位生成 投递话术/面试题/技能差距/简历优化（按岗位定制）。"""
    if body.kind not in {"cover_letter", "interview", "skill_gap", "resume_tailor"}:
        raise HTTPException(400, "未知的助手类型")
    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise HTTPException(404, "职位不存在")
        u = session.get(User, user.id)
        profile_src = u.profile_json if u else None
        resume_text = (u.resume_text if u else "") or ""
        job_payload = {
            "title": job.title,
            "company": job.company,
            "salary": job.salary,
            "location": job.location,
            "experience": job.experience,
            "education": job.education,
            "tags": job.tags,
            "description": job.description,
        }
    profile_text = render_profile_text(profile_src)
    try:
        if body.kind == "resume_tailor":
            from ..analyzer.assist import tailor_resume_for_job

            text = await tailor_resume_for_job(resume_text, profile_text, job_payload)
        else:
            text = await job_assistant(job_payload, profile_text, body.kind)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "kind": body.kind, "text": text}


# ---------- AI 助手：关键词推荐 + 对话式智能体 ----------
@router.get("/keyword-presets")
def keyword_presets():
    """按岗位大类的静态关键词预设（公开），供订阅表单「推荐关键词」快填。

    presets：拉平的角色列表（向后兼容）；groups：按六大类分组（前端分组渲染）。
    """
    return {"presets": all_presets(), "groups": all_groups()}


@router.post("/me/keywords/recommend")
async def me_keywords_recommend(user: User = Depends(require_user)):
    """按用户画像生成推荐关键词与站点（静态预设优先，否则 AI 生成）。"""
    with session_scope() as session:
        u = session.get(User, user.id)
        profile_src = u.profile_json if u else None
    profile_text = render_profile_text(profile_src or "")
    try:
        result = await recommend_keywords(profile_text)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


class AssistantChatIn(BaseModel):
    messages: list[dict] = []
    job_id: Optional[int] = None


@router.post("/me/assistant/chat")
async def me_assistant_chat(body: AssistantChatIn, user: User = Depends(require_user)):
    """对话式智能体：可建/改/删订阅、推荐关键词、带职位上下文答疑。"""
    try:
        result = await run_assistant(user, body.messages or [], body.job_id)
    except LLMError as exc:
        raise HTTPException(400, str(exc))
    return result


# ---------- 内推 ----------
class ReferralIn(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    keywords: str = ""
    description: str = ""
    contact: str = ""
    url: str = ""
    enabled: bool = True


def _referral_to_dict(ref: Referral, session, include_email: bool = False) -> dict:
    owner = session.get(User, ref.user_id)
    publisher = (owner.name or owner.email) if owner else "未知"
    contact = (ref.contact or "").strip() or (owner.email if owner else "")
    out = {
        "id": ref.id,
        "title": ref.title,
        "company": ref.company,
        "location": ref.location,
        "salary": ref.salary,
        "keywords": ref.keywords,
        "description": ref.description,
        "contact": contact,
        "contact_raw": ref.contact or "",
        "url": ref.url,
        "enabled": ref.enabled,
        "publisher": publisher,
        "created_at": ref.created_at,
    }
    if include_email:
        out["publisher_email"] = owner.email if owner else ""
        out["user_id"] = ref.user_id
    return out


@router.post("/me/referrals")
def me_create_referral(body: ReferralIn, user: User = Depends(require_user)):
    if not (body.title or "").strip():
        raise HTTPException(400, "请填写职位名")
    with session_scope() as session:
        ref = Referral(
            user_id=user.id,
            title=body.title.strip(),
            company=body.company.strip(),
            location=body.location.strip(),
            salary=body.salary.strip(),
            keywords=body.keywords.strip(),
            description=body.description.strip(),
            contact=body.contact.strip(),
            url=body.url.strip(),
            enabled=body.enabled,
        )
        session.add(ref)
        session.flush()
        rid = ref.id
    return {"ok": True, "id": rid}


@router.get("/me/referrals")
def me_list_referrals(user: User = Depends(require_user)):
    with session_scope() as session:
        refs = list(
            session.exec(
                select(Referral)
                .where(Referral.user_id == user.id)
                .order_by(Referral.created_at.desc())
            ).all()
        )
        return [_referral_to_dict(r, session) for r in refs]


@router.get("/me/referrals/matched")
def me_matched_referrals(user: User = Depends(require_user), limit: int = 50):
    """与我启用订阅相关的、他人发布的内推（含联系方式）。"""
    ids = referrals_for_user(user.id, limit=limit)
    with session_scope() as session:
        out = []
        for rid in ids:
            ref = session.get(Referral, rid)
            if ref:
                out.append(_referral_to_dict(ref, session))
        return out


def _owned_referral(session, ref_id: int, user_id: int) -> Referral:
    ref = session.get(Referral, ref_id)
    if ref is None or ref.user_id != user_id:
        raise HTTPException(404, "内推不存在")
    return ref


@router.put("/me/referrals/{ref_id}")
def me_update_referral(ref_id: int, body: ReferralIn, user: User = Depends(require_user)):
    if not (body.title or "").strip():
        raise HTTPException(400, "请填写职位名")
    with session_scope() as session:
        ref = _owned_referral(session, ref_id, user.id)
        ref.title = body.title.strip()
        ref.company = body.company.strip()
        ref.location = body.location.strip()
        ref.salary = body.salary.strip()
        ref.keywords = body.keywords.strip()
        ref.description = body.description.strip()
        ref.contact = body.contact.strip()
        ref.url = body.url.strip()
        ref.enabled = body.enabled
        ref.updated_at = datetime.utcnow()
        session.add(ref)
    return {"ok": True}


@router.delete("/me/referrals/{ref_id}")
def me_delete_referral(ref_id: int, user: User = Depends(require_user)):
    with session_scope() as session:
        ref = _owned_referral(session, ref_id, user.id)
        session.delete(ref)
    return {"ok": True}


@router.get("/referrals")
def browse_referrals(
    user: User = Depends(require_user),
    q: str = "",
    location: str = "",
    limit: int = 100,
):
    """浏览/搜索所有在招内推（需登录，含联系方式）。q 命中标题/公司/关键词/描述。"""
    q = (q or "").strip().lower()
    loc = (location or "").strip().lower()
    with session_scope() as session:
        refs = list(
            session.exec(
                select(Referral)
                .where(Referral.enabled == True)  # noqa: E712
                .order_by(Referral.created_at.desc())
            ).all()
        )
        out = []
        for ref in refs:
            if q:
                hay = " ".join(
                    x.lower() for x in (ref.title, ref.company, ref.keywords, ref.description) if x
                )
                if q not in hay:
                    continue
            if loc and loc not in (ref.location or "").lower():
                continue
            out.append(_referral_to_dict(ref, session))
            if len(out) >= limit:
                break
        return out


# ---------- 搜索配置 ----------
@router.get("/configs", dependencies=[ADMIN_DEP])
def list_configs():
    with session_scope() as session:
        return list(session.exec(select(SearchConfig)).all())


@router.post("/configs", dependencies=[ADMIN_DEP])
def create_config(body: SearchConfigIn):
    with session_scope() as session:
        cfg = SearchConfig(**body.model_dump())
        session.add(cfg)
        session.flush()
        session.refresh(cfg)
        cfg_id = cfg.id
    scheduler_service.reload_jobs()
    return {"id": cfg_id}


@router.put("/configs/{config_id}", dependencies=[ADMIN_DEP])
def update_config(config_id: int, body: SearchConfigIn):
    with session_scope() as session:
        cfg = session.get(SearchConfig, config_id)
        if not cfg:
            raise HTTPException(404, "配置不存在")
        for k, v in body.model_dump().items():
            setattr(cfg, k, v)
        session.add(cfg)
    scheduler_service.reload_jobs()
    return {"ok": True}


@router.delete("/configs/{config_id}", dependencies=[ADMIN_DEP])
def delete_config(config_id: int):
    with session_scope() as session:
        cfg = session.get(SearchConfig, config_id)
        if not cfg:
            raise HTTPException(404, "配置不存在")
        session.delete(cfg)
    scheduler_service.reload_jobs()
    return {"ok": True}


@router.post("/configs/{config_id}/run", dependencies=[ADMIN_DEP])
def run_config(config_id: int, background: BackgroundTasks):
    """立即抓取并分析（后台执行，接口立刻返回）。"""
    with session_scope() as session:
        if not session.get(SearchConfig, config_id):
            raise HTTPException(404, "配置不存在")
    background.add_task(run_and_analyze, config_id)
    return {"ok": True, "message": "已在后台开始抓取与分析"}


@router.post("/configs/run-all", dependencies=[ADMIN_DEP])
def run_all_configs(background: BackgroundTasks):
    """一键搜索所有启用的配置（后台顺序执行，接口立刻返回）。"""
    with session_scope() as session:
        n = len(
            session.exec(
                select(SearchConfig).where(SearchConfig.enabled == True)  # noqa: E712
            ).all()
        )
    if n == 0:
        return {"ok": False, "message": "没有启用的搜索配置"}
    background.add_task(run_all_and_analyze)
    return {"ok": True, "message": f"已在后台开始一键搜索（共 {n} 条配置，顺序执行）"}


# ---------- 职位 ----------
@router.get("/jobs", dependencies=[ADMIN_DEP])
def list_jobs(site: Optional[Site] = None, only_analyzed: bool = False, limit: int = 200):
    with session_scope() as session:
        stmt = select(JobPosting).order_by(JobPosting.scraped_at.desc())
        if site:
            stmt = stmt.where(JobPosting.site == site)
        if only_analyzed:
            stmt = stmt.where(JobPosting.analyzed == True)  # noqa: E712
        jobs = list(session.exec(stmt.limit(limit)).all())

        result = []
        for job in jobs:
            analysis = session.exec(
                select(Analysis)
                .where(Analysis.job_id == job.id)
                .order_by(Analysis.created_at.desc())
            ).first()
            result.append(
                {
                    "job": job,
                    "analysis": analysis,
                }
            )
        return result


@router.delete("/jobs", dependencies=[ADMIN_DEP])
def clear_jobs():
    """清空所有职位与分析记录（搜索任务保留）。"""
    with session_scope() as session:
        n_analysis = 0
        for a in session.exec(select(Analysis)).all():
            session.delete(a)
            n_analysis += 1
        n_jobs = 0
        for j in session.exec(select(JobPosting)).all():
            session.delete(j)
            n_jobs += 1
    return {"ok": True, "deleted_jobs": n_jobs, "deleted_analysis": n_analysis}


@router.post("/jobs/{job_id}/analyze", dependencies=[ADMIN_DEP])
async def analyze_one(job_id: int):
    try:
        analysis = await analyze_job_by_id(job_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return analysis


@router.post("/analyze-pending", dependencies=[ADMIN_DEP])
def analyze_all(background: BackgroundTasks):
    """后台批量分析所有未分析职位，立即返回；结果随页面轮询陆续显示。"""
    n = count_pending()
    if n == 0:
        return {"ok": True, "started": 0, "message": "没有待分析的职位"}
    background.add_task(analyze_pending_loop)
    return {
        "ok": True,
        "started": n,
        "message": f"已在后台开始分析 {n} 条职位，完成后会陆续显示匹配度",
    }


# ---------- 浏览器（托管 debug Chrome）----------
@router.get("/browser/status", dependencies=[ADMIN_DEP])
def browser_status():
    """托管 debug Chrome 的状态：是否启用、是否在运行。"""
    from ..browser import cdp_enabled, debug_chrome_running

    enabled = cdp_enabled()
    return {
        "enabled": enabled,
        "managed": _settings.use_debug_chrome and not (_settings.cdp_endpoint or "").strip(),
        "running": debug_chrome_running() if enabled else False,
        "port": _settings.chrome_debug_port,
    }


@router.get("/browser/logins", dependencies=[ADMIN_DEP])
async def browser_logins():
    """各站点在托管 Chrome 里是否已登录（按 cookie 判断）。"""
    from ..browser import site_login_status

    return await site_login_status()


@router.post("/browser/launch", dependencies=[ADMIN_DEP])
async def browser_launch():
    """手动确保托管 debug Chrome 已启动（未运行则拉起）。"""
    from ..browser import ensure_debug_chrome

    if not _settings.use_debug_chrome:
        raise HTTPException(400, "未启用托管 debug Chrome（.env 设 USE_DEBUG_CHROME=true）")
    try:
        endpoint = await ensure_debug_chrome(_settings.chrome_debug_port)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"启动失败：{exc}") from exc
    return {"ok": True, "endpoint": endpoint}


# ---------- 远程看屏查看器（CDP screencast）所需的数据接口 ----------
def _chrome_http_base() -> str:
    return f"http://127.0.0.1:{_settings.chrome_debug_port}"


# 各招聘站点登录页（用于查看器里的快捷打开按钮）
_SITE_LOGIN_URLS = {
    "zhilian": "https://passport.zhaopin.com/login",
    "boss": "https://www.zhipin.com/",
    "liepin": "https://www.liepin.com/",
    "job51": "https://login.51job.com/login.php",
    "linkedin": "https://www.linkedin.com/login",
}


class BrowserOpenIn(BaseModel):
    url: str


@router.get("/browser/targets", dependencies=[ADMIN_DEP])
async def browser_targets():
    """列出托管 Chrome 当前的页面标签（供查看器选择/连接）。"""
    import httpx

    from ..browser import _ensure_localhost_no_proxy, ensure_debug_chrome

    # 确保托管 Chrome 在跑、且局域网转发口(9223)已建立——这样仅打开查看器页面就能连。
    if _settings.use_debug_chrome:
        try:
            await ensure_debug_chrome(_settings.chrome_debug_port)
        except Exception:  # noqa: BLE001  让下面的 /json/list 给出更具体错误
            pass
    _ensure_localhost_no_proxy()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            resp = await client.get(f"{_chrome_http_base()}/json/list")
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"无法连接调试 Chrome：{exc}") from exc
    pages = [
        {"id": t.get("id"), "title": t.get("title"), "url": t.get("url")}
        for t in data
        if t.get("type") == "page"
    ]
    return {
        "targets": pages,
        "view_port": int(getattr(_settings, "chrome_debug_view_port", 0) or 0),
        "login_urls": _SITE_LOGIN_URLS,
    }


@router.post("/browser/open", dependencies=[ADMIN_DEP])
async def browser_open(body: BrowserOpenIn):
    """在托管 Chrome 中新开一个标签并导航到指定 URL（不会自动关闭）。"""
    import httpx

    from ..browser import _ensure_localhost_no_proxy, ensure_debug_chrome

    if not _settings.use_debug_chrome:
        raise HTTPException(400, "未启用托管 debug Chrome（.env 设 USE_DEBUG_CHROME=true）")
    await ensure_debug_chrome(_settings.chrome_debug_port)
    _ensure_localhost_no_proxy()
    url = (body.url or "").strip() or "about:blank"
    target = f"{_chrome_http_base()}/json/new?{url}"
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            try:
                resp = await client.put(target)
            except Exception:  # noqa: BLE001  老版本只认 GET
                resp = await client.get(target)
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"打开页面失败：{exc}") from exc
    return {"ok": True, "id": data.get("id"), "url": data.get("url")}


# ---------- 登录 ----------
@router.post("/login/{site}", dependencies=[ADMIN_DEP])
def login(site: Site, background: BackgroundTasks):
    """以可见浏览器打开登录页，请在弹出的窗口里手动登录。"""
    scraper = get_scraper(site)
    # BOSS 需要先过安全验证再扫码登录，给更长时间
    wait_seconds = 300 if site == Site.boss else 120

    async def _login():
        await scraper.login(wait_seconds=wait_seconds)

    background.add_task(_login)
    from ..browser import cdp_enabled

    where = "托管 Chrome 中新开的登录标签" if cdp_enabled() else "弹出的窗口"
    if site == Site.boss:
        msg = (
            f"已在{where}打开 BOSS 登录页。请点『点击按钮进行验证』过安全验证，"
            "再扫码登录，完成后手动关闭该标签即保存登录态。"
        )
    else:
        msg = f"已在{where}打开 {site.value} 登录页，登录成功会自动关闭（也可手动关闭该标签）"
    return {"ok": True, "message": msg}


# ---------- 画像 ----------
@router.get("/profile", dependencies=[ADMIN_DEP])
def get_profile():
    return {"profile": _settings.candidate_profile}


@router.post("/profile", dependencies=[ADMIN_DEP])
def set_profile(body: ProfileIn):
    # 运行期内存更新（重启后以 .env 为准）
    _settings.candidate_profile = body.profile
    return {"ok": True}


# ---------- 邮件推送 ----------
@router.get("/email/settings", dependencies=[ADMIN_DEP])
def get_email_settings():
    es = get_email_setting()
    return {
        "recipients": es.recipients,
        "enabled": es.enabled,
        "include_analysis": es.include_analysis,
        "min_score": es.min_score,
        "smtp_configured": smtp_configured(),
        "smtp_host": _settings.smtp_host,
        "smtp_from": _settings.smtp_from or _settings.smtp_user,
    }


@router.post("/email/settings", dependencies=[ADMIN_DEP])
def save_email_settings(body: EmailSettingIn):
    with session_scope() as session:
        es = session.get(EmailSetting, 1)
        if es is None:
            es = EmailSetting(id=1)
        es.recipients = body.recipients
        es.enabled = body.enabled
        es.include_analysis = body.include_analysis
        es.min_score = body.min_score
        session.add(es)
    return {"ok": True}


@router.post("/email/test", dependencies=[ADMIN_DEP])
async def email_test():
    """给配置的收件人发一封测试邮件。"""
    from ..notifier import parse_recipients

    es = get_email_setting()
    recips = parse_recipients(es.recipients)
    if not smtp_configured():
        raise HTTPException(400, "未配置 SMTP，请在 .env 设置 SMTP_HOST/SMTP_USER/SMTP_PASSWORD")
    if not recips:
        raise HTTPException(400, "未配置收件人")
    import asyncio

    html = (
        "<div style='font-family:sans-serif;padding:16px;'>"
        "<h2>Job Hunter 测试邮件</h2>"
        "<p>如果你能收到这封邮件，说明 SMTP 配置正确，职位推送已就绪。</p></div>"
    )
    try:
        n = await asyncio.to_thread(send_email, "[Job Hunter] 测试邮件", html, recips)
    except EmailError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "sent": n}


@router.post("/email/push", dependencies=[ADMIN_DEP])
async def email_push(body: EmailPushIn):
    """立即把当前（或指定）职位推送到收件人邮箱。"""
    try:
        result = await push_jobs_email(
            job_ids=body.job_ids,
            include_analysis=body.include_analysis,
            min_score=body.min_score,
        )
    except EmailError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


# ---------- 订阅（自助） ----------
@router.get("/subscribe/sites")
def subscribe_sites():
    """订阅可选的站点。登录由服务端维护，订阅用户无需感知，所有站点均可选。"""
    names = {
        "zhilian": "智联招聘",
        "linkedin": "领英",
        "boss": "BOSS直聘",
        "liepin": "猎聘",
        "job51": "前程无忧",
    }
    disabled = get_settings().disabled_sites_set
    out = [
        {"value": site.value, "name": names.get(site.value, site.value)}
        for site in SCRAPERS.keys()
        if site.value not in disabled
    ]
    slots = [
        {"value": "daily_09", "name": "每天 10:00"},
        {"value": "daily_21", "name": "每天 21:00"},
        {"value": "weekday_09", "name": "工作日 10:00"},
        {"value": "weekly_mon_09", "name": "每周一 10:00"},
    ]
    return {"sites": out, "slots": slots}


@router.post("/subscribe")
def create_subscription(body: SubscriptionIn):
    if "@" not in body.email:
        raise HTTPException(400, "邮箱格式不正确")
    token = secrets.token_urlsafe(16)
    with session_scope() as session:
        sub = Subscription(
            email=body.email.strip(),
            name=body.name.strip(),
            sites=",".join(body.sites),
            keywords=body.keywords.strip(),
            job_type=body.job_type.strip(),
            location=body.location.strip(),
            salary_min=max(0, body.salary_min),
            salary_max=max(0, body.salary_max),
            profile_json=json.dumps(body.profile, ensure_ascii=False),
            send_slots=",".join(_valid_slots(body.send_slots)),
            min_score=max(0, body.min_score),
            include_analysis=body.include_analysis,
            max_jobs=_clamp_max_jobs(body.max_jobs),
            notify_empty=body.notify_empty,
            enabled=body.enabled,
            manage_token=token,
        )
        session.add(sub)
        session.flush()
    scheduler_service.reload_jobs()
    return {"ok": True, "token": token, "manage_url": manage_url(token)}


@router.get("/my-subscriptions")
def my_subscriptions(email: str):
    """按邮箱列出该用户的所有订阅（部门内部测试用，未做账号验证）。

    用非嵌套路径，避免与 /subscriptions/{token} 路由冲突。
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "请提供正确邮箱")
    with session_scope() as session:
        subs = list(session.exec(select(Subscription)).all())
    mine = [s for s in subs if (s.email or "").strip().lower() == email]
    return [_sub_to_dict(s) for s in mine]


class RecoverIn(BaseModel):
    email: str


@router.post("/subscriptions/recover")
async def recover_subscriptions(body: RecoverIn):
    """输入邮箱，把名下所有订阅的管理链接发到该邮箱（页面不回显，保护隐私）。"""
    email = (body.email or "").strip()
    if "@" not in email:
        raise HTTPException(400, "邮箱格式不正确")
    if not smtp_configured():
        raise HTTPException(400, "服务端未配置 SMTP，无法发送找回邮件")
    try:
        await send_subscription_recovery(email)
    except EmailError as exc:
        raise HTTPException(400, str(exc))
    # 中性提示：不泄露该邮箱是否存在订阅
    return {"ok": True, "message": "如果该邮箱有订阅，管理链接已发送到邮箱，请查收。"}


@router.get("/subscriptions/{token}")
def get_subscription(token: str):
    with session_scope() as session:
        sub = session.exec(
            select(Subscription).where(Subscription.manage_token == token)
        ).first()
        if not sub:
            raise HTTPException(404, "订阅不存在或链接已失效")
        return _sub_to_dict(sub)


@router.post("/subscriptions/{token}")
def update_subscription(token: str, body: SubscriptionIn):
    with session_scope() as session:
        sub = session.exec(
            select(Subscription).where(Subscription.manage_token == token)
        ).first()
        if not sub:
            raise HTTPException(404, "订阅不存在或链接已失效")
        sub.email = body.email.strip() or sub.email
        sub.name = body.name.strip()
        sub.sites = ",".join(body.sites)
        sub.keywords = body.keywords.strip()
        sub.job_type = body.job_type.strip()
        sub.location = body.location.strip()
        sub.salary_min = max(0, body.salary_min)
        sub.salary_max = max(0, body.salary_max)
        sub.profile_json = json.dumps(body.profile, ensure_ascii=False)
        sub.send_slots = ",".join(_valid_slots(body.send_slots))
        sub.min_score = max(0, body.min_score)
        sub.include_analysis = body.include_analysis
        sub.max_jobs = _clamp_max_jobs(body.max_jobs)
        sub.notify_empty = body.notify_empty
        sub.enabled = body.enabled
        session.add(sub)
    scheduler_service.reload_jobs()
    return {"ok": True}


@router.post("/subscriptions/{token}/unsubscribe")
def unsubscribe(token: str):
    with session_scope() as session:
        sub = session.exec(
            select(Subscription).where(Subscription.manage_token == token)
        ).first()
        if not sub:
            raise HTTPException(404, "订阅不存在或链接已失效")
        sub.enabled = False
        session.add(sub)
    scheduler_service.reload_jobs()
    return {"ok": True, "message": "已退订"}


@router.post("/subscriptions/{token}/test")
async def test_subscription(token: str):
    """立即按当前订阅条件匹配现有职位池并发一封（不重新抓取）。"""
    with session_scope() as session:
        sub = session.exec(
            select(Subscription).where(Subscription.manage_token == token)
        ).first()
        if not sub:
            raise HTTPException(404, "订阅不存在或链接已失效")
        sub_id = sub.id
    if not smtp_configured():
        raise HTTPException(400, "服务端未配置 SMTP，无法发送")
    try:
        result = await run_subscription_digest(sub_id)
    except EmailError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


# ---------- 订阅（管理端） ----------
@router.get("/subscriptions", dependencies=[ADMIN_DEP])
def list_subscriptions():
    with session_scope() as session:
        subs = list(session.exec(select(Subscription)).all())
        return [_sub_to_dict(s) for s in subs]


# ---------- 内推（管理端） ----------
@router.get("/admin/referrals", dependencies=[ADMIN_DEP])
def admin_list_referrals():
    """管理员查询所有内推（含已停用、发布者邮箱）。"""
    with session_scope() as session:
        refs = list(
            session.exec(select(Referral).order_by(Referral.created_at.desc())).all()
        )
        return [_referral_to_dict(r, session, include_email=True) for r in refs]


# ---------- 抓取批次（管理端运营面板） ----------
def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _fmt_cron(params: dict) -> str:
    """把 cron 参数渲染成易读时间，如 '08:00' 或 'mon 08:00'。"""
    t = f"{params.get('hour', 0):02d}:{params.get('minute', 0):02d}"
    dow = params.get("day_of_week")
    return f"{dow} {t}" if dow else t


def _used_slots() -> list[str]:
    """当前启用订阅用到的发送时段。"""
    with session_scope() as session:
        subs = list(
            session.exec(
                select(Subscription).where(Subscription.enabled == True)  # noqa: E712
            ).all()
        )
    used: set[str] = set()
    for s in subs:
        for slot in (s.send_slots or "").split(","):
            slot = slot.strip()
            if slot in _SLOT_CRON:
                used.add(slot)
    return sorted(used)


def _slot_counts(slot: str, run_date: str) -> dict:
    with session_scope() as session:
        tasks = list(
            session.exec(
                select(CrawlTask)
                .where(CrawlTask.slot == slot)
                .where(CrawlTask.run_date == run_date)
            ).all()
        )
    counts = {st.value: 0 for st in CrawlStatus}
    for t in tasks:
        counts[t.status.value] = counts.get(t.status.value, 0) + 1
    counts["total"] = len(tasks)
    return counts


@router.get("/slots", dependencies=[ADMIN_DEP])
def list_slots(date: Optional[str] = None):
    """运营面板：当前启用的发送时段 + 预抓取/发送时间 + 当日各组合状态计数。"""
    run_date = date or _today()
    lead = max(0, _settings.crawl_lead_minutes)
    out = []
    for slot in _used_slots():
        params = _SLOT_CRON[slot]
        out.append(
            {
                "slot": slot,
                "name": _SLOT_NAMES.get(slot, slot),
                "send_time": _fmt_cron(params),
                "crawl_time": _fmt_cron(_shift_cron(params, lead)),
                "counts": _slot_counts(slot, run_date),
            }
        )
    return {"date": run_date, "lead_minutes": lead, "slots": out}


@router.get("/slots/{slot}/crawl-tasks", dependencies=[ADMIN_DEP])
def list_crawl_tasks(slot: str, date: Optional[str] = None):
    """某时段某天的抓取组合明细（含失败原因），供运营面板展示与重跑参考。"""
    if slot not in {s.value for s in SendSlot}:
        raise HTTPException(400, "未知时段")
    run_date = date or _today()
    with session_scope() as session:
        tasks = list(
            session.exec(
                select(CrawlTask)
                .where(CrawlTask.slot == slot)
                .where(CrawlTask.run_date == run_date)
                .order_by(CrawlTask.status, CrawlTask.site)
            ).all()
        )
        return [
            {
                "id": t.id,
                "site": t.site,
                "keyword": t.keyword,
                "city": t.city,
                "status": t.status.value,
                "error": t.error,
                "scraped": t.scraped,
                "new": t.new,
                "run_date": t.run_date,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
            }
            for t in tasks
        ]


@router.post("/slots/{slot}/crawl", dependencies=[ADMIN_DEP])
def slot_crawl(slot: str):
    """管理端：立即对某时段做一次预抓取（聚合抓取，不发信）。"""
    if slot not in {s.value for s in SendSlot}:
        raise HTTPException(400, "未知时段")
    scheduler_service.trigger_slot_crawl_now(slot)
    return {"ok": True, "message": f"已在后台开始时段 {slot} 的预抓取"}


@router.post("/slots/{slot}/retry", dependencies=[ADMIN_DEP])
def slot_retry(slot: str):
    """管理端：只重跑某时段当日失败/被拦的抓取组合（人工干预后调用）。"""
    if slot not in {s.value for s in SendSlot}:
        raise HTTPException(400, "未知时段")
    scheduler_service.trigger_slot_retry_now(slot)
    return {"ok": True, "message": f"已在后台重跑时段 {slot} 的失败组合"}


@router.post("/slots/{slot}/send", dependencies=[ADMIN_DEP])
def slot_send(slot: str):
    """管理端：立即对某时段做一次发送（逐订阅匹配+分析+发信）。"""
    if slot not in {s.value for s in SendSlot}:
        raise HTTPException(400, "未知时段")
    scheduler_service.trigger_slot_send_now(slot)
    return {"ok": True, "message": f"已在后台开始时段 {slot} 的发送"}


@router.post("/baseline/warmup", dependencies=[ADMIN_DEP])
def baseline_warmup():
    """管理端：立即跑一轮夜间基础预热（热门城市 × 全角色，仅列表灌池）。"""
    scheduler_service.trigger_baseline_now()
    return {"ok": True, "message": "已在后台开始基础预热（列表-only 灌池）"}


@router.get("/health")
def health():
    from ..analyzer import provider_status

    ps = provider_status()
    return {
        "ok": True,
        "llm_provider": ps["provider"],
        "llm_configured": ps["configured"],
        "sites": [s.value for s in Site],
    }
