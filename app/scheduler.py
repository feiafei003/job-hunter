"""基于 APScheduler 的定时任务：按每条启用的 SearchConfig 周期抓取+分析。"""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import select

from .browser import heartbeat
from .config import get_settings
from .db import session_scope
from .models import ScheduleUnit, SearchConfig, Subscription
from .services import (
    run_and_analyze,
    run_baseline_warmup,
    run_slot_crawl,
    run_slot_retry,
    run_slot_send,
)

logger = logging.getLogger("jobhunter.scheduler")
_settings = get_settings()

_UNIT_KW = {
    ScheduleUnit.minutes: "minutes",
    ScheduleUnit.hours: "hours",
    ScheduleUnit.days: "days",
}

# 固定发送时段 -> cron 参数（发送时间 T）。时间按北京时间（见调度器 timezone）。
# 注：slot 的键名（daily_09 等）作为稳定标识符保留以兼容历史订阅，实际发送时间以此处为准。
_SLOT_CRON = {
    "daily_09": {"hour": 10, "minute": 0},
    "daily_21": {"hour": 21, "minute": 0},
    "weekday_09": {"day_of_week": "mon-fri", "hour": 10, "minute": 0},
    "weekly_mon_09": {"day_of_week": "mon", "hour": 10, "minute": 0},
}

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _shift_day(token: str) -> str:
    """把 day_of_week 规格整体往前挪一天，处理跨午夜的预抓取。"""

    def back(day: str) -> str:
        day = day.strip().lower()
        if day not in _WEEKDAYS:
            return day
        return _WEEKDAYS[(_WEEKDAYS.index(day) - 1) % 7]

    parts = []
    for piece in token.split(","):
        piece = piece.strip()
        if "-" in piece:
            a, b = piece.split("-", 1)
            parts.append(f"{back(a)}-{back(b)}")
        else:
            parts.append(back(piece))
    return ",".join(parts)


def _shift_cron(params: dict, minutes_back: int) -> dict:
    """把发送时段的 cron 参数往前挪 minutes_back 分钟，得到预抓取时间。"""
    out = dict(params)
    total = out.get("hour", 0) * 60 + out.get("minute", 0) - max(0, minutes_back)
    crossed = total < 0
    total %= 24 * 60
    out["hour"] = total // 60
    out["minute"] = total % 60
    if crossed and "day_of_week" in out:
        out["day_of_week"] = _shift_day(out["day_of_week"])
    return out


def _job_id(config_id: int) -> str:
    return f"search-{config_id}"


def _slot_crawl_job_id(slot: str) -> str:
    return f"slot-crawl-{slot}"


def _slot_send_job_id(slot: str) -> str:
    return f"slot-send-{slot}"


