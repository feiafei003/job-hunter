"""核心业务逻辑：抓取入库、去重、调用分析。被 scheduler 和 API 复用。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from datetime import datetime
from typing import List

from sqlmodel import select

from .analyzer import analyze_job
from .config import get_settings
from .db import session_scope
from .matching import match_job_ids
from .models import (
    Analysis,
    Company,
    CrawlStatus,
    CrawlTask,
    Delivery,
    EmailSetting,
    InterviewQuestion,
    JobPosting,
    SearchConfig,
    Site,
    Subscription,
    User,
)
from .notifier import EmailError, build_jobs_html, parse_recipients, send_email
from .scrapers import SCRAPERS, ScrapeBlockedError, get_scraper

logger = logging.getLogger("jobhunter.services")
_settings = get_settings()


async def _prepare_crawl_browser() -> None:
    """抓取批次开始前：确保托管 Chrome 在线、且其出网代理可用（失效则换可用代理重启）。

    一并解决两类反复出现的故障：调试 Chrome 没起来、以及当前代理挂了。失败不阻断
    抓取（个别组合仍会按既有逻辑记失败，便于人工排查）。
    """
    try:
        from .browser import ensure_working_proxy_chrome

        proxy = await ensure_working_proxy_chrome()
        logger.info("抓取前置检查完成：生效代理 %s", proxy or "(直连)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("抓取前置检查（Chrome/代理）失败，仍继续抓取：%s", exc)


async def _list_crawl_pause() -> None:
    """列表页抓取的组合间隔：随机区间，拉长以降低单位时间请求量，减少封禁。"""
    lo = max(0.0, float(_settings.scrape_list_min_delay or 0))
    hi = max(lo, float(_settings.scrape_list_max_delay or 0))
    if hi > 0:
        await asyncio.sleep(random.uniform(lo, hi))


def _company_to_dict(row: Company) -> dict:
    return {
        "name": row.name,
        "overall": row.overall,
        "summary": row.summary,
        "business": row.business,
        "promotion": row.promotion,
        "pay": row.pay,
        "culture": row.culture,
        "score_business": row.score_business,
        "score_promotion": row.score_promotion,
        "score_pay": row.score_pay,
        "score_culture": row.score_culture,
        "pros": [x for x in (row.pros or "").split("\n") if x.strip()],
        "cons": [x for x in (row.cons or "").split("\n") if x.strip()],
        "updated_at": row.updated_at,
    }


_SAL_WAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*(?:万|w|W)")
_SAL_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)\s*[kK千]")


def _parse_salary_k(s: str) -> "tuple[float, float] | None":
    """把薪资文本解析成月薪(单位 k)的 (下限, 上限)。解析不出返回 None。

    支持 '15-25K'、'20K-40K'、'1.5-2.5万' 等常见写法；'面议' 等返回 None。
    """
    s = (s or "").strip()
    if not s:
        return None
    m = _SAL_WAN_RE.search(s)
    if m:
        return float(m.group(1)) * 10, float(m.group(2)) * 10
    m = _SAL_K_RE.search(s)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def compare_sites(keyword: str, city: str = "", days: int = 0) -> dict:
    """招聘网站横向对比：对某关键词(+城市)在中央池里按站点聚合。

    每站统计：在招数、薪资区间/均值(可解析的)、含直达链接比例、平均匹配分、最近抓取时间。
    纯读现有 JobPosting/Analysis，不触发抓取。
    """
    from sqlalchemy import or_

    kw = (keyword or "").strip()
    city = (city or "").strip()
    with session_scope() as session:
        stmt = select(JobPosting)
        if kw:
            stmt = stmt.where(
                or_(
                    JobPosting.title.contains(kw),  # type: ignore[attr-defined]
                    JobPosting.tags.contains(kw),  # type: ignore[attr-defined]
                    JobPosting.company.contains(kw),  # type: ignore[attr-defined]
                )
            )
        if city:
            stmt = stmt.where(JobPosting.location.contains(city))  # type: ignore[attr-defined]
        if days and days > 0:
            from datetime import timedelta

            cutoff = datetime.utcnow() - timedelta(days=days)
            stmt = stmt.where(JobPosting.scraped_at >= cutoff)
        rows = list(session.exec(stmt).all())

        # 通用画像(profile_hash='')下的匹配分，用于各站平均分
        ids = [r.id for r in rows]
        score_by_job: dict[int, int] = {}
        if ids:
            for a in session.exec(
                select(Analysis).where(
                    Analysis.job_id.in_(ids),  # type: ignore[attr-defined]
                    Analysis.profile_hash == "",
                )
            ).all():
                # 同职位可能多条，保留最新（按 created_at）
                prev = score_by_job.get(a.job_id)
                score_by_job[a.job_id] = a.match_score if prev is None else prev

    agg: dict[str, dict] = {}
    for r in rows:
        site = r.site.value if hasattr(r.site, "value") else str(r.site)
        d = agg.setdefault(
            site,
            {
                "site": site,
                "count": 0,
                "with_url": 0,
                "sal_mids": [],
                "sal_min": None,
                "sal_max": None,
                "scores": [],
                "latest": None,
            },
        )
        d["count"] += 1
        if (r.url or "").strip():
            d["with_url"] += 1
        sal = _parse_salary_k(r.salary)
        if sal:
            lo, hi = sal
            d["sal_mids"].append((lo + hi) / 2)
            d["sal_min"] = lo if d["sal_min"] is None else min(d["sal_min"], lo)
            d["sal_max"] = hi if d["sal_max"] is None else max(d["sal_max"], hi)
        if r.id in score_by_job:
            d["scores"].append(score_by_job[r.id])
        if r.scraped_at and (d["latest"] is None or r.scraped_at > d["latest"]):
            d["latest"] = r.scraped_at

    sites = []
    for d in agg.values():
        n = len(d["sal_mids"])
        sites.append(
            {
                "site": d["site"],
                "count": d["count"],
                "with_url_pct": round(100 * d["with_url"] / d["count"]) if d["count"] else 0,
                "salary_avg": round(sum(d["sal_mids"]) / n, 1) if n else None,
                "salary_min": d["sal_min"],
                "salary_max": d["sal_max"],
                "salary_n": n,
                "avg_score": round(sum(d["scores"]) / len(d["scores"])) if d["scores"] else None,
                "score_n": len(d["scores"]),
                "latest": d["latest"],
            }
        )
    sites.sort(key=lambda x: x["count"], reverse=True)
    return {
        "keyword": kw,
        "city": city,
        "days": days,
        "total": len(rows),
        "sites": sites,
    }


# ===================== 面试题库 =====================

def _q_fp(company: str, role: str, qtype: str, question: str) -> str:
    basis = f"{(company or '').strip()}|{(role or '').strip()}|{qtype}|{(question or '').strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _q_to_dict(q: InterviewQuestion) -> dict:
    return {
        "id": q.id,
        "company": q.company,
        "role": q.role,
        "qtype": q.qtype,
        "question": q.question,
        "answer": q.answer,
        "tags": [t for t in re.split(r"[，,、]", q.tags or "") if t.strip()],
        "difficulty": q.difficulty,
        "source": q.source,
    }


def _upsert_questions(items: list[dict], company: str, role: str, source: str) -> int:
    """把题目列表入库（按 fp 去重），返回新增条数。"""
    added = 0
    with session_scope() as session:
        for it in items:
            question = str(it.get("question", "") or "").strip()
            if not question:
                continue
            qtype = str(it.get("qtype", "interview") or "interview").strip()
            comp = (str(it.get("company", "")).strip() or company or "")
            rl = (str(it.get("role", "")).strip() or role or "")
            fp = _q_fp(comp, rl, qtype, question)
            exists = session.exec(
                select(InterviewQuestion).where(InterviewQuestion.fp == fp)
            ).first()
            if exists is not None:
                continue
            tags = it.get("tags")
            if isinstance(tags, (list, tuple)):
                tags = "、".join(str(x).strip() for x in tags if str(x).strip())
            session.add(
                InterviewQuestion(
                    fp=fp, company=comp, role=rl, qtype=qtype, question=question,
                    answer=str(it.get("answer", "") or "").strip(),
                    tags=str(tags or "").strip(),
                    difficulty=str(it.get("difficulty", "") or "").strip(),
                    source=source,
                )
            )
            added += 1
    return added


async def generate_interview_questions(company: str, role: str, count: int = 12) -> dict:
    """AI 生成并入库面试/笔试题，返回 {added, questions(该公司+岗位最新列表)}。"""
    from .analyzer.assist import generate_interview_questions as _gen

    items = await _gen(company, role, count)  # 可能抛 LLMError
    added = _upsert_questions(items, company, role, source="ai")
    return {"added": added, "questions": list_interview_questions(company=company, role=role, limit=200)}


def list_interview_questions(
    company: str = "", role: str = "", qtype: str = "", q: str = "", limit: int = 200
) -> list[dict]:
    company = (company or "").strip()
    role = (role or "").strip()
    qtype = (qtype or "").strip()
    q = (q or "").strip()
    with session_scope() as session:
        stmt = select(InterviewQuestion)
        if company:
            stmt = stmt.where(InterviewQuestion.company.contains(company))  # type: ignore[attr-defined]
        if role:
            stmt = stmt.where(InterviewQuestion.role.contains(role))  # type: ignore[attr-defined]
        if qtype:
            stmt = stmt.where(InterviewQuestion.qtype == qtype)
        if q:
            from sqlalchemy import or_

            stmt = stmt.where(
                or_(
                    InterviewQuestion.question.contains(q),  # type: ignore[attr-defined]
                    InterviewQuestion.tags.contains(q),  # type: ignore[attr-defined]
                )
            )
        stmt = stmt.order_by(InterviewQuestion.created_at.desc()).limit(max(1, min(500, limit)))
        return [_q_to_dict(x) for x in session.exec(stmt).all()]


def interview_companies() -> list[str]:
    with session_scope() as session:
        rows = session.exec(select(InterviewQuestion.company)).all()
    seen = []
    for c in rows:
        c = (c or "").strip()
        if c and c not in seen:
            seen.append(c)
    return sorted(seen)


def import_interview_questions(items: list[dict], source: str = "import") -> int:
    """从外部题源批量导入（item 至少含 question；可含 company/role/qtype/answer/tags/difficulty）。"""
    return _upsert_questions(items, company="", role="", source=source)


async def get_company_score(
    name: str, refresh: bool = False, max_age_days: int = 30
) -> dict:
    """取公司多维评分：命中且未过期直接返回缓存，否则调 LLM 生成并入库缓存。"""
    from .analyzer.assist import score_company

    name = (name or "").strip()
    if not name:
        raise ValueError("公司名为空")
    now = datetime.utcnow()
    with session_scope() as session:
        row = session.exec(select(Company).where(Company.name == name)).first()
        if (
            row is not None
            and not refresh
            and row.updated_at is not None
            and (now - row.updated_at).days < max_age_days
        ):
            return _company_to_dict(row)

    data = await score_company(name)  # 可能抛 LLMError

    with session_scope() as session:
        row = session.exec(select(Company).where(Company.name == name)).first()
        if row is None:
            row = Company(name=name)
        row.overall = data["overall"]
        row.summary = data["summary"]
        row.business = data["business"]
        row.promotion = data["promotion"]
        row.pay = data["pay"]
        row.culture = data["culture"]
        row.score_business = data["score_business"]
        row.score_promotion = data["score_promotion"]
        row.score_pay = data["score_pay"]
        row.score_culture = data["score_culture"]
        row.pros = "\n".join(data["pros"])
        row.cons = "\n".join(data["cons"])
        row.raw = data.get("raw", "")
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _company_to_dict(row)

# 自画像结构化字段 -> 中文标签，用于渲染成 LLM 可读文本
_PROFILE_LABELS = {
    "years": "工作年限",
    "current_role": "当前职位",
    "target_role": "目标职位",
    "skills": "技能",
    "skills_common": "通用技能(同岗位普遍具备，仅作基线)",
    "skills_private": "差异化技能(个人亮点，匹配时重点考量)",
    "industry": "行业",
    "expected_salary": "期望薪资",
    "city": "期望城市",
    "relocate": "是否可异地",
    "work_mode": "工作模式",
    "company_size": "公司规模偏好",
    "education": "学历",
    "avoid": "想避开",
    "goal": "职业目标",
}


def render_profile_text(profile_json: str) -> str:
    """把自画像结构化 JSON 渲染成 LLM 可读的多行文本。"""
    if not profile_json:
        return ""
    try:
        data = json.loads(profile_json)
    except Exception:  # noqa: BLE001
        return profile_json.strip()
    if not isinstance(data, dict):
        return str(data)
    lines: list[str] = []
    for key, label in _PROFILE_LABELS.items():
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, (list, tuple)):
            val = "、".join(str(x) for x in val if x)
        if str(val).strip():
            lines.append(f"- {label}: {str(val).strip()}")
    # 保留未识别字段
    for key, val in data.items():
        if key in _PROFILE_LABELS or not val:
            continue
        lines.append(f"- {key}: {val}")
    return "\n".join(lines)


def profile_hash(profile_text: str) -> str:
    """画像文本指纹（用于分析结果缓存）。空画像固定为空串。"""
    norm = (profile_text or "").strip()
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def get_email_setting() -> EmailSetting:
    """读取（必要时创建）唯一的邮件配置行。返回游离副本。"""
    with session_scope() as session:
        es = session.get(EmailSetting, 1)
        if es is None:
            es = EmailSetting(id=1)
            session.add(es)
            session.flush()
        return EmailSetting(
            id=es.id,
            recipients=es.recipients,
            enabled=es.enabled,
            include_analysis=es.include_analysis,
            min_score=es.min_score,
        )


async def run_search_config(config_id: int) -> dict:
    """执行一条搜索配置：抓取 -> 去重入库。返回统计信息。"""
    with session_scope() as session:
        config = session.get(SearchConfig, config_id)
        if config is None:
            raise ValueError(f"搜索配置 {config_id} 不存在")
        # 复制需要的字段，避免会话关闭后访问
        site = config.site
        keyword = config.keyword
        city = config.city
        salary = config.salary
        date_range = config.date_range.value

    scraper = get_scraper(site)
    logger.info(
        "开始抓取 site=%s keyword=%s city=%s date_range=%s",
        site,
        keyword,
        city,
        date_range,
    )
    raw_jobs = await scraper.search(
        keyword=keyword,
        city=city,
        salary=salary,
        limit=_settings.max_jobs_per_run,
        date_range=date_range,
    )

    new_ids = _ingest_raw_jobs(site, raw_jobs, config_id=config_id)

    with session_scope() as session:
        config = session.get(SearchConfig, config_id)
        if config:
            config.last_run_at = datetime.utcnow()
            session.add(config)

    logger.info("抓取完成 site=%s 抓到=%d 新增=%d", site, len(raw_jobs), len(new_ids))
    return {"scraped": len(raw_jobs), "new": len(new_ids), "new_ids": new_ids}


def _ingest_raw_jobs(site: Site, raw_jobs, config_id: int | None = None) -> list[int]:
    """抓取结果去重入库；已存在但本次补到详情则更新。返回新增职位 id。"""
    new_ids: list[int] = []
    with session_scope() as session:
        for raw in raw_jobs:
            fp = raw.fingerprint(site.value)
            exists = session.exec(
                select(JobPosting).where(JobPosting.fingerprint == fp)
            ).first()
            if exists:
                if raw.description and not exists.description:
                    exists.description = raw.description
                    exists.tags = raw.tags or exists.tags
                    exists.experience = raw.experience or exists.experience
                    exists.education = raw.education or exists.education
                    exists.analyzed = False
                    session.add(exists)
                continue
            job = JobPosting(
                fingerprint=fp,
                site=site,
                config_id=config_id,
                title=raw.title,
                company=raw.company,
                salary=raw.salary,
                location=raw.location,
                experience=raw.experience,
                education=raw.education,
                tags=raw.tags,
                description=raw.description,
                url=raw.url,
            )
            session.add(job)
            session.flush()
            new_ids.append(job.id)
    return new_ids


async def ingest_search(
    site: Site, keyword: str, city: str = "", fetch_detail: bool | None = None
) -> dict:
    """临时抓取一次（不依赖 SearchConfig），结果入中央职位池。

    fetch_detail=False 时只抓列表页（基础预热/快速预抓取用）。
    """
    scraper = get_scraper(site)
    raw_jobs = await scraper.search(
        keyword=keyword, city=city, limit=_settings.max_jobs_per_run, fetch_detail=fetch_detail
    )
    new_ids = _ingest_raw_jobs(site, raw_jobs, config_id=None)
    logger.info(
        "聚合抓取 site=%s keyword=%s city=%s 抓到=%d 新增=%d",
        site.value,
        keyword,
        city,
        len(raw_jobs),
        len(new_ids),
    )
    return {"scraped": len(raw_jobs), "new": len(new_ids)}


async def fetch_job_detail(job_id: int) -> bool:
    """按需抓取单条职位的详情页 JD，回写库并使其可被重新分析。

    返回是否成功补到 JD。已有 description 的直接返回 True（无需重抓）。
    """
    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            return False
        if (job.description or "").strip():
            return True
        site = job.site
        url = job.url

    if not url:
        return False
    scraper = get_scraper(site)
    try:
        detail = await scraper.fetch_detail_for(url)
    except ScrapeBlockedError:
        logger.warning("按需详情被拦 job=%s site=%s", job_id, site.value)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("按需详情抓取失败 job=%s: %s", job_id, exc)
        return False

    desc = (detail.get("description") or "").strip()
    if not desc:
        return False
    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            return False
        job.description = desc
        if detail.get("tags"):
            job.tags = detail["tags"]
        if detail.get("experience"):
            job.experience = detail["experience"]
        if detail.get("education"):
            job.education = detail["education"]
        job.analyzed = False  # 有了完整 JD，作废旧分析以便重算
        session.add(job)
    logger.info("按需详情补全成功 job=%s（%d 字）", job_id, len(desc))
    return True


async def analyze_job_by_id(
    job_id: int, profile: str | None = None, ph: str | None = None
) -> Analysis:
    """分析职位。按 (job_id, profile_hash) 缓存复用，避免同画像重复花钱。"""
    if ph is None:
        ph = profile_hash(profile or "")

    # 命中缓存直接返回
    with session_scope() as session:
        cached = session.exec(
            select(Analysis)
            .where(Analysis.job_id == job_id)
            .where(Analysis.profile_hash == ph)
            .order_by(Analysis.created_at.desc())
        ).first()
        if cached:
            return Analysis(
                id=cached.id,
                job_id=cached.job_id,
                profile_hash=cached.profile_hash,
                match_score=cached.match_score,
                summary=cached.summary,
                advice=cached.advice,
                skills_to_learn=cached.skills_to_learn,
                resume_tips=cached.resume_tips,
                raw=cached.raw,
                created_at=cached.created_at,
            )

    with session_scope() as session:
        job = session.get(JobPosting, job_id)
        if job is None:
            raise ValueError(f"职位 {job_id} 不存在")
        job_dict = {
            "title": job.title,
            "company": job.company,
            "salary": job.salary,
            "location": job.location,
            "experience": job.experience,
            "education": job.education,
            "tags": job.tags,
            "description": job.description,
            "site": job.site.value,
        }

    result = await analyze_job(job_dict, profile=profile)

    with session_scope() as session:
        analysis = Analysis(
            job_id=job_id,
            profile_hash=ph,
            match_score=result["match_score"],
            summary=result["summary"],
            advice=result["advice"],
            skills_to_learn=result.get("skills_to_learn", ""),
            resume_tips=result.get("resume_tips", ""),
            raw=result["raw"],
        )
        session.add(analysis)
        job = session.get(JobPosting, job_id)
        if job:
            job.analyzed = True
            session.add(job)
        session.flush()
        session.refresh(analysis)
        # 触发属性加载后返回一个游离副本
        detached = Analysis(
            id=analysis.id,
            job_id=analysis.job_id,
            profile_hash=analysis.profile_hash,
            match_score=analysis.match_score,
            summary=analysis.summary,
            advice=analysis.advice,
            skills_to_learn=analysis.skills_to_learn,
            resume_tips=analysis.resume_tips,
            raw=analysis.raw,
            created_at=analysis.created_at,
        )
    return detached


async def analyze_pending(limit: int = 0, profile: str | None = None) -> dict:
    """分析所有尚未分析的职位（并发执行以提速）。

    limit<=0 时用配置 analyze_max 作为单次上限；并发度由 analyze_concurrency 控制。
    每条分析为独立子进程调 LLM（约 20 秒/条），串行很慢，故并发跑。
    """
    cap = limit if limit and limit > 0 else max(1, _settings.analyze_max)
    with session_scope() as session:
        pending = list(
            session.exec(
                select(JobPosting.id).where(JobPosting.analyzed == False)  # noqa: E712
            ).all()
        )
    pending = pending[:cap]
    if not pending:
        return {"analyzed": 0, "failed": 0, "total": 0}

    conc = max(1, _settings.analyze_concurrency)
    sem = asyncio.Semaphore(conc)
    counters = {"ok": 0, "failed": 0}

    async def _one(job_id: int) -> None:
        async with sem:
            try:
                await analyze_job_by_id(job_id, profile=profile)
                counters["ok"] += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("分析职位 %s 失败: %s", job_id, exc)
                counters["failed"] += 1

    logger.info("开始分析 %d 条未处理职位（并发 %d）...", len(pending), conc)
    await asyncio.gather(*[_one(i) for i in pending])
    logger.info(
        "分析完成：成功 %d，失败 %d。", counters["ok"], counters["failed"]
    )
    return {"analyzed": counters["ok"], "failed": counters["failed"], "total": len(pending)}


def count_pending() -> int:
    """当前待分析（未分析）职位数。"""
    with session_scope() as session:
        return len(
            session.exec(
                select(JobPosting.id).where(JobPosting.analyzed == False)  # noqa: E712
            ).all()
        )


async def analyze_pending_loop(profile: str | None = None, safety_cap: int = 1000) -> dict:
    """分批分析所有未分析职位，直到清空（或达安全上限）。

    analyze_pending 单批受 analyze_max 限制；这里循环跑直到没有待分析项，
    适合放后台任务，避免同步请求长时间阻塞。
    """
    total_ok = 0
    total_failed = 0
    processed = 0
    while processed < safety_cap:
        r = await analyze_pending(profile=profile)
        if r["total"] == 0:
            break
        total_ok += r["analyzed"]
        total_failed += r["failed"]
        processed += r["total"]
    logger.info("批量分析全部完成：成功 %d，失败 %d。", total_ok, total_failed)
    return {"analyzed": total_ok, "failed": total_failed, "total": processed}


def _gather_job_items(job_ids: list[int] | None, min_score: int) -> list[dict]:
    """把职位+最新分析整理成发信用的纯 dict 列表。"""
    with session_scope() as session:
        if job_ids:
            jobs = [j for j in (session.get(JobPosting, i) for i in job_ids) if j]
        else:
            jobs = list(
                session.exec(
                    select(JobPosting).order_by(JobPosting.scraped_at.desc()).limit(50)
                ).all()
            )

        items: list[dict] = []
        for job in jobs:
            analysis = session.exec(
                select(Analysis)
                .where(Analysis.job_id == job.id)
                .order_by(Analysis.created_at.desc())
            ).first()
            if min_score > 0:
                score = analysis.match_score if analysis else -1
                if score < min_score:
                    continue
            items.append(
                {
                    "job": {
                        "title": job.title,
                        "company": job.company,
                        "salary": job.salary,
                        "location": job.location,
                        "experience": job.experience,
                        "education": job.education,
                        "tags": job.tags,
                        "description": job.description,
                        "url": job.url,
                        "site": job.site.value,
                    },
                    "analysis": (
                        {
                            "match_score": analysis.match_score,
                            "summary": analysis.summary,
                            "advice": analysis.advice,
                            "skills_to_learn": analysis.skills_to_learn,
                            "resume_tips": analysis.resume_tips,
                        }
                        if analysis
                        else None
                    ),
                }
            )
        return items


async def push_jobs_email(
    job_ids: list[int] | None = None,
    recipients: list[str] | None = None,
    include_analysis: bool | None = None,
    min_score: int | None = None,
) -> dict:
    """构建并发送职位推送邮件。参数留空则取数据库里的邮件配置。"""
    es = get_email_setting()
    recips = recipients if recipients is not None else parse_recipients(es.recipients)
    inc = es.include_analysis if include_analysis is None else include_analysis
    ms = es.min_score if min_score is None else min_score

    if not recips:
        raise EmailError("未配置收件人")

    items = await asyncio.to_thread(_gather_job_items, job_ids, ms)
    if not items:
        logger.info("邮件推送：无符合条件的职位，跳过。")
        return {"sent": 0, "jobs": 0, "skipped": "无符合条件的职位"}

    subject = f"[Job Hunter] {len(items)} 个新职位推送"
    html = build_jobs_html(items, inc)
    n = await asyncio.to_thread(send_email, subject, html, recips)
    return {"sent": n, "jobs": len(items)}


async def run_and_analyze(config_id: int, profile: str | None = None) -> dict:
    scrape_stats = await run_search_config(config_id)
    analyze_stats = await analyze_pending(profile=profile)

    push_stats = None
    es = get_email_setting()
    new_ids = scrape_stats.get("new_ids") or []
    if es.enabled and new_ids and parse_recipients(es.recipients):
        try:
            push_stats = await push_jobs_email(job_ids=new_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("邮件推送失败: %s", exc)
            push_stats = {"error": str(exc)}

    return {"scrape": scrape_stats, "analyze": analyze_stats, "push": push_stats}


def list_enabled_configs() -> List[SearchConfig]:
    with session_scope() as session:
        return list(session.exec(select(SearchConfig).where(SearchConfig.enabled == True)).all())  # noqa: E712


async def run_all_and_analyze(profile: str | None = None) -> dict:
    """依次跑完所有启用的搜索配置（顺序执行，避免并发触发反爬）。"""
    with session_scope() as session:
        config_ids = [
            c.id
            for c in session.exec(
                select(SearchConfig).where(SearchConfig.enabled == True)  # noqa: E712
            ).all()
        ]

    logger.info("一键搜索：共 %d 条启用配置，开始顺序执行。", len(config_ids))
    results = []
    for cid in config_ids:
        try:
            stats = await run_and_analyze(cid, profile=profile)
            results.append({"config_id": cid, "ok": True, "stats": stats})
        except Exception as exc:  # noqa: BLE001
            logger.warning("一键搜索：配置 %s 执行失败：%s", cid, exc)
            results.append({"config_id": cid, "ok": False, "error": str(exc)})
    logger.info("一键搜索完成：%d 条配置已跑完。", len(config_ids))
    return {"configs": len(config_ids), "results": results}


# ---------- 订阅：个性化匹配 + 发信 ----------

_MAX_JOBS_PER_EMAIL = 20


def _job_dict(job: JobPosting) -> dict:
    return {
        "title": job.title,
        "company": job.company,
        "salary": job.salary,
        "location": job.location,
        "experience": job.experience,
        "education": job.education,
        "tags": job.tags,
        "description": job.description,
        "url": job.url,
        "site": job.site.value,
    }


def manage_url(token: str) -> str:
    base = (_settings.base_url or "").rstrip("/")
    return f"{base}/subscribe?token={token}"


async def send_subscription_recovery(email: str) -> int:
    """把某邮箱名下所有订阅的管理链接发到该邮箱。返回订阅条数（0=该邮箱无订阅）。

    出于隐私，链接只发到邮箱、不在页面回显；调用方应统一回中性提示。
    """
    email = (email or "").strip()
    if "@" not in email:
        return 0
    with session_scope() as session:
        subs = [
            Subscription(**s.model_dump())
            for s in session.exec(
                select(Subscription).where(Subscription.email == email)
            ).all()
        ]
    if not subs:
        logger.info("订阅找回：邮箱 %s 无订阅，跳过发送。", email)
        return 0

    rows = []
    for s in subs:
        status = "启用中" if s.enabled else "已退订"
        link = manage_url(s.manage_token)
        rows.append(
            f'<li style="margin-bottom:10px;"><b>{s.name or "(未命名订阅)"}</b>'
            f'（{status}）<br><a href="{link}">{link}</a></li>'
        )
    html = (
        "<div style='font-family:sans-serif;padding:16px;line-height:1.6;'>"
        "<h2>你的职位订阅管理链接</h2>"
        f"<p>以下是与 {email} 关联的订阅，点击对应链接即可查看 / 修改 / 退订：</p>"
        f"<ul style='padding-left:18px;'>{''.join(rows)}</ul>"
        "<p style='color:#888;font-size:12px;'>如果这不是你本人操作，请忽略本邮件。</p></div>"
    )
    await asyncio.to_thread(send_email, "[Job Hunter] 你的订阅管理链接", html, [email])
    logger.info("订阅找回：已向 %s 发送 %d 条订阅的管理链接。", email, len(subs))
    return len(subs)


async def run_subscription_digest(sub_id: int) -> dict:
    """对一条订阅：匹配职位 -> 个性化分析 -> 过滤 -> 发邮件 -> 记录已发。"""
    with session_scope() as session:
        sub = session.get(Subscription, sub_id)
        if sub is None or not sub.enabled:
            return {"skipped": "订阅不存在或已停用"}
        # 取游离副本，后续在会话外使用
        sub = Subscription(**sub.model_dump())

    # 每次最多发送条数（取最匹配的前 N 条），钳制 1–50
    max_jobs = min(50, max(1, sub.max_jobs or _MAX_JOBS_PER_EMAIL))
    # 候选池：上限约 30 条，至少覆盖到 max_jobs（先打分后排序取 Top N）
    cand_limit = min(30, max(max_jobs, max_jobs * 3))
    job_ids = match_job_ids(sub, limit=cand_limit)
    if not job_ids:
        logger.info("订阅 %s(%s) 无新匹配职位。", sub_id, sub.email)
        return await _maybe_notify_empty(sub, sub_id)

    # 订阅自身画像为空时，回退到所属用户的用户级画像
    profile_src = sub.profile_json
    if not (profile_src or "").strip() and sub.user_id:
        with session_scope() as session:
            owner = session.get(User, sub.user_id)
            if owner and (owner.profile_json or "").strip():
                profile_src = owner.profile_json
    profile_text = render_profile_text(profile_src)
    ph = profile_hash(profile_text)
    need_analysis = sub.include_analysis or sub.min_score > 0

    # 先收集 (job_id, item, score)，再按匹配度排序取 Top N。
    # 候选多为“列表态”（无完整 JD）：按顺序逐条——缺详情则按需补抓，再分析。
    # 详情抓取量被限制在实际要发的量级（收满 max_jobs 个通过项即停），不对整池补抓。
    scored: list[tuple[int, dict, int]] = []
    for jid in job_ids:
        if len(scored) >= max_jobs:
            break  # 已收集足够可发送的候选，停止继续补详情/分析
        # 列表态职位缺完整 JD：发送前按需补抓详情，分析才有依据
        with session_scope() as session:
            job = session.get(JobPosting, jid)
            has_desc = bool(job and (job.description or "").strip())
        if not has_desc:
            try:
                await fetch_job_detail(jid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("订阅 %s 补抓职位 %s 详情失败：%s", sub_id, jid, exc)

        analysis_obj = None
        if need_analysis:
            try:
                analysis_obj = await analyze_job_by_id(jid, profile=profile_text, ph=ph)
            except Exception as exc:  # noqa: BLE001
                logger.warning("订阅 %s 分析职位 %s 失败：%s", sub_id, jid, exc)
        score = analysis_obj.match_score if analysis_obj else -1
        if sub.min_score > 0 and score < sub.min_score:
            continue

        with session_scope() as session:
            job = session.get(JobPosting, jid)
            if job is None:
                continue
            jd = _job_dict(job)
        item = {
            "job": jd,
            "analysis": (
                {
                    "match_score": analysis_obj.match_score,
                    "summary": analysis_obj.summary,
                    "advice": analysis_obj.advice,
                    "skills_to_learn": analysis_obj.skills_to_learn,
                    "resume_tips": analysis_obj.resume_tips,
                }
                if (sub.include_analysis and analysis_obj)
                else None
            ),
        }
        scored.append((jid, item, score))

    if not scored:
        logger.info("订阅 %s(%s) 匹配到职位但均低于分数线，跳过。", sub_id, sub.email)
        return await _maybe_notify_empty(sub, sub_id, matched=len(job_ids))

    # 有分析分数时按匹配度从高到低排序；否则保持匹配（抓取时间）原序
    if need_analysis:
        scored.sort(key=lambda t: t[2], reverse=True)
    selected = scored[:max_jobs]
    items = [it for _, it, _ in selected]
    sent_ids = [jid for jid, _, _ in selected]

    subject = f"[Job Hunter] {sub.name or sub.email} · {len(items)} 个新职位"
    html = build_jobs_html(items, sub.include_analysis, manage_url=manage_url(sub.manage_token))
    n = await asyncio.to_thread(send_email, subject, html, [sub.email])

    now = datetime.utcnow()
    with session_scope() as session:
        for jid in sent_ids:
            session.add(Delivery(subscription_id=sub_id, job_id=jid))
        s = session.get(Subscription, sub_id)
        if s:
            s.last_sent_at = now
            session.add(s)

    logger.info("订阅 %s(%s) 已推送 %d 个职位。", sub_id, sub.email, len(items))
    return {"sent": n, "jobs": len(items), "matched": len(job_ids)}


async def _maybe_notify_empty(sub: Subscription, sub_id: int, matched: int = 0) -> dict:
    """无新匹配时：若订阅开启 notify_empty 则发一封简短提醒，否则静默跳过。"""
    if not sub.notify_empty:
        return {"sent": 0, "matched": matched}
    subject = f"[Job Hunter] {sub.name or sub.email} · 本次暂无新匹配职位"
    link = manage_url(sub.manage_token)
    html = (
        "<div style='max-width:680px;margin:0 auto;font-family:-apple-system,Segoe UI,"
        "Roboto,Helvetica,Arial,sans-serif;background:#f1f5f9;padding:20px;'>"
        "<div style='background:#0f172a;color:#fff;border-radius:12px;padding:16px 20px;margin-bottom:16px;'>"
        "<div style='font-size:18px;font-weight:700;'>Job Hunter · 职位推送</div>"
        "<div style='color:#94a3b8;font-size:13px;margin-top:2px;'>本次暂无符合条件的新职位</div></div>"
        "<div style='background:#fff;border-radius:12px;padding:16px;color:#475569;font-size:13px;line-height:1.7;'>"
        "本次发送时段没有匹配到新的职位。可在管理页适当放宽关键词 / 城市 / 最低匹配度，或增加发送时段。<br>"
        f"<a href='{link}' style='color:#1d4ed8;'>管理订阅</a></div>"
        "<div style='color:#94a3b8;font-size:12px;text-align:center;margin-top:8px;'>由 Job Hunter Agent 自动发送</div>"
        "</div>"
    )
    try:
        n = await asyncio.to_thread(send_email, subject, html, [sub.email])
    except Exception as exc:  # noqa: BLE001
        logger.warning("订阅 %s 发送无匹配提醒失败：%s", sub_id, exc)
        return {"sent": 0, "matched": matched}
    now = datetime.utcnow()
    with session_scope() as session:
        s = session.get(Subscription, sub_id)
        if s:
            s.last_sent_at = now
            session.add(s)
    logger.info("订阅 %s(%s) 无新匹配，已发送提醒。", sub_id, sub.email)
    return {"sent": n, "jobs": 0, "matched": matched, "empty_notice": True}


def _subs_for_slot(slot: str) -> list[Subscription]:
    """该时段下所有启用订阅（游离副本）。"""
    with session_scope() as session:
        all_subs = list(
            session.exec(
                select(Subscription).where(Subscription.enabled == True)  # noqa: E712
            ).all()
        )
    return [
        Subscription(**s.model_dump())
        for s in all_subs
        if slot in [x.strip() for x in (s.send_slots or "").split(",") if x.strip()]
    ]


def _aggregate_combos(subs: list[Subscription]) -> set[tuple[str, str, str]]:
    """把多条订阅聚合去重成抓取组合 (site, keyword, city)。已停用站点(disabled_sites)跳过。"""
    disabled = _settings.disabled_sites_set
    all_sites = [s for s in SCRAPERS.keys() if s.value not in disabled]
    combos: set[tuple[str, str, str]] = set()
    for s in subs:
        sites = [x.strip() for x in (s.sites or "").split(",") if x.strip()]
        site_objs: list[Site] = []
        for sv in sites:
            if sv in disabled:
                continue
            try:
                site_objs.append(Site(sv))
            except ValueError:
                continue
        if not site_objs:
            site_objs = all_sites
        keywords = [k.strip() for k in re.split(r"[,\s，、]+", s.keywords or "") if k.strip()]
        if not keywords:
            keywords = [""]
        city = (s.location or "").strip()
        for site in site_objs:
            for kw in keywords:
                combos.add((site.value, kw, city))
    return combos


def _today() -> str:
    """批次日期键（本地时间 YYYY-MM-DD）。"""
    return datetime.now().strftime("%Y-%m-%d")


async def _run_crawl_task(task_id: int, fetch_detail: bool | None = None) -> dict:
    """执行单个抓取组合并把状态写回 CrawlTask。返回该任务统计。

    fetch_detail=False 时只抓列表页（基础预热/快速预抓取）。
    """
    with session_scope() as session:
        task = session.get(CrawlTask, task_id)
        if task is None:
            return {"error": "任务不存在"}
        site_v, kw, city = task.site, task.keyword, task.city

    status = CrawlStatus.ok
    error = ""
    scraped = new = 0
    try:
        stat = await ingest_search(Site(site_v), kw, city, fetch_detail=fetch_detail)
        scraped = stat.get("scraped", 0)
        new = stat.get("new", 0)
    except ScrapeBlockedError as exc:
        status = CrawlStatus.blocked
        error = str(exc) or "被反爬/登录墙拦截"
        logger.warning("预抓取被拦 site=%s kw=%s：%s", site_v, kw, error)
    except Exception as exc:  # noqa: BLE001
        status = CrawlStatus.failed
        error = str(exc)
        logger.warning("预抓取失败 site=%s kw=%s：%s", site_v, kw, exc)

    with session_scope() as session:
        task = session.get(CrawlTask, task_id)
        if task:
            task.status = status
            task.error = error
            task.scraped = scraped
            task.new = new
            task.updated_at = datetime.utcnow()
            session.add(task)
    return {
        "site": site_v,
        "keyword": kw,
        "city": city,
        "status": status.value,
        "scraped": scraped,
        "new": new,
        "error": error,
    }


async def run_slot_crawl(slot: str) -> dict:
    """预抓取阶段（发送时间前提前跑）：聚合组合 -> 逐个抓取并记录成败。

    单个组合失败不影响其余组合；不发信。失败/被拦的组合留待管理员重跑。
    """
    subs = _subs_for_slot(slot)
    if not subs:
        logger.info("时段 %s 无到点订阅，跳过预抓取。", slot)
        return {"slot": slot, "subscriptions": 0, "combos": 0}

    combos = _aggregate_combos(subs)
    run_date = _today()

    # upsert：本批次每个组合建/置为 pending
    task_ids: list[int] = []
    with session_scope() as session:
        for site_v, kw, city in sorted(combos):
            existing = session.exec(
                select(CrawlTask)
                .where(CrawlTask.slot == slot)
                .where(CrawlTask.run_date == run_date)
                .where(CrawlTask.site == site_v)
                .where(CrawlTask.keyword == kw)
                .where(CrawlTask.city == city)
            ).first()
            if existing is None:
                existing = CrawlTask(
                    slot=slot, run_date=run_date, site=site_v, keyword=kw, city=city
                )
            existing.status = CrawlStatus.pending
            existing.error = ""
            existing.updated_at = datetime.utcnow()
            session.add(existing)
            session.flush()
            task_ids.append(existing.id)

    logger.info(
        "时段 %s：%d 条订阅，聚合出 %d 个抓取组合，开始顺序预抓取。",
        slot,
        len(subs),
        len(task_ids),
    )
    # 时段预抓取只抓列表页（更快）；完整 JD 与分析留给发送阶段对入选职位逐条补抓。
    await _prepare_crawl_browser()
    results = []
    for i, tid in enumerate(task_ids):
        if i:
            await _list_crawl_pause()
        results.append(await _run_crawl_task(tid, fetch_detail=False))

    ok = sum(1 for r in results if r.get("status") == CrawlStatus.ok.value)
    failed = sum(1 for r in results if r.get("status") in (CrawlStatus.failed.value, CrawlStatus.blocked.value))
    logger.info("时段 %s 预抓取完成：成功 %d，失败/被拦 %d。", slot, ok, failed)

    # 抓完用全局默认画像跑一遍分析，面板即可看到匹配度；结果按 (职位,画像) 缓存，
    # 发送时若订阅者用同画像可直接复用，不会重复花钱。
    analyze_stats: dict | None = None
    if _settings.analyze_after_crawl:
        try:
            analyze_stats = await analyze_pending_loop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("时段 %s 预抓取后自动分析失败: %s", slot, exc)

    return {
        "slot": slot,
        "subscriptions": len(subs),
        "combos": len(task_ids),
        "ok": ok,
        "failed": failed,
        "results": results,
        "analyze": analyze_stats,
    }


async def run_slot_retry(slot: str) -> dict:
    """只重跑本批次里 failed/blocked 的抓取组合（管理员人工干预后调用）。"""
    run_date = _today()
    with session_scope() as session:
        task_ids = [
            t.id
            for t in session.exec(
                select(CrawlTask)
                .where(CrawlTask.slot == slot)
                .where(CrawlTask.run_date == run_date)
                .where(CrawlTask.status.in_([CrawlStatus.failed, CrawlStatus.blocked]))  # type: ignore[attr-defined]
            ).all()
        ]
    if not task_ids:
        logger.info("时段 %s 无失败组合可重跑。", slot)
        return {"slot": slot, "retried": 0}

    logger.info("时段 %s：重跑 %d 个失败/被拦组合。", slot, len(task_ids))
    await _prepare_crawl_browser()
    results = []
    for i, tid in enumerate(task_ids):
        if i:
            await _list_crawl_pause()
        results.append(await _run_crawl_task(tid))
    ok = sum(1 for r in results if r.get("status") == CrawlStatus.ok.value)
    return {"slot": slot, "retried": len(task_ids), "ok": ok, "results": results}


def _baseline_combos() -> set[tuple[str, str, str]]:
    """基础预热组合：每个角色取代表关键词 × 热门城市 × 该角色推荐站点（去停用站点）。"""
    from .keyword_presets import all_roles

    disabled = _settings.disabled_sites_set
    cities = _settings.baseline_cities_list or [""]
    combos: set[tuple[str, str, str]] = set()
    for role in all_roles():
        kws = role.get("keywords") or []
        if not kws:
            continue
        kw = kws[0]  # 代表关键词，避免每角色多词导致组合爆炸
        sites = [s for s in (role.get("suggest_sites") or []) if s not in disabled]
        for site in sites:
            if site not in {s.value for s in SCRAPERS.keys()}:
                continue
            for city in cities:
                combos.add((site, kw, city))
    return combos


async def run_baseline_warmup() -> dict:
    """夜间基础预热：六大类全角色 × 热门城市，只抓列表页灌入中央池，不做分析。

    目的：新用户订阅后“今日匹配”立刻有内容；完整 JD/AI 留给按需点击或发送阶段。
    """
    if not _settings.baseline_warmup_enabled:
        logger.info("基础预热未启用，跳过。")
        return {"enabled": False}

    combos = _baseline_combos()
    run_date = _today()
    slot = "baseline"
    task_ids: list[int] = []
    with session_scope() as session:
        for site_v, kw, city in sorted(combos):
            existing = session.exec(
                select(CrawlTask)
                .where(CrawlTask.slot == slot)
                .where(CrawlTask.run_date == run_date)
                .where(CrawlTask.site == site_v)
                .where(CrawlTask.keyword == kw)
                .where(CrawlTask.city == city)
            ).first()
            if existing is None:
                existing = CrawlTask(
                    slot=slot, run_date=run_date, site=site_v, keyword=kw, city=city
                )
            existing.status = CrawlStatus.pending
            existing.error = ""
            existing.updated_at = datetime.utcnow()
            session.add(existing)
            session.flush()
            task_ids.append(existing.id)

    logger.info("基础预热：聚合出 %d 个组合（仅列表页），开始顺序抓取。", len(task_ids))
    await _prepare_crawl_browser()
    results = []
    for i, tid in enumerate(task_ids):
        if i:
            await _list_crawl_pause()
        results.append(await _run_crawl_task(tid, fetch_detail=False))

    ok = sum(1 for r in results if r.get("status") == CrawlStatus.ok.value)
    new_total = sum(r.get("new", 0) for r in results)
    failed = sum(
        1 for r in results
        if r.get("status") in (CrawlStatus.failed.value, CrawlStatus.blocked.value)
    )
    logger.info("基础预热完成：组合 %d，成功 %d，失败/被拦 %d，新增职位 %d。",
                len(task_ids), ok, failed, new_total)
    return {"combos": len(task_ids), "ok": ok, "failed": failed, "new": new_total}


async def run_slot_send(slot: str) -> dict:
    """发送阶段（到点跑）：逐订阅个性化匹配 + 分析 + 发信。"""
    subs = _subs_for_slot(slot)
    if not subs:
        logger.info("时段 %s 无到点订阅，跳过发送。", slot)
        return {"slot": slot, "subscriptions": 0}

    digest_stats = []
    for s in subs:
        try:
            st = await run_subscription_digest(s.id)
            digest_stats.append({"sub": s.id, **st})
        except Exception as exc:  # noqa: BLE001
            logger.warning("订阅 %s 发信失败：%s", s.id, exc)
            digest_stats.append({"sub": s.id, "error": str(exc)})

    logger.info("时段 %s 发送完成。", slot)
    return {"slot": slot, "subscriptions": len(subs), "digests": digest_stats}
