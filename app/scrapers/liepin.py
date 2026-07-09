"""猎聘抓取插件。

CDP 模式：接管你登录好（或匿名可看）的真实 Chrome，程序自己导航到搜索页，
读取职位卡片基本信息（标题/薪资/地点/经验/学历/公司），再（可选）逐条进
详情页补全完整 JD。

猎聘列表卡片的 class 名是动态哈希（如 `_40108XXXX`，每次构建都会变），
不能用 class 选择器。这里改用"语义锚点 + 文本分词 + 正则"提取：每张卡片只有
一个 `a[href*='/job/']` 职位链接，标题/薪资/地点等都在卡片文本里，按顺序解析。
详情页不登录也可浏览，用文本锚点（职位描述/岗位职责/任职要求）定位 JD。
"""

from __future__ import annotations

import asyncio
import logging
from typing import List
from urllib.parse import quote

from playwright.async_api import Page

from ..browser import cdp_enabled, open_context
from ..config import get_settings
from ..models import Site
from .base import BaseScraper, RawJob, register

_settings = get_settings()
log = logging.getLogger("jobhunter.scrapers")

_SEARCH_URL = "https://www.liepin.com/zhaopin/?key={kw}"

# 列表提取：卡片容器是 div.job-list-box 的直接子元素（class 名稳定），
# 每张卡片只有一个职位链接，文本按"标题→地点→薪资→经验→学历→公司"顺序排列。
_EXTRACT_JS = r"""
() => {
  const salaryRe = /(\d+(\.\d+)?\s*-\s*\d+(\.\d+)?\s*[kK万千])|面议|\d+(\.\d+)?\s*[kK万]\s*以[上下]|\d+\s*元/;
  const expRe = /(\d+\s*-\s*\d+\s*年|\d+\s*年以[上下内]|\d+\s*年|经验不限|经验?\s*应届|应届|在校生?|实习)/;
  const eduRe = /(本科|大专|硕士|博士|学历不限|高中|中专|初中|MBA|EMBA|统招)/;
  const compDescRe = /(上市|未上市|融资|不需要融资|天使轮|[A-D]\s*轮|战略投资|\d+\s*-\s*\d+\s*人|\d+\s*人以[上下]|少于\s*\d+\s*人|人$)/;
  const status = ['招聘','急聘','猎头','置顶','急','直招','热招','名企'];

  let containers = document.querySelectorAll('[class*=job-list-box] > *');
  if (!containers.length) {
    // 兜底：直接用职位链接的近邻容器
    const seen = new Set();
    containers = Array.from(document.querySelectorAll('a[href*="/job/"]')).map(a => {
      let box = a;
      for (let i = 0; i < 4 && box.parentElement; i++) box = box.parentElement;
      return box;
    }).filter(b => { if (seen.has(b)) return false; seen.add(b); return true; });
  }

  const seenUrl = new Set();
  const jobs = [];
  Array.from(containers).forEach(c => {
    const a = c.querySelector('a[href*="/job/"]');
    if (!a) return;
    let href = a.getAttribute('href') || '';
    if (href.startsWith('//')) href = 'https:' + href;
    else if (href.startsWith('/')) href = 'https://www.liepin.com' + href;
    const idMatch = href.match(/\/job\/(\d+)/);
    const id = idMatch ? idMatch[1] : href;
    if (seenUrl.has(id)) return;
    seenUrl.add(id);

    const toks = (c.innerText || '')
      .split('\n').map(s => s.trim())
      // 丢掉纯分隔符 token（猎聘用的圆点不是 U+00B7），只保留含中英数字的
      .filter(s => s && /[\u4e00-\u9fa5A-Za-z0-9]/.test(s));
    if (!toks.length) return;

    const title = toks[0];
    let salary = '', si = -1;
    for (let i = 1; i < toks.length; i++) {
      if (salaryRe.test(toks[i])) { salary = toks[i]; si = i; break; }
    }
    let location = '';
    const end = si < 0 ? toks.length : si;
    for (let i = 1; i < end; i++) {
      if (!status.includes(toks[i])) { location = toks[i]; break; }
    }
    let exp = '', edu = '', company = '';
    const after = si < 0 ? 1 : si + 1;
    for (let i = after; i < toks.length; i++) {
      const t = toks[i];
      if (!exp && expRe.test(t)) { exp = t; continue; }
      if (!edu && eduRe.test(t)) { edu = t; continue; }
      if (status.includes(t) || salaryRe.test(t) || compDescRe.test(t)) continue;
      // 第一个不属于薪资/经验/学历/公司规模/状态词的，认作公司名
      company = t; break;
    }
    jobs.push({ title, url: href, salary, location, experience: exp, education: edu, company });
  });
  return jobs;
}
"""

# 详情页 JD 文本锚点提取（不依赖会变的 class）。
_JD_JS = r"""
() => {
  const markers = ['岗位职责','任职要求','职位描述','岗位要求','工作职责','职责描述','岗位描述','职位要求'];
  const nodes = Array.from(document.querySelectorAll('div,section,article,dd,ul'));
  let best = '';
  for (const el of nodes) {
    const txt = (el.innerText || '').trim();
    if (txt.length < 60 || txt.length > 8000) continue;
    if (!markers.some(m => txt.includes(m))) continue;
    const childMax = Math.max(0, ...Array.from(el.children).map(c => (c.innerText || '').trim().length));
    if (childMax >= txt.length - 5) continue;
    if (best === '' || txt.length < best.length) best = txt;
  }
  return best;
}
"""