async def _run_job(config_id: int) -> None:
    try:
        stats = await run_and_analyze(config_id)
        logger.info("定时任务完成 config=%s stats=%s", config_id, stats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("定时任务失败 config=%s: %s", config_id, exc)


async def _run_slot_crawl(slot: str) -> None:
    try:
        stats = await run_slot_crawl(slot)
        logger.info(
            "时段预抓取完成 slot=%s combos=%s failed=%s",
            slot,
            stats.get("combos"),
            stats.get("failed"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("时段预抓取失败 slot=%s: %s", slot, exc)


async def _run_slot_send(slot: str) -> None:
    try:
        stats = await run_slot_send(slot)
        logger.info("时段发送完成 slot=%s subs=%s", slot, stats.get("subscriptions"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("时段发送失败 slot=%s: %s", slot, exc)


async def _run_slot_retry(slot: str) -> None:
    try:
        stats = await run_slot_retry(slot)
        logger.info("时段重跑完成 slot=%s retried=%s", slot, stats.get("retried"))
    except Exception as exc:  # noqa: BLE001
        logger.exception("时段重跑失败 slot=%s: %s", slot, exc)


async def _run_baseline_warmup() -> None:
    try:
        stats = await run_baseline_warmup()
        logger.info("基础预热完成 %s", stats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("基础预热失败：%s", exc)


async def _run_heartbeat() -> None:
    try:
        stats = await heartbeat()
        if stats:
            logger.info("会话心跳完成：%s", stats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("会话心跳失败：%s", exc)


class SchedulerService:
    def __init__(self) -> None:
        # 固定时段按北京时间触发，避免依赖服务器本地时区
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Shanghai"))

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()
        self.reload_jobs()
        logger.info("调度器已启动")

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def reload_jobs(self) -> None:
        """根据数据库中启用的配置，重建所有定时任务。"""
        for job in self._scheduler.get_jobs():
            job.remove()

        with session_scope() as session:
            configs = list(
                session.exec(
                    select(SearchConfig).where(SearchConfig.enabled == True)  # noqa: E712
                ).all()
            )

        for cfg in configs:
            kwargs = {_UNIT_KW[cfg.unit]: max(1, cfg.interval)}
            self._scheduler.add_job(
                _run_job,
                trigger="interval",
                id=_job_id(cfg.id),
                args=[cfg.id],
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                **kwargs,
            )

        # 订阅用到的固定时段，每个建一个 cron 任务
        with session_scope() as session:
            subs = list(
                session.exec(
                    select(Subscription).where(Subscription.enabled == True)  # noqa: E712
                ).all()
            )
        used_slots: set[str] = set()
        for s in subs:
            for slot in (s.send_slots or "").split(","):
                slot = slot.strip()
                if slot in _SLOT_CRON:
                    used_slots.add(slot)
        lead = max(0, _settings.crawl_lead_minutes)
        # 漏发补跑窗口：发送/抓取时刻若服务恰好在重启/宕机，恢复后 6 小时内仍补跑一次
        # （coalesce=True 会把多次漏触发合并为一次），避免"当天没起在点上就整天不发"。
        GRACE = 6 * 3600
        for slot in used_slots:
            # 发送时间前 lead 分钟：预抓取
            self._scheduler.add_job(
                _run_slot_crawl,
                trigger=CronTrigger(**_shift_cron(_SLOT_CRON[slot], lead)),
                id=_slot_crawl_job_id(slot),
                args=[slot],
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=GRACE,
            )
            # 发送时间 T：逐订阅发信
            self._scheduler.add_job(
                _run_slot_send,
                trigger=CronTrigger(**_SLOT_CRON[slot]),
                id=_slot_send_job_id(slot),
                args=[slot],
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=GRACE,
            )

        # 夜间基础预热：每天固定时刻，按热门城市 × 全角色只抓列表灌池
        if _settings.baseline_warmup_enabled:
            self._scheduler.add_job(
                _run_baseline_warmup,
                trigger=CronTrigger(
                    hour=_settings.baseline_warmup_hour,
                    minute=_settings.baseline_warmup_minute,
                ),
                id="baseline-warmup",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )

        # 会话心跳：定时刷新已登录站点的 cookie，尽量延长登录态
        if _settings.heartbeat_enabled:
            from datetime import datetime, timedelta

            self._scheduler.add_job(
                _run_heartbeat,
                trigger="interval",
                id="session-heartbeat",
                minutes=max(1, _settings.heartbeat_minutes),
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                # 启动后约 1 分钟先跑一次，便于即时验证
                next_run_time=datetime.now() + timedelta(seconds=60),
            )

        logger.info(
            "已加载 %d 个抓取任务 + %d 个订阅时段（预抓取+发送）任务%s",
            len(configs),
            len(used_slots),
            f" + 会话心跳每 {max(1, _settings.heartbeat_minutes)} 分钟"
            if _settings.heartbeat_enabled
            else "",
        )

    def trigger_now(self, config_id: int) -> None:
        """在调度器事件循环里立即跑一次（不阻塞请求）。"""
        loop = self._scheduler._eventloop  # AsyncIOScheduler 的事件循环
        if loop is None:
            asyncio.create_task(_run_job(config_id))
        else:
            asyncio.run_coroutine_threadsafe(_run_job(config_id), loop)

    def _spawn(self, coro) -> None:
        loop = self._scheduler._eventloop
        if loop is None:
            asyncio.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    def trigger_slot_crawl_now(self, slot: str) -> None:
        """立即跑一次某时段的预抓取（不发信）。"""
        self._spawn(_run_slot_crawl(slot))

    def trigger_slot_retry_now(self, slot: str) -> None:
        """立即重跑某时段失败/被拦的抓取组合。"""
        self._spawn(_run_slot_retry(slot))

    def trigger_slot_send_now(self, slot: str) -> None:
        """立即跑一次某时段的发送（逐订阅发信）。"""
        self._spawn(_run_slot_send(slot))

    def trigger_baseline_now(self) -> None:
        """立即跑一轮基础预热（列表-only 灌池）。"""
        self._spawn(_run_baseline_warmup())


scheduler_service = SchedulerService()
