"""抓取插件抽象基类与注册表。

新增站点：继承 BaseScraper，设置 site，用 @register 装饰，实现 search()。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Type

from playwright.async_api import Page

from ..browser import interactive_login, open_page
from ..config import get_settings
from ..models import Site

_settings = get_settings()


class ScrapeBlockedError(Exception):
    """抓取被反爬/登录墙拦截（验证码、登录重定向等），区别于"0 结果"。

    预抓取阶段据此把组合标记为 blocked，提示管理员人工干预后重跑。
    """


@dataclass
class RawJob:
    """抓取阶段的中间结构，落库前转换为 JobPosting。"""

    title: str = ""
    company: str = ""
    salary: str = ""
    location: str = ""
    experience: str = ""
    education: str = ""
    tags: str = ""
    description: str = ""
    url: str = ""
    extra: dict = field(default_factory=dict)

    def fingerprint(self, site: str) -> str:
        """优先用 URL 去重；没有 URL 时退化用 站点+标题+公司。

        关键：去掉 URL 的查询参数与锚点（如智联的 preactionid/refcode 每次都变），
        只用 scheme+host+path 作为稳定标识，避免同一职位每抓一次就新增一条重复记录。
        """
        u = (self.url or "").strip()
        if u:
            try:
                from urllib.parse import urlsplit, urlunsplit

                p = urlsplit(u)
                u = urlunsplit((p.scheme, p.netloc, p.path, "", "")) or u
            except Exception:  # noqa: BLE001
                pass
        basis = u or f"{site}|{self.title}|{self.company}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class BaseScraper(ABC):
    site: Site
    login_url: str = ""
    # 某些站点（如 BOSS）无头会被反爬识破返回空白，必须有界面运行。
    force_headed: bool = False
    # 登录窗口是否等用户手动关闭（而非按 URL 自动判定登录成功）。
    login_manual_close: bool = False

    async def human_pause(self) -> None:
        """随机延迟，模拟人类操作节奏。"""
        await asyncio.sleep(
            random.uniform(_settings.scrape_min_delay, _settings.scrape_max_delay)
        )

    async def human_scroll(self, page: Page, steps: int = 3) -> None:
        """逐步滚动页面，触发懒加载并显得更自然（偶尔回滚一点，像真人重读）。"""
        for _ in range(steps):
            await page.mouse.wheel(0, random.randint(300, 800))
            await asyncio.sleep(random.uniform(0.6, 1.6))
            if random.random() < 0.25:
                await page.mouse.wheel(0, -random.randint(80, 220))
                await asyncio.sleep(random.uniform(0.3, 0.9))

    async def human_mouse_move(self, page: Page, moves: int = 0) -> None:
        """随机移动鼠标若干次，制造真实鼠标轨迹。

        反爬的行为评分会看 mousemove 是否为可信(trusted)事件、轨迹是否自然。
        程序触发的 move 带 steps 即逐点移动，比瞬移更像真人。
        """
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
        except Exception:  # noqa: BLE001
            vp = {"width": 1280, "height": 800}
        w = int(vp.get("width", 1280) or 1280)
        h = int(vp.get("height", 800) or 800)
        if moves <= 0:
            moves = random.randint(2, 5)
        for _ in range(moves):
            tx = random.randint(int(w * 0.05), int(w * 0.95))
            ty = random.randint(int(h * 0.1), int(h * 0.9))
            try:
                await page.mouse.move(tx, ty, steps=random.randint(8, 24))
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(random.uniform(0.05, 0.35))

    async def human_engage(self, page: Page) -> None:
        """模拟真人在页面上的浏览：移动鼠标 + 滚动 + 不规律停留。

        给反爬的交互埋点（mousemove/scroll/停留时长）喂真实信号，提升行为评分。
        """
        await self.human_mouse_move(page)
        await self.human_scroll(page, steps=random.randint(2, 4))
        await asyncio.sleep(random.uniform(0.8, 2.5))

    # EdgeOne / 腾讯云"确认您是真人"验证页特征（命中即认为在验证页）
    _HUMAN_CHECK_MARKERS = (
        "确认您是真人",
        "Tencent Cloud EdgeOne",
        "TEOJsChallenge",
        "widget_ele_eo",
        "Verifying the safety of the connection",
    )

    async def looks_human_check(self, page: Page) -> bool:
        """页面是否停在 EdgeOne/腾讯云的『确认您是真人』验证页。"""
        try:
            html = await page.content()
        except Exception:  # noqa: BLE001
            return False
        return any(m in html for m in self._HUMAN_CHECK_MARKERS)

    async def _find_human_checkbox(self, page: Page):
        """在主框架与所有 iframe 里找『确认您是真人』复选框/可点控件。"""
        # 验证页内容很简单，用较宽松的候选选择器即可
        sels = [
            "input[type=checkbox]",
            "[role='checkbox']",
            "[class*='check'] [class*='box']",
            "[class*='verify'] [class*='check']",
            "[id*='check']",
            "label",
        ]
        for fr in page.frames:
            for sel in sels:
                try:
                    for el in await fr.query_selector_all(sel):
                        try:
                            if await el.is_visible():
                                return el
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def try_pass_human_checkbox(self, page: Page, timeout: float = 25.0) -> bool:
        """尝试自动勾选 EdgeOne『确认您是真人』复选框。

        勾选本身不难，能否通过取决于环境是否干净（rebrowser 已消除 CDP 特征）；
        这里用真人鼠标轨迹移动到复选框再点击，给行为风控喂真实信号。
        若升级成图片拼图（找不到复选框）则无法自动处理，返回 False（需人工/跳过）。

        返回 True 表示验证已通过（页面已离开验证页）。
        """
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not await self.looks_human_check(page):
                return True
            el = await self._find_human_checkbox(page)
            if el is not None:
                try:
                    box = await el.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        # 先移到附近再移到中心，制造自然轨迹，然后点击
                        await page.mouse.move(
                            cx - random.randint(20, 60),
                            cy - random.randint(10, 30),
                            steps=random.randint(10, 20),
                        )
                        await asyncio.sleep(random.uniform(0.2, 0.6))
                        await page.mouse.move(cx, cy, steps=random.randint(6, 14))
                        await asyncio.sleep(random.uniform(0.1, 0.4))
                        await page.mouse.click(cx, cy)
                    else:
                        await el.click(timeout=2000)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(1.5)
        return not await self.looks_human_check(page)

    async def dump_debug(self, page: Page, tag: str = "") -> str:
        """把当前页面 HTML + 截图存到 data/debug/，便于排查选择器/反爬。"""
        import logging
        import time

        debug_dir = _settings.data_path / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = debug_dir / f"{self.site.value}_{tag}_{ts}"
        try:
            html = await page.content()
            base.with_suffix(".html").write_text(html, encoding="utf-8")
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("jobhunter.scrapers").warning("转储调试页面失败: %s", exc)
        logging.getLogger("jobhunter.scrapers").info(
            "已转储调试页面: %s (url=%s)", base, page.url
        )
        return str(base)

    async def login(self, wait_seconds: int = 180) -> None:
        if not self.login_url:
            raise RuntimeError(f"{self.site} 未配置 login_url")
        await interactive_login(
            self.site.value,
            self.login_url,
            wait_seconds,
            wait_for_manual_close=self.login_manual_close,
        )

    # 单次抓取的“是否进详情页”覆盖：None=用全局 scrape_fetch_detail；True/False=强制。
    _fetch_detail_override: "bool | None" = None

    def _should_fetch_detail(self) -> bool:
        """各站详情步骤统一调用此方法判定，支持按调用覆盖全局开关。"""
        if self._fetch_detail_override is not None:
            return self._fetch_detail_override
        return bool(_settings.scrape_fetch_detail)

    async def search(
        self,
        keyword: str,
        city: str = "",
        salary: str = "",
        limit: int = 20,
        date_range: str = "any",
        fetch_detail: "bool | None" = None,
    ) -> List[RawJob]:
        """子类通过 _search(page,...) 实现具体逻辑；这里负责开页面与限量。

        fetch_detail=False 时只抓列表页（基础预热/快速预抓取用），不进详情页。
        """
        self._fetch_detail_override = fetch_detail
        results: List[RawJob] = []
        headless = False if self.force_headed else None
        try:
            async with open_page(self.site.value, headless=headless) as page:
                results = await self._search(page, keyword, city, salary, limit, date_range)
        finally:
            self._fetch_detail_override = None
        return results[:limit]

    async def fetch_detail_for(self, url: str, referer: str = "") -> dict:
        """按需抓取单条职位详情页，返回 {description}（取不到则空 dict）。

        打开一个页面导航到 url，再交给子类的 _extract_detail_jd 提取完整 JD。
        不支持详情的站点（_extract_detail_jd 返回空）会得到空结果。
        """
        if not url:
            return {}
        headless = False if self.force_headed else None
        async with open_page(self.site.value, headless=headless) as page:
            try:
                await page.goto(
                    url,
                    referer=referer or None,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await self.human_mouse_move(page)
                desc = await self._extract_detail_jd(page)
            except Exception:  # noqa: BLE001
                return {}
        return {"description": desc} if desc else {}

    async def _extract_detail_jd(self, page: Page) -> str:
        """从已打开的详情页提取完整 JD。默认空；支持详情的站点覆盖。"""
        return ""

    @abstractmethod
    async def _search(
        self,
        page: Page,
        keyword: str,
        city: str,
        salary: str,
        limit: int,
        date_range: str = "any",
    ) -> List[RawJob]:
        ...


SCRAPERS: Dict[Site, BaseScraper] = {}


def register(cls: Type[BaseScraper]) -> Type[BaseScraper]:
    SCRAPERS[cls.site] = cls()
    return cls


def get_scraper(site: Site) -> BaseScraper:
    scraper = SCRAPERS.get(site)
    if scraper is None:
        raise KeyError(f"未注册的站点: {site}")
    return scraper