@register
class LiepinScraper(BaseScraper):
    site = Site.liepin
    login_url = "https://www.liepin.com/"

    async def search(
        self,
        keyword: str,
        city: str = "",
        salary: str = "",
        limit: int = 20,
        date_range: str = "any",
        fetch_detail: "bool | None" = None,
    ) -> List[RawJob]:
        """CDP 模式：接管真实 Chrome，自己导航搜索页读列表，再可选补全 JD。
        非 CDP 模式走父类（open_page + _search）。
        fetch_detail=False 时只抓列表页（基础预热/快速预抓取）。"""
        self._fetch_detail_override = fetch_detail
        if not cdp_enabled():
            return await super().search(keyword, city, salary, limit, date_range, fetch_detail=fetch_detail)

        url = _SEARCH_URL.format(kw=quote(keyword))
        async with open_context(self.site.value) as context:
            page = await context.new_page()
            log.info("CDP 驱动调试 Chrome 搜索猎聘：%s", url)
            jobs: List[RawJob] = []
            try:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    await asyncio.sleep(2.5)
                    jobs = (await self._extract_list(page, city, limit))[:limit]
                except Exception as exc:  # noqa: BLE001
                    log.warning("liepin CDP 读取异常：%s", str(exc)[:120])
                    return []

                if self._should_fetch_detail() and jobs:
                    try:
                        await self._enrich_cdp(context, jobs)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("liepin CDP 详情补全异常：%s", str(exc)[:120])
                return jobs
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _search(
        self,
        page: Page,
        keyword: str,
        city: str,
        salary: str,
        limit: int,
        date_range: str = "any",
    ) -> List[RawJob]:
        """非 CDP 回退：直接导航+提取列表（不补全详情，避免被反爬限流）。"""
        url = _SEARCH_URL.format(kw=quote(keyword))
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await self.human_pause()
        await self.human_scroll(page, steps=3)
        return await self._extract_list(page, city, limit)

    async def _extract_list(self, page: Page, city: str, limit: int) -> List[RawJob]:
        # 等渲染稳定
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
                log.info("liepin 提取被页面跳转打断，重试 %d/4：%s", attempt + 1, str(exc)[:60])
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
            log.warning("liepin 未读到职位卡片（页面可能在刷新/验证中）。URL=%s", url)
            return []

        jobs: List[RawJob] = []
        for item in raw[:limit]:
            title = (item.get("title") or "").strip()
            company = (item.get("company") or "").strip()
            if not (title or company):
                continue
            jobs.append(
                RawJob(
                    title=title,
                    company=company,
                    salary=(item.get("salary") or "").strip(),
                    location=(item.get("location") or "").strip() or city,
                    experience=(item.get("experience") or "").strip(),
                    education=(item.get("education") or "").strip(),
                    tags="",
                    description="",
                    url=item.get("url") or "",
                )
            )

        log.info("liepin 读到 %d 条职位（列表级，共抓到 %d 个卡片）。", len(jobs), len(raw))
        return jobs

    async def _enrich_cdp(self, context, jobs: List[RawJob]) -> None:
        """慢速逐条进详情页补全完整 JD，单次限量 + 连续被拦熔断。"""
        targets = [j for j in jobs if j.url and not j.description][
            : max(0, _settings.scrape_detail_max)
        ]
        if not targets:
            return
        log.info(
            "liepin 详情补全开始：本次最多 %d 条，每条间隔 %.0f-%.0f 秒。",
            len(targets),
            _settings.scrape_detail_min_delay,
            _settings.scrape_detail_max_delay,
        )

        fetched = 0
        consecutive_blocked = 0
        for idx, job in enumerate(targets):
            if idx > 0:
                await self._detail_pause()

            detail = await context.new_page()
            try:
                await detail.goto(job.url, wait_until="domcontentloaded", timeout=45000)
                desc = await self._wait_for_jd(detail, timeout=20.0)
                if desc:
                    job.description = desc
                    fetched += 1
                    consecutive_blocked = 0
                    log.info("详情补全成功（%d/%d）：%s", fetched, len(targets), job.title)
                else:
                    consecutive_blocked += 1
                    log.warning("详情页未匹配到 JD：%s", job.url)
            except Exception as exc:  # noqa: BLE001
                log.warning("抓取详情失败 %s: %s", job.url, exc)
            finally:
                try:
                    await detail.close()
                except Exception:  # noqa: BLE001
                    pass

            if consecutive_blocked >= _settings.scrape_detail_block_giveup:
                log.warning("连续 %d 条被拦，停止本次详情补全。", consecutive_blocked)
                break

        log.info("liepin 详情补全完成：%d/%d 条获得完整 JD。", fetched, len(targets))

    async def _extract_jd(self, page: Page) -> str:
        try:
            txt = await page.evaluate(_JD_JS)
            if txt and len(txt.strip()) >= 60:
                return txt.strip()
        except Exception:
            pass
        return ""

    async def _extract_detail_jd(self, page: Page) -> str:
        """按需单条详情：等待并提取 JD。"""
        return await self._wait_for_jd(page, timeout=20.0)

    async def _wait_for_jd(self, page: Page, timeout: float) -> str:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jd = await self._extract_jd(page)
            if jd:
                return jd
            await asyncio.sleep(1.0)
        return await self._extract_jd(page)

    async def _detail_pause(self) -> None:
        import random

        lo = _settings.scrape_detail_min_delay
        hi = max(lo, _settings.scrape_detail_max_delay)
        await asyncio.sleep(random.uniform(lo, hi))
