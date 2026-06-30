"""前程无忧（51job）抓取插件。

CDP 模式：接管真实 Chrome，导航到 we.51job.com 搜索页。

51job 新版（we.51job.com）是 SPA，列表数据由后台接口
`we.51job.com/api/job/search-pc` 返回。本插件**直接拦截该接口的 JSON**，里面
每条职位都带：jobId、jobHref（独立详情 URL）、provideSalaryString（薪资）、
workYearString（经验）、degreeString（学历）、jobTags（技能/福利标签），以及
**jobDescribe（完整职位描述 JD）**。因此能一次拿全信息 + 完整 JD，且无需进详情页、
绕开了详情页的腾讯云 WAF 人机验证。

若接口未捕获到（站点改版/限流），回退到 DOM 列表级（无完整 JD）并打日志提示。
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import List
from urllib.parse import quote

from playwright.async_api import Page

from ..browser import cdp_enabled, open_context
from ..config import get_settings
from ..models import Site
from .base import BaseScraper, RawJob, register

_settings = get_settings()
log = logging.getLogger("jobhunter.scrapers")

_SEARCH_URL = "https://we.51job.com/pc/search?keyword={kw}"
_SEARCH_URL_CITY = "https://we.51job.com/pc/search?jobArea={area}&keyword={kw}"

# 常见城市 -> 51job jobArea 代码（找不到则不带地区，按默认结果）
_CITY_AREA = {
    "北京": "010000",
    "上海": "020000",
    "广州": "030200",
    "深圳": "040000",
    "天津": "050000",
    "重庆": "060000",
    "南京": "070200",
    "苏州": "070300",
    "杭州": "080200",
    "成都": "090200",
    "合肥": "150200",
    "武汉": "180200",
    "长沙": "190200",
    "西安": "200200",
}

# 列表提取：卡片 .joblist-item，子元素 class 稳定。
_EXTRACT_JS = r"""
() => {
  const txt = (el, s) => { const e = el.querySelector(s); return e ? (e.innerText || '').trim() : ''; };
  const expRe = /(\d+\s*-?\s*\d*\s*年|经验不限|无需经验|应届|在校|实习|\d+年以[上下])/;
  const eduRe = /(本科|大专|硕士|博士|学历不限|高中|中专|初中|MBA|EMBA|中技)/;
  const cards = document.querySelectorAll('.joblist-item');
  const jobs = [];
  cards.forEach(c => {
    const title = txt(c, '.jname');
    if (!title) return;
    const salary = txt(c, '.sal');
    const location = txt(c, '.area');
    const company = txt(c, '.cname');
    // 经验/学历散落在 .joblist-item-jobinfo / .status / .shrink-0 等短文本里，
    // 直接扫卡片内所有"短文本"节点分类，避免依赖具体 class。
    let exp = '', edu = '';
    c.querySelectorAll('span, div, em, i').forEach(e => {
      const t = (e.textContent || '').trim();
      if (!t || t.length > 8) return;
      if (!exp && expRe.test(t)) exp = t;
      if (!edu && eduRe.test(t)) edu = t;
    });
    const tags = Array.from(c.querySelectorAll('.tag'))
      .map(e => (e.innerText || '').trim())
      .filter(t => t && t !== '标签');
    // 尽量抓到独立职位链接：优先指向 jobs.51job.com 详情的 <a>，否则取卡片内任意带 href 的 <a>
    const a = c.querySelector('a[href*="jobs.51job.com"]') || c.querySelector('a[href]');
    const url = a ? a.href : '';
    jobs.push({ title, company, salary, location, experience: exp, education: edu, tags, url });
  });
  return jobs;
}
"""


@register
class Job51Scraper(BaseScraper):
    site = Site.job51
    login_url = "https://login.51job.com/login.php"

    def _build_url(self, keyword: str, city: str) -> str:
        area = _CITY_AREA.get(city.strip())
        if area:
            return _SEARCH_URL_CITY.format(area=area, kw=quote(keyword))
        return _SEARCH_URL.format(kw=quote(keyword))

    async def search(
        self,
        keyword: str,
        city: str = "",
        salary: str = "",
        limit: int = 20,
        date_range: str = "any",
        fetch_detail: "bool | None" = None,
    ) -> List[RawJob]:
        self._fetch_detail_override = fetch_detail
        if not cdp_enabled():
            return await super().search(keyword, city, salary, limit, date_range, fetch_detail=fetch_detail)

        url = self._build_url(keyword, city)
        async with open_context(self.site.value) as context:
            page = await context.new_page()
            log.info("CDP 驱动调试 Chrome 搜索 51job：%s", url)

            captured: dict = {}

            async def on_resp(resp):
                if "api/job/search-pc" in resp.url and "data" not in captured:
                    try:
                        captured["data"] = await resp.json()
                    except Exception:  # noqa: BLE001
                        pass

            page.on("response", on_resp)
            try:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:  # noqa: BLE001
                    log.warning("51job 打开搜索页异常：%s", str(exc)[:120])
                    return []

                # 等搜索接口返回（最多 ~12 秒）
                for _ in range(24):
                    if captured.get("data"):
                        break
                    await asyncio.sleep(0.5)

                data = captured.get("data")
                if data:
                    jobs = self._parse_api(data, city, limit)
                    if jobs:
                        with_jd = sum(1 for j in jobs if j.description)
                        log.info(
                            "51job 经接口拿到 %d 条（其中 %d 条含完整 JD）。",
                            len(jobs),
                            with_jd,
                        )
                        return jobs[:limit]

                # 回退：DOM 列表级（拿不到完整 JD）
                log.warning(
                    "51job 未捕获到搜索接口（可能被限流/改版），回退列表级抓取，"
                    "本次无法补全完整 JD。"
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2.0)
                return (await self._extract_list(page, city, limit))[:limit]
            except Exception as exc:  # noqa: BLE001
                log.warning("51job CDP 读取异常：%s", str(exc)[:120])
                return []
            finally:
                try:
                    page.remove_listener("response", on_resp)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _clean_jd(raw: str) -> str:
        if not raw:
            return ""
        t = re.sub(r"<\s*br\s*/?\s*>", "\n", raw, flags=re.I)
        t = re.sub(r"</\s*(p|div|li)\s*>", "\n", t, flags=re.I)
        t = re.sub(r"<[^>]+>", "", t)
        t = html.unescape(t)
        # 压缩多余空行
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    def _parse_api(self, data: dict, city: str, limit: int) -> List[RawJob]:
        try:
            items = data["resultbody"]["job"]["items"]
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(items, list):
            return []

        jobs: List[RawJob] = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            title = (it.get("jobName") or "").strip()
            company = (it.get("fullCompanyName") or it.get("companyName") or "").strip()
            if not (title or company):
                continue
            href = (it.get("jobHref") or "").split("?")[0]
            desc = self._clean_jd(it.get("jobDescribe") or "")
            # jobTags 混了经验/学历/公司性质/技能/福利，去掉与独立字段重复的，保留技能/福利
            skip = {
                it.get("workYearString"),
                it.get("degreeString"),
                it.get("companyTypeString"),
                it.get("companySizeString"),
            }
            tags = [
                t for t in (it.get("jobTags") or []) if t and t not in skip
            ]
            jobs.append(
                RawJob(
                    title=title,
                    company=company,
                    salary=(it.get("provideSalaryString") or "").strip(),
                    location=(it.get("jobAreaString") or "").strip() or city,
                    experience=(it.get("workYearString") or "").strip(),
                    education=(it.get("degreeString") or "").strip(),
                    tags=", ".join(dict.fromkeys(tags)),
                    description=desc,
                    url=href,
                )
            )
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
        url = self._build_url(keyword, city)
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await self.human_pause()
        await self.human_scroll(page, steps=3)
        return await self._extract_list(page, city, limit)

    async def _extract_list(self, page: Page, city: str, limit: int) -> List[RawJob]:
        for _ in range(15):
            try:
                blen = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').length"
                )
            except Exception:
                blen = 0
            if blen and blen > 300:
                break
            await asyncio.sleep(1.0)

        raw = None
        for attempt in range(4):
            try:
                raw = await page.evaluate(_EXTRACT_JS)
                if raw:
                    break
            except Exception as exc:  # noqa: BLE001
                log.info("51job 提取被页面跳转打断，重试 %d/4：%s", attempt + 1, str(exc)[:60])
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
            log.warning("51job 未读到职位卡片（可能触发验证）。URL=%s", url)
            return []

        jobs: List[RawJob] = []
        for item in raw[:limit]:
            title = (item.get("title") or "").strip()
            company = (item.get("company") or "").strip()
            if not (title or company):
                continue
            tags = item.get("tags") or []
            # 抓到卡片里的直达链接就用；抓不到则留空（前端会退化为原站搜索链接，并把空链职位排到靠后）
            href = (item.get("url") or "").split("?")[0]
            jobs.append(
                RawJob(
                    title=title,
                    company=company,
                    salary=(item.get("salary") or "").strip(),
                    location=(item.get("location") or "").strip() or city,
                    experience=(item.get("experience") or "").strip(),
                    education=(item.get("education") or "").strip(),
                    tags=", ".join(dict.fromkeys(tags)),
                    description="",
                    url=href,
                )
            )

        log.info("51job 读到 %d 条职位（列表级，共抓到 %d 个卡片）。", len(jobs), len(raw))
        return jobs
