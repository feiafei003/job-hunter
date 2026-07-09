"""领英 (LinkedIn) 职位抓取插件。

LinkedIn 反爬严格，必须先用持久化会话登录。使用 /jobs/search 页面解析左侧
职位列表卡片。结构对齐智联：带日志、未命中时转储页面、尽量多抽字段。
DOM 改版时调整下方选择器即可（命中失败会转储 data/debug 供排查）。
"""

from __future__ import annotations

import logging
import re
from typing import List
from urllib.parse import quote

from playwright.async_api import Page

from ..models import Site
from .base import BaseScraper, RawJob, register

log = logging.getLogger("jobhunter.scrapers")

_SEARCH_URL = "https://www.linkedin.com/jobs/search/?keywords={kw}"
_SEARCH_URL_LOC = "https://www.linkedin.com/jobs/search/?keywords={kw}&location={loc}"

# 领英发布时间过滤参数 f_TPR=r<秒数>
_TPR = {
    "day": "r86400",
    "week": "r604800",
    "month": "r2592000",
}

_CARD_SELECTORS = [
    "div.job-card-container",
    "li.jobs-search-results__list-item",
    "div[data-job-id]",
    "ul.scaffold-layout__list-container li",
    "li.scaffold-layout__list-item",
]

_TITLE_SELECTORS = [
    "a.job-card-container__link",
    "a.job-card-list__title",
    "a.job-card-list__title--link",
    "[class*='job-card-list__title']",
    "a[href*='/jobs/view/'] strong",
    "strong",
]
_COMPANY_SELECTORS = [
    ".artdeco-entity-lockup__subtitle",
    ".job-card-container__primary-description",
    "[class*='company-name']",
    ".job-card-container__company-name",
]
_LOC_SELECTORS = [
    "ul.job-card-container__metadata-wrapper li",
    ".job-card-container__metadata-item",
    ".artdeco-entity-lockup__caption li",
    "[class*='metadata']",
]
# 卡片底部洞察/状态（Easy Apply、Promoted、福利等）当作标签
_FOOTER_SELECTORS = [
    "ul.job-card-list__footer-wrapper li",
    ".job-card-container__footer-item",
    "[class*='footer-job-state']",
]

# 右侧详情面板里的完整 JD 容器（点开卡片后出现）
_DETAIL_SELECTORS = [
    "div#job-details",
    ".jobs-description__content .jobs-box__html-content",
    ".jobs-description-content__text",
    "article.jobs-description__container",
    ".jobs-description__content",
    ".jobs-box__html-content",
]
# “显示更多”按钮，展开被折叠的 JD
_SHOWMORE_SELECTORS = [
    "button.jobs-description__footer-button",
    "button[aria-label*='more']",
    ".show-more-less-html__button--more",
]

# 薪资特征（领英常不显示，命中则填）
_SALARY_RE = re.compile(r"(\$|¥|￥|€|£|/yr|/hr|/year|/hour|K\b|万|薪)", re.I)
# 工作模式
_WORKPLACE_RE = re.compile(r"(Remote|Hybrid|On-?site|远程|混合|现场)", re.I)


async def _text(card, selectors: list[str]) -> str:
    for sel in selectors:
        el = await card.query_selector(sel)
        if el:
            txt = (await el.inner_text()).strip()
            if txt:
                return txt
    return ""


async def _texts(card, selectors: list[str]) -> list[str]:
    out: list[str] = []
    for sel in selectors:
        for el in await card.query_selector_all(sel):
            t = (await el.inner_text()).strip()
            if t and t not in out:
                out.append(t)
        if out:
            break
    return out


