"""订阅匹配引擎：在中央职位池里按订阅过滤，排除已推送过的职位。

过滤维度：站点、关键词(OR)、工作类型、地点、薪资区间。薪资为字符串
（如 "12-20k" / "1.5-2万·13薪"），尽力解析为月薪 k 区间做重叠判断；
解析不出来（如"面议"）则不因薪资过滤掉，从宽。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from sqlmodel import select

from .db import session_scope
from .models import Delivery, JobPosting, Referral, Subscription

log = logging.getLogger("jobhunter.matching")

_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*(万|w|W|k|K|千)?"
)
_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|w|W|k|K|千)")


def _unit_mult(unit: Optional[str]) -> float:
    """换算成 k（千元/月）：万/w=10，k/千=1。"""
    if not unit:
        return 1.0
    return 10.0 if unit in ("万", "w", "W") else 1.0


def _to_k(value: float, unit: Optional[str]) -> float:
    """换算成 k：带 万/k/千 单位按单位；无单位但数值很大（如 8000元）按元/月折算。"""
    if unit:
        return value * _unit_mult(unit)
    # 无单位：>=1000 视为"元/月"，折成 k
    return value / 1000.0 if value >= 1000 else value


def parse_salary_range(text: str) -> Optional[tuple[float, float]]:
    """把薪资字符串解析成 (min_k, max_k) 月薪区间；无法解析返回 None。"""
    if not text:
        return None
    m = _RANGE_RE.search(text)
    if m:
        unit = m.group(3)
        return (_to_k(float(m.group(1)), unit), _to_k(float(m.group(2)), unit))
    m = _SINGLE_RE.search(text)
    if m:
        v = float(m.group(1)) * _unit_mult(m.group(2))
        return (v, v)
    return None


def _salary_ok(job_salary: str, smin: int, smax: int) -> bool:
    if smin <= 0 and smax <= 0:
        return True
    rng = parse_salary_range(job_salary)
    if rng is None:
        return True  # 解析不出（如"面议"）从宽，不过滤
    jlo, jhi = rng
    if smin > 0 and jhi < smin:
        return False
    if smax > 0 and jlo > smax:
        return False
    return True


def _keywords_ok(haystack: str, keywords: str) -> bool:
    kws = [k.strip().lower() for k in re.split(r"[,\s，、]+", keywords or "") if k.strip()]
    if not kws:
        return True
    return any(k in haystack for k in kws)


# 明确的"非全职"类型词；招聘文本一般只对这些做标注，全职通常不写
_NON_FULLTIME = ("实习", "兼职", "外包", "派遣", "临时", "日结", "intern", "part-time", "part time")
_FULLTIME_ALIASES = ("全职", "fulltime", "full-time", "full time")
# 其它可识别的"工作性质"（按字面要求）；不在此列的值视为非性质字段（如误填的职类名）不过滤
_OTHER_NATURE = ("远程", "remote", "现场", "驻场", "混合办公", "在家办公")


def _job_type_ok(haystack: str, job_type: str) -> bool:
    """工作类型匹配（仅针对"工作性质"做过滤）。

    "全职"是默认类型，绝大多数职位文本里并不会写"全职"二字，因此不做字面要求，
    只要该职位不是明确的实习/兼职/外包等即可；实习/兼职/远程等按字面包含匹配。

    若该字段填的不是工作性质（如把"大模型算法工程师"这类职类名误填进来），
    则不作硬过滤（返回 True），避免把整池职位都筛掉——职类语义已由关键词覆盖。
    """
    jt = (job_type or "").strip().lower()
    if not jt:
        return True
    if jt in _FULLTIME_ALIASES:
        return not any(t in haystack for t in _NON_FULLTIME)
    nature_hits = [t for t in (_NON_FULLTIME + _OTHER_NATURE) if t in jt]
    if nature_hits:
        return any(t in haystack for t in nature_hits)
    return True


def match_job_ids(
    sub: Subscription, limit: int = 50, exclude_delivered: bool = True
) -> list[int]:
    """返回该订阅匹配的职位 id（按抓取时间倒序）。

    exclude_delivered=True 时排除已推送过的（发信用）；门户"今日匹配"展示可设 False。
    """
    sites = [s.strip() for s in (sub.sites or "").split(",") if s.strip()]
    location = (sub.location or "").strip().lower()
    job_type = (sub.job_type or "").strip().lower()

    with session_scope() as session:
        delivered: set[int] = set()
        if exclude_delivered:
            delivered = {
                d.job_id
                for d in session.exec(
                    select(Delivery).where(Delivery.subscription_id == sub.id)
                ).all()
            }

        stmt = select(JobPosting).order_by(JobPosting.scraped_at.desc())
        jobs = list(session.exec(stmt).all())

        matched: list[int] = []
        for job in jobs:
            if job.id in delivered:
                continue
            if sites and job.site.value not in sites:
                continue

            haystack = " ".join(
                x.lower()
                for x in (job.title, job.tags, job.description, job.company)
                if x
            )
            if not _keywords_ok(haystack, sub.keywords):
                continue
            if not _job_type_ok(haystack, job_type):
                continue
            if location:
                jl = (job.location or "").lower()
                if jl and location not in jl:
                    continue
            if not _salary_ok(job.salary, sub.salary_min, sub.salary_max):
                continue

            matched.append(job.id)
            if len(matched) >= limit:
                break
        return matched


def referrals_for_user(user_id: int, limit: int = 50) -> list[int]:
    """返回与该用户启用订阅相关的、其他人发布的内推 id（按时间倒序）。

    相关 = 命中任一启用订阅的关键词(OR)，且地点不冲突（订阅填了地点时需包含匹配）。
    订阅没填关键词则该订阅匹配全部内推。排除用户自己发布的。
    """
    with session_scope() as session:
        subs = list(
            session.exec(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .where(Subscription.enabled == True)  # noqa: E712
            ).all()
        )
        if not subs:
            return []
        refs = list(
            session.exec(
                select(Referral)
                .where(Referral.enabled == True)  # noqa: E712
                .where(Referral.user_id != user_id)
                .order_by(Referral.created_at.desc())
            ).all()
        )

        matched: list[int] = []
        for ref in refs:
            haystack = " ".join(
                x.lower()
                for x in (ref.title, ref.keywords, ref.company, ref.description)
                if x
            )
            ref_loc = (ref.location or "").lower()
            hit = False
            for sub in subs:
                if not _keywords_ok(haystack, sub.keywords):
                    continue
                sub_loc = (sub.location or "").strip().lower()
                if sub_loc and ref_loc and sub_loc not in ref_loc:
                    continue
                hit = True
                break
            if hit:
                matched.append(ref.id)
                if len(matched) >= limit:
                    break
        return matched
