"""BOSS 直聘 (zhipin.com) 职位抓取插件。

BOSS 直聘也是左右布局：左侧职位列表，点开后右侧（或新标签）显示完整 JD。
结构对齐领英：带日志、等 SPA 渲染、未命中/空白时转储页面、点开读详情。
注意：BOSS 反爬较强（可能出现滑块验证），选择器按公开经验给多套候选，
命中失败会转储 data/debug，按真实 DOM 再校正。
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

# BOSS 用城市 code，不识别城市名。常见城市映射；未命中则不带城市（全国）。
_CITY_CODE = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "西安": "101110100",
    "苏州": "101190400",
    "天津": "101030100",
    "青岛": "101120200",
    "厦门": "101230200",
    "长沙": "101250100",
    "重庆": "101040100",
    "郑州": "101180100",
    "合肥": "101220100",
}

_HOME_URL = "https://www.zhipin.com/"
_SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={kw}"

# 首页搜索框 / 搜索按钮候选选择器（BOSS 改版时按真实 DOM 调整）
_SEARCH_INPUT_SELECTORS = [
    ".search-form input.ipt-search",
    "input.ipt-search",
    ".search-form-con input[name='query']",
    "input[name='query']",
    ".search-input input",
    "form.search-form input[type='text']",
    "input[placeholder*='搜索']",
    "input[placeholder*='职位']",
]
_SEARCH_BTN_SELECTORS = [
    ".search-form button.btn-search",
    "button.btn-search",
    ".search-form-con .btn-search",
    ".search-form button",
    "button[type='submit']",
]

_CARD_SELECTORS = [
    "li.job-card-box",
    "ul.job-list-box li",
    "li.job-card-wrapper",
    "div.job-card-wrap",
    "[class*='job-card']",
]
_TITLE_SELECTORS = [
    ".job-name",
    ".job-title",
    "[class*='job-name']",
    "[class*='job-title']",
]
_COMPANY_SELECTORS = [
    ".company-name",
    "[class*='company-name']",
    ".boss-name",
]
_SALARY_SELECTORS = [".job-salary", ".salary", "[class*='salary']"]
_LOC_SELECTORS = [
    ".company-location",
    ".job-area",
    "[class*='job-area']",
    "[class*='location']",
]
_TAG_SELECTORS = [
    ".tag-list li",
    ".job-tags span",
    "[class*='tag-list'] li",
    "[class*='job-tag']",
]

# 右侧详情面板 / 详情页里的完整 JD 容器
_DETAIL_SELECTORS = [
    ".job-detail-box .job-sec-text",
    ".job-detail .job-sec-text",
    ".job-sec-text",
    "[class*='job-detail'] [class*='text']",
    ".desc",
    ".text-desc",
]
# 滑块/安全验证特征
_VERIFY_SELECTORS = [
    ".geetest_panel",
    "[class*='geetest']",
    "[class*='verify-slider']",
    "#nc_1_wrapper",
]

# 一次性把所有卡片信息抓回来，避免多次往返被页面刷新/跳转打断（更快也更稳）。
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
  const pickAll = (root, sels) => {
    for (const s of sels) {
      try {
        const els = Array.from(root.querySelectorAll(s));
        if (els.length) {
          const arr = els.map(e => (e.innerText || '').trim()).filter(Boolean);
          if (arr.length) return arr;
        }
      } catch (e) {}
    }
    return [];
  };
  let cards = [];
  for (const cs of S.card) {
    try { const f = Array.from(document.querySelectorAll(cs)); if (f.length) { cards = f; break; } } catch (e) {}
  }
  return cards.map(c => {
    let href = '';
    try { const a = c.querySelector("a[href*='job_detail'], a.job-card-left, a"); if (a) href = a.getAttribute('href') || ''; } catch (e) {}
    return {
      title: pick(c, S.title),
      company: pick(c, S.company),
      salary: pick(c, S.salary),
      location: pick(c, S.loc),
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


async def _texts(node, selectors: list[str]) -> list[str]:
    out: list[str] = []
    for sel in selectors:
        for el in await node.query_selector_all(sel):
            t = (await el.inner_text()).strip()
            if t and t not in out:
                out.append(t)
        if out:
            break
    return out


@register
class BossScraper(BaseScraper):
    site = Site.boss
    # 首页能正常渲染（会跳安全验证页）；而 /web/user 登录页对自动化直接返回空白。
    login_url = "https://www.zhipin.com/"
    # BOSS 无头必白板，必须有界面 + stealth 才会渲染。
    force_headed = True
    # 过完安全验证会跳回首页（非登录 URL），不能按 URL 判定登录成功而过早关窗，
    # 让用户自己点验证、扫码登录后手动关窗。
    login_manual_close = True

    async def search(
        self,
        keyword: str,
        city: str = "",
        salary: str = "",
        limit: int = 20,
        date_range: str = "any",
        fetch_detail: "bool | None" = None,
    ) -> List[RawJob]:
        """CDP 模式：全自动。程序自己在真实 Chrome 里走"首页→搜索框输入→点搜索"
        的有机导航流程拿到结果页，再读它的 DOM。

        BOSS 拦的是"直接 goto 搜索深链"（缺少搜索流程里 JS 生成的 token，会白板）；
        而模拟真人在首页搜索框操作产生的导航是有机的，token 正常生成，不被拦。
        自动流程失败时，才兜底复用你已打开的搜索结果标签。
        非 CDP 模式仍走父类（新开页 + 导航）逻辑。
        fetch_detail=False 时只抓列表页（基础预热/快速预抓取）。
        """
        self._fetch_detail_override = fetch_detail
        if not cdp_enabled():
            return await super().search(keyword, city, salary, limit, date_range, fetch_detail=fetch_detail)

        async with open_context(self.site.value) as context:
            page, owned = await self._obtain_results_page(context, keyword, city)
            if page is None:
                raise ScrapeBlockedError(
                    f"无法获取 BOSS 搜索结果页（关键词『{keyword}』{('· ' + city) if city else ''}）。"
                    "可能是首页命中安全验证或未登录：请在调试 Chrome 里完成一次"
                    "『安全验证』并保持登录，然后重跑（之后即可全自动）。"
                )
            log.info("CDP 抓取搜索结果页（%s）：%s", "自动" if owned else "复用已开标签", page.url)
            try:
                results = await self._extract_from_page(
                    page, city, limit, click_detail=False
                )
                # 列表抓到后，逐条进详情页补全完整 JD（会话已热身，带 Referer 慢速抓）
                if results:
                    try:
                        await self._enrich_details(page, results)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("BOSS 详情补全异常：%s", str(exc)[:120])
            except Exception as exc:  # noqa: BLE001
                log.warning("boss CDP 读取异常（页面可能在刷新/验证）：%s", str(exc)[:120])
                results = []
            finally:
                # 只关我们自己开的标签；用户手动开的标签保留。关前停留几秒更像真人。
                if owned:
                    try:
                        await asyncio.sleep(random.uniform(3.0, 7.0))
                        await page.close()
                    except Exception:  # noqa: BLE001
                        pass
            return results[:limit]

    async def _obtain_results_page(self, context, keyword: str, city: str):
        """拿到一个 BOSS 搜索结果页。

        返回 (page, owned)：owned=True 表示这是程序自己新开的标签（用完要关）；
        owned=False 表示复用了用户已打开的标签（不要关）。
        """
        # 1) 首选：模拟真人在首页搜索（有机导航，绕过深链 token 拦截）
        page = await self._auto_search(context, keyword, city)
        if page is not None:
            return page, True
        # 2) 兜底：复用你已经打开并渲染好的搜索结果标签
        page = await self._find_job_tab(context)
        if page is not None:
            return page, False
        return None, False

    async def _auto_search(self, context, keyword: str, city: str):
        """模拟真人搜索流程：开首页 → 在搜索框逐字输入关键词 → 点搜索按钮。

        成功返回结果页 Page；失败（找不到搜索框/命中验证/没跳到结果页）返回 None。
        """
        page = await context.new_page()
        try:
            await page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            await self.human_engage(page)

            # 探测首页是否被吐成白板（body 基本为空）：是则转储取证后放弃自动搜索
            try:
                blen = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').length"
                )
            except Exception:  # noqa: BLE001
                blen = 0
            log.info("BOSS 首页加载完成：body 文本长度=%s，URL=%s", blen, page.url)
            if not blen or blen < 50:
                await self.dump_debug(page, tag="home_blank")
                log.warning("BOSS 首页被吐成白板（程序导航被反爬拦截），自动搜索放弃，转兜底。")
                await page.close()
                return None

            # 命中 EdgeOne/腾讯云『确认您是真人』复选框：自动勾选过验证
            if await self.looks_human_check(page):
                log.info("BOSS 首页命中『确认您是真人』，尝试自动勾选…")
                await self.try_pass_human_checkbox(page, timeout=25.0)

            # 首页若命中安全验证，给真实 Chrome（已登录会话）一点时间自动放行
            if await self._is_verification(page):
                log.warning("BOSS 首页命中安全验证，等待放行（最多 ~20 秒）...")
                for _ in range(20):
                    await asyncio.sleep(1.0)
                    if not await self._is_verification(page):
                        break
                else:
                    log.warning("BOSS 首页验证未放行，自动搜索放弃，转兜底。")
                    await page.close()
                    return None

            inp = await self._find_visible(page, _SEARCH_INPUT_SELECTORS, timeout_each=3000)
            if inp is None:
                log.warning("BOSS 首页未找到搜索框，自动搜索放弃，转兜底。")
                await page.close()
                return None

            # 逐字输入 + 真人鼠标轨迹
            await self.human_mouse_move(page, moves=2)
            try:
                await inp.click()
                await inp.fill("")
            except Exception:  # noqa: BLE001
                pass
            await inp.type(keyword, delay=random.uniform(80, 180))
            await asyncio.sleep(random.uniform(0.4, 1.0))

            # 点搜索按钮（优先），否则回车；等待导航到结果页
            await self._submit_search(page)
            try:
                await page.wait_for_url("**/web/geek/job**", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            await self.human_engage(page)

            if await self.looks_human_check(page):
                await self.try_pass_human_checkbox(page, timeout=25.0)

            if await self._is_verification(page):
                log.warning("BOSS 搜索后命中安全验证，自动搜索放弃，转兜底。")
                await self.dump_debug(page, tag="verify")
                await page.close()
                return None

            # 指定了城市且当前 URL 没带 city：此时会话已"热"，带 referer 跳到含城市 code 的结果页
            code = _CITY_CODE.get(city.strip()) if city else None
            if code and "city=" not in page.url:
                target = _SEARCH_URL.format(kw=quote(keyword)) + f"&city={code}"
                try:
                    await page.goto(
                        target,
                        referer=page.url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await self.human_engage(page)
                except Exception:  # noqa: BLE001
                    pass
            elif city and not code:
                log.info("BOSS 未收录城市『%s』的 code，本次按全国搜索。", city)

            if ("/web/geek/job" in page.url) or ("query=" in page.url):
                return page
            log.warning("BOSS 自动搜索未跳到结果页：URL=%s", page.url)
            await page.close()
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("BOSS 自动搜索异常：%s", str(exc)[:120])
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
            return None

    async def _find_visible(self, page: Page, selectors: list[str], timeout_each: int = 3000):
        """按候选选择器找第一个可见元素，找不到返回 None。"""
        for sel in selectors:
            try:
                await page.wait_for_selector(sel, timeout=timeout_each)
            except Exception:  # noqa: BLE001
                pass
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _submit_search(self, page: Page) -> None:
        """点击搜索按钮触发导航；没有可见按钮则回车提交。"""
        btn = await self._find_visible(page, _SEARCH_BTN_SELECTORS, timeout_each=1500)
        try:
            if btn is not None:
                await self.human_mouse_move(page, moves=1)
                async with page.expect_navigation(
                    wait_until="domcontentloaded", timeout=20000
                ):
                    await btn.click()
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            async with page.expect_navigation(
                wait_until="domcontentloaded", timeout=20000
            ):
                await page.keyboard.press("Enter")
        except Exception:  # noqa: BLE001
            pass

    async def _find_job_tab(self, context) -> Page | None:
        """在已接管的 Chrome 里找一个停在 BOSS 搜索结果页、且已渲染出内容的标签。"""
        best = None
        for p in context.pages:
            try:
                u = p.url
            except Exception:
                continue
            if "zhipin.com" not in u:
                continue
            if ("/web/geek/job" in u) or ("query=" in u):
                try:
                    blen = await p.evaluate(
                        "() => (document.body && document.body.innerText || '').length"
                    )
                except Exception:
                    blen = 0
                if blen and blen > 200:
                    return p
                best = best or p
        return best

    async def _search(
        self,
        page: Page,
        keyword: str,
        city: str,
        salary: str,
        limit: int,
        date_range: str = "any",
    ) -> List[RawJob]:
        url = _SEARCH_URL.format(kw=quote(keyword))
        code = _CITY_CODE.get(city.strip()) if city else None
        if code:
            url += f"&city={code}"
        elif city:
            log.info("BOSS 未收录城市『%s』的 code，本次按全国搜索。", city)

        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await self.human_pause()

        if await self._is_verification(page):
            await self.dump_debug(page, tag="verify")
            raise ScrapeBlockedError(f"BOSS 命中滑块/安全验证，需人工过验证后重跑。URL={page.url}")

        jobs = await self._extract_from_page(page, city, limit)
        if jobs:
            await self._enrich_details(page, jobs)
        return jobs

    async def _extract_from_page(
        self, page: Page, city: str, limit: int, click_detail: bool = True
    ) -> List[RawJob]:
        # 等 SPA 渲染稳定（页面在刷新时 evaluate 会偶发失败，多等几次）
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
            "title": _TITLE_SELECTORS,
            "company": _COMPANY_SELECTORS,
            "salary": _SALARY_SELECTORS,
            "loc": _LOC_SELECTORS,
            "tag": _TAG_SELECTORS,
        }

        # 原子化提取：单次 evaluate 取回全部卡片。页面刷新打断则重试。
        raw = None
        for attempt in range(4):
            try:
                raw = await page.evaluate(_EXTRACT_JS, sel_arg)
                if raw:
                    break
            except Exception as exc:  # noqa: BLE001
                log.info(
                    "boss 提取被页面跳转打断，重试 %d/4：%s", attempt + 1, str(exc)[:60]
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
            log.warning("boss 未读到职位卡片（页面可能在刷新/验证中）。URL=%s", url)
            return []

        jobs: List[RawJob] = []
        for item in raw[:limit]:
            title = (item.get("title") or "").strip()
            company = (item.get("company") or "").strip()
            if not (title or company):
                continue
            href = item.get("href") or ""
            if href.startswith("/"):
                href = "https://www.zhipin.com" + href
            tags = item.get("tags") or []
            jobs.append(
                RawJob(
                    title=title,
                    company=company,
                    salary=(item.get("salary") or "").strip(),
                    location=(item.get("location") or "").strip() or city,
                    tags=", ".join(dict.fromkeys(tags)),
                    description="",
                    url=href.split("?")[0],
                )
            )

        log.info("boss 读到 %d 条职位（列表级，共抓到 %d 个卡片）。", len(jobs), len(raw))
        return jobs

    async def _is_verification(self, page: Page) -> bool:
        try:
            title = (await page.title()).lower()
        except Exception:
            title = ""
        if any(k in title for k in ("验证", "verify", "安全验证")):
            return True
        for sel in _VERIFY_SELECTORS:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    # 按文本锚点提取 JD：找含"职位描述/岗位职责/任职要求"的最内层内容块，
    # 不依赖会变动的 class，详情页只要正常加载就能抓到。
    _JD_JS = r"""
    () => {
      const markers = ['职位描述','岗位职责','任职要求','岗位要求','工作职责','职位详情'];
      const nodes = Array.from(document.querySelectorAll('div,section,article,ul,dd,p'));
      let best = '';
      for (const el of nodes) {
        const txt = (el.innerText || '').trim();
        if (txt.length < 60 || txt.length > 6000) continue;
        if (!markers.some(m => txt.includes(m))) continue;
        const childMax = Math.max(0, ...Array.from(el.children).map(c => (c.innerText||'').trim().length));
        if (childMax >= txt.length - 5) continue;
        if (best === '' || txt.length < best.length) best = txt;
      }
      return best;
    }
    """

    async def _extract_detail(self, page: Page) -> str:
        # 先试固定选择器（快），再退回文本锚点（稳）
        for sel in _DETAIL_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if len(txt) >= 60:
                        return txt
            except Exception:  # noqa: BLE001
                continue
        try:
            txt = await page.evaluate(self._JD_JS)
            if txt and len(txt.strip()) >= 60:
                return txt.strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _extract_detail_jd(self, page: Page) -> str:
        """按需单条详情：等待并提取 JD。"""
        return await self._wait_for_detail(page, timeout=20.0)

    async def _wait_for_detail(self, page: Page, timeout: float = 20.0) -> str:
        """轮询等待详情页 JD 出现（SPA 渲染/挑战放行需要时间）。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jd = await self._extract_detail(page)
            if jd:
                return jd
            await asyncio.sleep(1.0)
        return await self._extract_detail(page)

    async def _detail_pause(self) -> None:
        """详情页之间的长间隔，尽量避免触发 BOSS 限流。"""
        lo = _settings.scrape_detail_min_delay
        hi = max(lo, _settings.scrape_detail_max_delay)
        await asyncio.sleep(random.uniform(lo, hi))

    async def _enrich_details(self, page: Page, jobs: List[RawJob]) -> None:
        """逐条进详情页补全完整 JD。

        会话已经过有机搜索"热身"，详情页带上列表页 Referer 直接 goto；
        慢速 + 单次限量 + 被拦指数退避熔断，尽量贴近真人、避免触发限流。
        """
        if not self._should_fetch_detail():
            return
        context = page.context
        referer = ""
        try:
            referer = page.url
        except Exception:  # noqa: BLE001
            pass

        targets = [j for j in jobs if j.url and not j.description][
            : max(0, _settings.scrape_detail_max)
        ]
        if not targets:
            return
        log.info(
            "BOSS 详情补全开始：本次最多 %d 条，每条间隔 %.0f-%.0f 秒，慢速进行。",
            len(targets),
            _settings.scrape_detail_min_delay,
            _settings.scrape_detail_max_delay,
        )

        fetched = 0
        consecutive_blocked = 0
        for idx, job in enumerate(targets):
            if idx > 0:
                await self._detail_pause()
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
                await self.human_mouse_move(detail)
                jd = await self._wait_for_detail(detail, timeout=20.0)
                if jd:
                    await self.human_scroll(detail, steps=random.randint(1, 3))
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                    job.description = jd
                    fetched += 1
                    consecutive_blocked = 0
                    log.info("BOSS 详情补全成功（%d/%d）：%s", fetched, len(targets), job.title)
                else:
                    consecutive_blocked += 1
                    if await self._is_verification(detail):
                        log.warning("BOSS 详情页命中验证，跳过：%s", job.url)
                    else:
                        log.warning("BOSS 详情页未匹配到 JD：%s", job.url)
            except Exception as exc:  # noqa: BLE001
                log.warning("BOSS 抓取详情失败 %s: %s", job.url, exc)
            finally:
                # 不要秒关：像真人读完再离开，停留几秒
                try:
                    await asyncio.sleep(random.uniform(3.0, 7.0))
                    await detail.close()
                except Exception:  # noqa: BLE001
                    pass

            if consecutive_blocked >= _settings.scrape_detail_block_giveup:
                log.warning(
                    "BOSS 连续 %d 条被拦，停止本次详情补全（避免触发限流）。",
                    consecutive_blocked,
                )
                break

        log.info("BOSS 详情补全完成：%d/%d 条获得完整 JD。", fetched, len(targets))