@register
class LinkedInScraper(BaseScraper):
    site = Site.linkedin
    login_url = "https://www.linkedin.com/login"

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
            url = _SEARCH_URL_LOC.format(kw=quote(keyword), loc=quote(city))
        else:
            url = _SEARCH_URL.format(kw=quote(keyword))
        if date_range in _TPR:
            url += f"&f_TPR={_TPR[date_range]}"

        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await self.human_pause()

        low = page.url.lower()
        if any(k in low for k in ("/login", "/authwall", "/checkpoint", "/signup", "cold-join")):
            raise RuntimeError(
                "LinkedIn 未真正登录（停在登录/注册页），请先在网页里点『登录领英』完成登录"
            )

        # 等 SPA 渲染：轮询 body 文本量，避免在空壳上找卡片
        import asyncio

        for _ in range(12):
            try:
                blen = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').length"
                )
            except Exception:
                blen = 0
            if blen and blen > 200:
                break
            await asyncio.sleep(1.0)

        await self.human_scroll(page, steps=6)

        # 页面基本空白：多半未登录或该版本不可用，转储后退出
        try:
            blen = await page.evaluate(
                "() => (document.body && document.body.innerText || '').length"
            )
        except Exception:
            blen = 0
        if not blen or blen < 200:
            await self.dump_debug(page, tag="blank")
            log.warning(
                "linkedin 页面内容空白(len=%s)，可能未登录或该版本不可用。URL=%s",
                blen,
                page.url,
            )
            return []

        cards = []
        matched_sel = _CARD_SELECTORS[0]
        for sel in _CARD_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=4000)
            except Exception:
                pass
            cards = await page.query_selector_all(sel)
            if cards:
                matched_sel = sel
                log.info("linkedin 命中选择器 '%s'，卡片数=%d", sel, len(cards))
                break

        if not cards:
            await self.dump_debug(page, tag="nocards")
            log.warning("linkedin 未匹配到职位卡片，已转储页面。URL=%s", page.url)
            return []

        n = min(len(cards), limit)
        jobs: List[RawJob] = []
        enriched = 0
        for i in range(n):
            # 每次重新取卡片，避免点开右侧详情后元素句柄失效
            cur = await page.query_selector_all(matched_sel)
            if i >= len(cur):
                break
            card = cur[i]
            try:
                title = await _text(card, _TITLE_SELECTORS)
                if not title:
                    link = await card.query_selector("a[href*='/jobs/view/']")
                    if link:
                        title = (await link.get_attribute("aria-label") or "").strip()

                company = await _text(card, _COMPANY_SELECTORS)
                meta = await _texts(card, _LOC_SELECTORS)
                footer = await _texts(card, _FOOTER_SELECTORS)

                location = ""
                salary_txt = ""
                for m in meta:
                    if _SALARY_RE.search(m) and not salary_txt:
                        salary_txt = m
                    elif not location:
                        location = m
                if not location:
                    location = city

                tags: list[str] = []
                for m in meta + footer:
                    wp = _WORKPLACE_RE.search(m)
                    if wp:
                        tags.append(wp.group(0))
                for f in footer:
                    if len(f) <= 24 and f not in tags:
                        tags.append(f)

                link_el = await card.query_selector("a[href*='/jobs/view/']")
                href = await link_el.get_attribute("href") if link_el else ""
                if href and href.startswith("/"):
                    href = "https://www.linkedin.com" + href

                if not (title or company):
                    continue

                # 点开卡片，从右侧详情面板读取完整 JD（无需独立页面，无验证码）
                description = ""
                try:
                    click_target = link_el or card
                    # 点击前先移动鼠标，制造真实轨迹（避免"瞬间精准点击"的爬虫特征）
                    await self.human_mouse_move(page, moves=2)
                    await click_target.click(timeout=5000)
                    description = await self._extract_detail(page)
                    if description:
                        enriched += 1
                except Exception:
                    pass

                jobs.append(
                    RawJob(
                        title=title,
                        company=company,
                        salary=salary_txt,
                        location=location,
                        tags=", ".join(dict.fromkeys(tags)),
                        description=description,
                        url=(href or "").split("?")[0],
                    )
                )
                await self.human_pause()
            except Exception:
                continue

        log.info("linkedin 解析到 %d 条职位（%d 条含完整 JD）。", len(jobs), enriched)
        return jobs

    async def _extract_detail(self, page: Page) -> str:
        """点开卡片后，从右侧详情面板提取完整 JD。"""
        import asyncio

        # 等右侧面板加载
        for sel in _DETAIL_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=6000)
                break
            except Exception:
                continue
        await asyncio.sleep(1.0)

        # 展开"显示更多"
        for sel in _SHOWMORE_SELECTORS:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click(timeout=1000)
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                continue

        for sel in _DETAIL_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if el:
                    txt = (await el.inner_text()).strip()
                    if len(txt) >= 60:
                        return txt
            except Exception:
                continue
        return ""
