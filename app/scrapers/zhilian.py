"""智联招聘抓取插件。

流程：搜索列表页提取卡片基本信息（职位名/公司/薪资/地点/经验/学历/标签），
再逐个进入职位详情页抓取完整 JD。详情页不登录也可浏览，偶尔弹出的登录框
直接关闭/移除遮罩即可，无需登录；只有真正命中滑块/安全验证时才跳过该条。
DOM 改版时调整选择器即可。
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import List
from urllib.parse import quote

from playwright.async_api import Page

from ..browser import cdp_enabled, open_context
from ..config import get_settings
from ..models import Site
from .base import BaseScraper, RawJob, ScrapeBlockedError, register

_settings = get_settings()
log = logging.getLogger("jobhunter.scrapers")

_SEARCH_URL = "https://sou.zhaopin.com/?kw={kw}"
_SEARCH_URL_CITY = "https://sou.zhaopin.com/?jl={city}&kw={kw}"

_CARD_SELECTORS = [
    "div.joblist-box__item",
    "div.positionlist .position-card",
    "div[class*='joblist'] div[class*='item']",
]

# 详情页 JD 容器候选选择器（智联多次改版，按命中顺序尝试）
_JD_SELECTORS = [
    ".describtion__detail-content",
    "div.describtion",
    "[class*='describtion']",
    "[class*='job-description']",
    "[class*='describe']",
    "div.pos-ul",
    "[class*='job-detail']",
]

# 登录弹窗/遮罩关闭按钮候选
_LOGIN_CLOSE_SELECTORS = [
    ".zppp-panel__close",
    ".zppp-panel-close",
    ".zp-login__close",
    "[class*='login'] [class*='close']",
    "[class*='dialog'] [class*='close']",
    ".risk-close",
]

# 命中即认为是滑块/安全验证（无法自动绕过，跳过该条）
_VERIFY_SELECTORS = [
    "#nc_1_wrapper",
    ".nc-container",
    "[class*='captcha']",
    "[id*='captcha']",
    ".geetest_panel",
]


# 一次性把所有卡片信息抓回来（CDP 读已打开标签时用），避免多次往返被刷新打断。
_EXTRACT_JS = """
(S) => {
  const pick = (root, sels) => {
    for (const s of sels) {
      try { const el = root.querySelector(s);
        if (el) { const t = (el.innerText || '').trim(); if (t) return t; }
      } catch (e) {}
    }
    return '';
  };
  const pickAll = (root, sel) => {
    try { return Array.from(root.querySelectorAll(sel)).map(e => (e.innerText || '').trim()).filter(Boolean); }
    catch (e) { return []; }
  };
  let cards = [];
  for (const cs of S.card) {
    try { const f = Array.from(document.querySelectorAll(cs)); if (f.length) { cards = f; break; } } catch (e) {}
  }
  return cards.map(c => {
    let href = '';
    try { const a = c.querySelector("a.jobinfo__name, a[href*='jobdetail']"); if (a) href = a.getAttribute('href') || ''; } catch (e) {}
    return {
      title: pick(c, S.title),
      company: pick(c, S.company),
      salary: pick(c, S.salary),
      info: pickAll(c, S.info),
      tags: pickAll(c, S.tag),
      href: href,
    };
  });
}
"""


async def _text(node, selectors: list[str]) -> str:
    for sel in selectors:
        el = await node.query_selector(sel)
        if el:
            txt = (await el.inner_text()).strip()
            if txt:
                return txt
    return ""


async def _texts(node, selector: str) -> list[str]:
    out = []
    for el in await node.query_selector_all(selector):
        t = (await el.inner_text()).strip()
        if t:
            out.append(t)
    return out


@register
class ZhilianScraper(BaseScraper):
    site = Site.zhilian
    login_url = "https://passport.zhaopin.com/login"

    async def search(
        self,
        keyword: str,
        city: str = "",
        salary: str = "",
        limit: int = 20,
        date_range: str = "any",
        fetch_detail: "bool | None" = None,
    ) -> List[RawJob]:
        """CDP 模式：接管你登录好的真实 Chrome，程序自己导航搜索并读取列表，
        再（可选）慢速逐条进详情页补全完整 JD。

        非 CDP 模式仍走父类（新开页 + 导航 + 详情补全）逻辑。
        fetch_detail=False 时只抓列表页（基础预热/快速预抓取）。
        """
        self._fetch_detail_override = fetch_detail
        if not cdp_enabled():
            return await super().search(keyword, city, salary, limit, date_range, fetch_detail=fetch_detail)

        if city:
            url = _SEARCH_URL_CITY.format(city=quote(city), kw=quote(keyword))
        else:
            url = _SEARCH_URL.format(kw=quote(keyword))

        async with open_context(self.site.value) as context:
            # 每次抓取用独立标签（并发安全），用完关闭，避免和你/其它任务标签互相打断。
            page = await context.new_page()
            log.info("CDP 驱动调试 Chrome 搜索：%s", url)
            jobs: List[RawJob] = []
            try:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)
                    # 命中 EdgeOne『确认您是真人』复选框：用真人轨迹自动勾选过验证
                    if await self.looks_human_check(page):
                        log.info("智联命中『确认您是真人』验证，尝试自动勾选…")
                        ok = await self.try_pass_human_checkbox(page, timeout=25.0)
                        log.info("智联真人验证%s。", "已通过" if ok else "未通过（可能升级为图片验证）")
                        await asyncio.sleep(1.5)
                    jobs = (await self._extract_from_page_cdp(page, city, limit))[:limit]
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "zhilian CDP 读取异常（页面可能在刷新/验证）：%s", str(exc)[:120]
                    )
                    return []

                # 列表为空且页面命中验证/反爬：标记为 blocked，提示人工干预后重跑
                if not jobs and await self._looks_blocked(page):
                    raise ScrapeBlockedError("智联列表为空且检测到反爬/安全验证，需登录或过验证后重跑")

                if self._should_fetch_detail() and jobs:
                    try:
                        await self._enrich_cdp(context, jobs, referer=url)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("CDP 详情补全异常：%s", str(exc)[:120])
                return jobs
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _enrich_cdp(
        self, context, jobs: List[RawJob], referer: str = ""
    ) -> None:
        """CDP 真实 Chrome 里慢速逐条补全完整 JD。

        真实 Chrome 中 EdgeOne 的 JS 验证会在数秒内自动放行，故每条给较长等待；
        条与条之间用 scrape_detail_*_delay 拉长间隔，单次限量 + 连续被拦熔断。

        反爬规避要点：
        - 进详情页带上列表页 Referer，伪装成"从搜索结果点进去"，而非凭空直达；
        - 加载后做真人浏览动作（移动鼠标 / 滚动 / 停留），喂真实交互埋点；
        - 被拦时指数退避，避免越刷越被限流。
        """
        targets = [j for j in jobs if j.url and not j.description][
            : max(0, _settings.scrape_detail_max)
        ]
        if not targets:
            return
        log.info(
            "CDP 详情补全开始：本次最多 %d 条，每条间隔 %.0f-%.0f 秒，慢速进行。",
            len(targets),
            _settings.scrape_detail_min_delay,
            _settings.scrape_detail_max_delay,
        )

        fetched = 0
        consecutive_blocked = 0
        for idx, job in enumerate(targets):
            if idx > 0:
                await self._detail_pause()
                # 上一条被拦：在常规间隔之外再叠加退避，给风控降温
                if consecutive_blocked:
                    backoff = min(180.0, 20.0 * (2 ** consecutive_blocked))
                    backoff *= random.uniform(0.8, 1.2)
                    log.info("上一条被拦，额外退避 %.0f 秒后再试。", backoff)
                    await asyncio.sleep(backoff)

            detail = await context.new_page()
            try:
                await detail.goto(
                    job.url,
                    referer=referer or None,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                # 加载后先做点真人动作，再等 JD（也给 EdgeOne 的 JS 挑战放行时间）
                await self.human_mouse_move(detail)
                desc = await self._wait_for_jd(detail, timeout=30.0)
                if desc:
                    # 像真人一样读一会儿、滚一滚
                    await self.human_scroll(detail, steps=random.randint(1, 3))
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                    job.description = desc
                    fetched += 1
                    consecutive_blocked = 0
                    log.info("详情补全成功（%d/%d）：%s", fetched, len(targets), job.title)
                else:
                    consecutive_blocked += 1
                    if await self._looks_blocked(detail):
                        log.warning("详情页卡在反爬/验证挑战，跳过：%s", job.url)
                    else:
                        log.warning("详情页未匹配到 JD：%s", job.url)
            except Exception as exc:  # noqa: BLE001
                log.warning("抓取详情失败 %s: %s", job.url, exc)
            finally:
                # 不要秒关：像真人读完页面再离开，停留几秒
                try:
                    await asyncio.sleep(random.uniform(3.0, 7.0))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await detail.close()
                except Exception:  # noqa: BLE001
                    pass

            if consecutive_blocked >= _settings.scrape_detail_block_giveup:
                log.warning(
                    "连续 %d 条被拦，停止本次详情补全（避免触发限流）。",
                    consecutive_blocked,
                )
                break

        log.info("CDP 详情补全完成：%d/%d 条获得完整 JD。", fetched, len(targets))

    async def _extract_from_page_cdp(
        self, page: Page, city: str, limit: int
    ) -> List[RawJob]:
        # 等渲染稳定
        for _ in range(15):
            try:
                blen = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').length"
                )
            except Exception:
                blen = 0
            if blen and blen > 200:
                break
            await asyncio.sleep(1.0)

        sel_arg = {
            "card": _CARD_SELECTORS,
            "title": ["a.jobinfo__name", "[class*='jobinfo__name']"],
            "company": ["a.companyinfo__name", "[class*='companyinfo__name']"],
            "salary": ["p.jobinfo__salary", "[class*='jobinfo__salary']"],
            "info": ".jobinfo__other-info-item",
            "tag": ".jobinfo__tag .joblist-box__item-tag",
        }

        raw = None
        for attempt in range(4):
            try:
                raw = await page.evaluate(_EXTRACT_JS, sel_arg)
                if raw:
                    break
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "zhilian 提取被页面跳转打断，重试 %d/4：%s", attempt + 1, str(exc)[:60]
                )
            await asyncio.sleep(2.0)

        if not raw:
            try:
                await self.dump_debug(page, tag="nocards")
            except Exception:  # noqa: BLE001
                pass
            url = ""
            try:
                url = page.url
            except Exception:  # noqa: BLE001
                pass
            log.warning("zhilian 未读到职位卡片（页面可能在刷新/验证中）。URL=%s", url)
            return []

        jobs: List[RawJob] = []
        for item in raw[:limit]:
            title = (item.get("title") or "").strip()
            company = (item.get("company") or "").strip()
            if not (title or company):
                continue
            href = item.get("href") or ""
            if href.startswith("//"):
                href = "https:" + href
            info = item.get("info") or []
            tags = item.get("tags") or []
            jobs.append(
                RawJob(
                    title=title,
                    company=company,
                    salary=(item.get("salary") or "").strip(),
                    location=(info[0] if len(info) > 0 else "") or city,
                    experience=info[1] if len(info) > 1 else "",
                    education=info[2] if len(info) > 2 else "",
                    tags=", ".join(dict.fromkeys(tags)),
                    description="",
                    url=href,
                )
            )

        log.info("zhilian 读到 %d 条职位（列表级，共抓到 %d 个卡片）。", len(jobs), len(raw))
        return jobs

    async def _search(
        self,
        page: Page,
        keyword: str,
        city: str,
        salary: str,
        limit: int,
        date_range: str = "any",
    ) -> List[RawJob]:
        if city:
            url = _SEARCH_URL_CITY.format(city=quote(city), kw=quote(keyword))
        else:
            url = _SEARCH_URL.format(kw=quote(keyword))

        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await self.human_pause()
        await self.human_scroll(page, steps=4)

        cards = []
        for sel in _CARD_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=4000)
            except Exception:
                pass
            cards = await page.query_selector_all(sel)
            if cards:
                log.info("zhilian 命中选择器 '%s'，卡片数=%d", sel, len(cards))
                break

        if not cards:
            await self.dump_debug(page, tag="nocards")
            log.warning("zhilian 未匹配到职位卡片，已转储页面。URL=%s", page.url)
            return []

        jobs: List[RawJob] = []
        for card in cards[:limit]:
            try:
                title = await _text(card, ["a.jobinfo__name", "[class*='jobinfo__name']"])
                company = await _text(
                    card, ["a.companyinfo__name", "[class*='companyinfo__name']"]
                )
                salary_txt = await _text(
                    card, ["p.jobinfo__salary", "[class*='jobinfo__salary']"]
                )
                info_items = await _texts(card, ".jobinfo__other-info-item")
                location = info_items[0] if len(info_items) > 0 else city
                experience = info_items[1] if len(info_items) > 1 else ""
                education = info_items[2] if len(info_items) > 2 else ""
                tags = await _texts(card, ".jobinfo__tag .joblist-box__item-tag")

                link_el = await card.query_selector(
                    "a.jobinfo__name, a[href*='jobdetail']"
                )
                href = await link_el.get_attribute("href") if link_el else ""
                if href and href.startswith("//"):
                    href = "https:" + href

                if not (title or company):
                    continue
                jobs.append(
                    RawJob(
                        title=title,
                        company=company,
                        salary=salary_txt,
                        location=location or city,
                        experience=experience,
                        education=education,
                        tags=", ".join(tags),
                        url=href or "",
                    )
                )
            except Exception:
                continue

        # 逐个进详情页补全完整 JD（关闭登录弹窗，不登录）
        if self._should_fetch_detail():
            await self._enrich_with_details(page, jobs)

        return jobs

    async def _dismiss_login(self, page: Page) -> None:
        """关闭可能弹出的登录框/遮罩：先按 Esc，再点关闭按钮，最后 JS 兜底移除遮罩。"""
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        for sel in _LOGIN_CLOSE_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click(timeout=1000)
                    break
            except Exception:
                continue
        # JS 兜底：移除常见登录遮罩/弹层，恢复滚动
        try:
            await page.evaluate(
                """
                () => {
                  const sels = ['.zppp-panel','.zp-login','[class*="login-dialog"]',
                                '.zppp-modal','.modal-mask','.zppp-overlay'];
                  sels.forEach(s => document.querySelectorAll(s).forEach(e => e.remove()));
                  document.body.style.overflow = 'auto';
                }
                """
            )
        except Exception:
            pass

    # 按文本锚点提取 JD：找含"职位描述/岗位职责/任职要求"的最内层内容块，
    # 不依赖会变动的 class，页面只要正常加载就能抓到。
    _JD_JS = r"""
    () => {
      const markers = ['职位描述','岗位职责','任职要求','岗位要求','工作职责','职位信息'];
      const nodes = Array.from(document.querySelectorAll('div,section,article,ul,dd'));
      let best = '';
      for (const el of nodes) {
        const txt = (el.innerText || '').trim();
        if (txt.length < 60 || txt.length > 6000) continue;
        if (!markers.some(m => txt.includes(m))) continue;
        // 选"最内层"：没有任何子元素单独包含同样多的文本
        const childMax = Math.max(0, ...Array.from(el.children).map(c => (c.innerText||'').trim().length));
        if (childMax >= txt.length - 5) continue;  // 文本主要来自某个子节点，往里钻
        if (best === '' || txt.length < best.length) best = txt;  // 取最紧凑的块
      }
      return best;
    }
    """

    async def _extract_jd(self, page: Page) -> str:
        # 先试固定选择器（快），再退回文本锚点（稳）
        for sel in _JD_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if len(txt) >= 60:
                        return txt
            except Exception:
                continue
        try:
            txt = await page.evaluate(self._JD_JS)
            if txt and len(txt.strip()) >= 60:
                return txt.strip()
        except Exception:
            pass
        return ""

    async def _extract_detail_jd(self, page: Page) -> str:
        """按需单条详情：等待并提取 JD（复用列表补全的等待逻辑）。"""
        return await self._wait_for_jd(page, timeout=20.0)

    async def _wait_for_jd(self, page: Page, timeout: float) -> str:
        """轮询等待 JD 出现（期间关掉登录弹窗）。timeout 较长时也用于等用户手动过验证。"""
        import time

        deadline = time.monotonic() + timeout
        # 详情页若命中『确认您是真人』，先自动勾选过验证
        if await self.looks_human_check(page):
            await self.try_pass_human_checkbox(page, timeout=20.0)
        while time.monotonic() < deadline:
            jd = await self._extract_jd(page)
            if jd:
                return jd
            await self._dismiss_login(page)
            await asyncio.sleep(1.0)
        return await self._extract_jd(page)

    async def _looks_blocked(self, page: Page) -> bool:
        try:
            html = await page.content()
        except Exception:
            return False
        markers = ("TEOJsChallenge", "widget_ele_eo", "captcha", "安全验证", "nc-container")
        return any(m in html for m in markers)

    async def _detail_pause(self) -> None:
        """详情页之间的长间隔，尽量避免触发 EdgeOne 限流。"""
        lo = _settings.scrape_detail_min_delay
        hi = max(lo, _settings.scrape_detail_max_delay)
        await asyncio.sleep(random.uniform(lo, hi))

    async def _enrich_with_details(self, page: Page, jobs: List[RawJob]) -> None:
        context = page.context
        # 用列表页 URL 作为详情页 Referer，伪装成从搜索结果点进去
        referer = ""
        try:
            referer = page.url
        except Exception:  # noqa: BLE001
            pass
        # 只抓还没有描述的，且单次限量，降低触发反爬的概率
        targets = [j for j in jobs if j.url and not j.description][
            : max(0, _settings.scrape_detail_max)
        ]
        if not targets:
            log.info("无需补全详情（无链接或已达单次详情上限）。")
            return

        headed = not _settings.scrape_headless
        log.info(
            "开始详情页补全：本次最多 %d 条，每条间隔 %.0f-%.0f 秒%s。",
            len(targets),
            _settings.scrape_detail_min_delay,
            _settings.scrape_detail_max_delay,
            "（有界面，遇验证请手动点一下）" if headed else "（无头，遇验证将跳过）",
        )

        fetched = 0
        blocked = 0
        consecutive_blocked = 0
        cleared = False
        dumped = False

        for idx, job in enumerate(targets):
            if idx > 0:
                await self._detail_pause()

            detail = await context.new_page()
            try:
                await detail.goto(
                    job.url,
                    referer=referer or None,
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await self.human_mouse_move(detail)

                if headed and not cleared:
                    log.warning(
                        "详情页可能需要手动过一次安全验证，请在弹出的窗口完成"
                        "（最多等 90 秒），过一次后本批剩余会自动抓。"
                    )
                    timeout = 90.0
                else:
                    timeout = 12.0

                desc = await self._wait_for_jd(detail, timeout=timeout)
                if desc:
                    await self.human_scroll(detail, steps=random.randint(1, 3))
                    job.description = desc
                    fetched += 1
                    cleared = True
                    consecutive_blocked = 0
                else:
                    blocked += 1
                    consecutive_blocked += 1
                    if await self._looks_blocked(detail):
                        log.warning("详情页卡在反爬/验证挑战，跳过：%s", job.url)
                    else:
                        log.warning("详情页未匹配到 JD：%s", job.url)
                    if not dumped:
                        await self.dump_debug(detail, tag="detail")
                        dumped = True
            except Exception as exc:  # noqa: BLE001
                log.warning("抓取详情失败 %s: %s", job.url, exc)
            finally:
                # 不要秒关：像真人读完页面再离开，停留几秒
                try:
                    await asyncio.sleep(random.uniform(3.0, 7.0))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await detail.close()
                except Exception:
                    pass

            # 熔断：连续被拦就停手，避免越刷越被限流
            if consecutive_blocked >= _settings.scrape_detail_block_giveup:
                log.warning(
                    "连续 %d 条被反爬拦截，本次停止详情补全（避免触发限流，建议稍后再试）。",
                    consecutive_blocked,
                )
                break

        log.info(
            "详情页补全完成：%d/%d 条获得完整 JD（%d 条被拦截）。",
            fetched,
            len(targets),
            blocked,
        )
