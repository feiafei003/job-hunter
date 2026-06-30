"""就业趋势聚合：基于每日 baseline 抓取留下的 CrawlTask 时间序列，
按角色（keyword_presets）与城市维度，算出招聘活跃度（new）与在招指数（scraped）趋势。

数据源说明：
- 仅用 slot='baseline' 的 CrawlTask——它每天 03:00 系统化覆盖 28 角色 × 8 城，
  是稳定的全市场时间序列；订阅时段(daily_*)的组合随用户而变，不纳入。
- new  = 当日该组合新增入库的职位数 → "招聘活跃度"（主指标）。
- scraped = 当日抓到的职位数（受 max_jobs_per_run 截顶）→ "在招指数"（相对采样值）。
- baseline 用每个角色的“首个关键词”作抓取词，故可由关键词反查角色名。
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import select

from .config import get_settings
from .db import session_scope
from .keyword_presets import all_roles
from .models import CrawlTask

_settings = get_settings()

# 趋势升/降判定阈值（前后两半窗口 new 合计的相对变化）
_CHANGE_THRESHOLD = 0.10


def keyword_to_role_map() -> dict[str, str]:
    """构建 {角色首个关键词(小写): 角色名}。baseline 用首关键词抓取，故可反查角色。"""
    mapping: dict[str, str] = {}
    for role in all_roles():
        kws = role.get("keywords") or []
        if not kws:
            continue
        mapping[str(kws[0]).strip().lower()] = role["role"]
    return mapping


def _direction(series_new: list[int]) -> tuple[str, float]:
    """按时间序列的前后两半 new 合计，给出 (direction, change_pct)。"""
    n = len(series_new)
    if n < 2:
        return "flat", 0.0
    half = n // 2
    earlier = sum(series_new[:half])
    recent = sum(series_new[half:])
    if earlier == 0:
        if recent > 0:
            return "up", 100.0
        return "flat", 0.0
    change = (recent - earlier) / earlier
    pct = round(change * 100, 1)
    if change >= _CHANGE_THRESHOLD:
        return "up", pct
    if change <= -_CHANGE_THRESHOLD:
        return "down", pct
    return "flat", pct


def compute_overview(days: int = 30, city: str = "") -> dict:
    """聚合最近 days 天的 baseline 数据，返回总活跃度与各角色趋势。"""
    days = max(1, int(days))
    city = (city or "").strip()
    start = (date.today() - timedelta(days=days - 1)).isoformat()

    kw_role = keyword_to_role_map()

    with session_scope() as session:
        stmt = (
            select(CrawlTask)
            .where(CrawlTask.slot == "baseline")
            .where(CrawlTask.run_date >= start)
        )
        if city:
            stmt = stmt.where(CrawlTask.city == city)
        tasks = list(session.exec(stmt).all())

    # 逐日全市场合计 + 角色×日 聚合
    daily_acc: dict[str, dict[str, int]] = {}
    role_day: dict[str, dict[str, dict[str, int]]] = {}
    dates: set[str] = set()

    for t in tasks:
        d = t.run_date
        dates.add(d)
        dt = daily_acc.setdefault(d, {"new": 0, "scraped": 0})
        dt["new"] += t.new or 0
        dt["scraped"] += t.scraped or 0

        role = kw_role.get((t.keyword or "").strip().lower())
        if not role:
            continue
        rd = role_day.setdefault(role, {})
        cell = rd.setdefault(d, {"new": 0, "scraped": 0})
        cell["new"] += t.new or 0
        cell["scraped"] += t.scraped or 0

    sorted_dates = sorted(dates)
    daily_total = [
        {
            "date": d,
            "new": daily_acc[d]["new"],
            "scraped": daily_acc[d]["scraped"],
        }
        for d in sorted_dates
    ]

    roles = []
    for role, by_day in role_day.items():
        series = [
            {
                "date": d,
                "new": by_day.get(d, {}).get("new", 0),
                "scraped": by_day.get(d, {}).get("scraped", 0),
            }
            for d in sorted_dates
        ]
        new_total = sum(p["new"] for p in series)
        scraped_total = sum(p["scraped"] for p in series)
        direction, change_pct = _direction([p["new"] for p in series])
        roles.append(
            {
                "role": role,
                "new_total": new_total,
                "scraped_total": scraped_total,
                "series": series,
                "change_pct": change_pct,
                "direction": direction,
            }
        )

    roles.sort(key=lambda r: (r["new_total"], r["scraped_total"]), reverse=True)

    return {
        "days": days,
        "city": city,
        "cities": _settings.baseline_cities_list,
        "as_of": sorted_dates[-1] if sorted_dates else date.today().isoformat(),
        "enough_data": len(sorted_dates) >= 3,
        "daily_total": daily_total,
        "roles": roles,
    }
