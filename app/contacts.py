"""人脉同步：从已登录的领英「我的人脉」列表抓取联系人（姓名/公司/职位/主页）。

- 只在人脉列表页操作，不逐个点开主页（逐个访问几十上百次极易被风控）。
- 慢速拟人化滚动加载，抓满上限即停；去重入库。
- 领英 DOM 常改版：用 JS 提取 + 多重回退，命中失败转储调试页。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

from sqlmodel import select

from .browser import open_page
from .db import session_scope
from .models import Contact

log = logging.getLogger("jobhunter.contacts")

_CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

# 在页面里提取人脉卡片：优先已知卡片类，回退到任何含 /in/ 链接的列表项。
_EXTRACT_JS = """
() => {
  const out = [];
  const seen = new Set();
  let cards = Array.from(document.querySelectorAll('li.mn-connection-card'));
  if (!cards.length) {
    cards = Array.from(document.querySelectorAll('li, div'))
      .filter(el => el.querySelector && el.querySelector('a[href*="/in/"]'));
  }
  for (const c of cards) {
    const a = c.querySelector('a[href*="/in/"]');
    if (!a) continue;
    const url = (a.href || '').split('?')[0];
    if (!url || seen.has(url)) continue;
    const nameEl = c.querySelector('.mn-connection-card__name')
      || c.querySelector('span[dir="ltr"]')
      || a.querySelector('span');
    let name = nameEl ? nameEl.innerText.trim() : (a.innerText || '').trim();
    name = (name || '').split('\\n')[0].trim();
    const occEl = c.querySelector('.mn-connection-card__occupation')
      || c.querySelector('[class*="occupation"]')
      || c.querySelector('[class*="subline"]');
    const occ = occEl ? occEl.innerText.trim() : '';
    if (!name || name.length > 40) continue;
    seen.add(url);
    out.push({ name, occupation: occ, url });
  }
  return out;
}
"""

_AT_SEPS = (" at ", " AT ", " At ", " @ ", "@", "｜", "|", " · ", "·", " - ", " — ")


def _company_from_occupation(occ: str) -> str:
    """从"职位 at 公司 / 公司 | 职位"等文案里尽力解析出公司名。解析不出返回空。"""
    occ = (occ or "").strip()
    if not occ:
        return ""
    for sep in _AT_SEPS:
        if sep in occ:
            parts = [p.strip() for p in occ.split(sep) if p.strip()]
            if len(parts) >= 2:
                # "xx at 公司" / "xx @ 公司"：公司在后
                if sep.strip().lower() in ("at", "@"):
                    return parts[-1]
                # 其它分隔符含义不定，取最后一段作近似公司
                return parts[-1]
    return ""


def _dedup_key(name: str, company: str, url: str) -> str:
    return (url or "").strip() or f"{(name or '').strip()}|{(company or '').strip()}"


def _contact_dict(r: Contact) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "company": r.company,
        "title": r.title,
        "profile_url": r.profile_url,
        "source": r.source,
        "synced_at": r.synced_at,
    }


_COMPANY_STOP = (
    "有限责任公司", "有限公司", "股份公司", "股份", "集团", "科技", "网络", "信息",
    "技术", "公司", "(", "（", ")", "）", "inc.", "inc", "ltd.", "ltd", "llc",
    "co.,ltd", "co.ltd", "co.", "co", "corporation", "corp.", "corp",
)


def _norm_company(s: str) -> str:
    s = (s or "").strip().lower()
    for w in _COMPANY_STOP:
        s = s.replace(w, "")
    return re.sub(r"\s+", "", s)


def list_contacts(user_id: int, q: str = "", limit: int = 300) -> list[dict]:
    q = (q or "").strip()
    with session_scope() as session:
        stmt = select(Contact).where(Contact.user_id == user_id)
        if q:
            from sqlalchemy import or_

            stmt = stmt.where(
                or_(
                    Contact.name.contains(q),  # type: ignore[attr-defined]
                    Contact.company.contains(q),  # type: ignore[attr-defined]
                    Contact.title.contains(q),  # type: ignore[attr-defined]
                )
            )
        stmt = stmt.order_by(Contact.synced_at.desc()).limit(max(1, min(500, limit)))
        return [_contact_dict(r) for r in session.exec(stmt).all()]


def contacts_for_company(user_id: int, company: str, limit: int = 20) -> list[dict]:
    """找出当前公司与目标公司匹配的人脉（模糊：去后缀后互相包含）。"""
    company = (company or "").strip()
    nc = _norm_company(company)
    if len(nc) < 2:
        return []
    with session_scope() as session:
        rows = session.exec(select(Contact).where(Contact.user_id == user_id)).all()
    out = []
    for r in rows:
        rc = _norm_company(r.company)
        if rc and (nc in rc or rc in nc):
            out.append(_contact_dict(r))
    return out[:limit]


def import_contacts(user_id: int, items: list[dict]) -> dict:
    """CSV/手动导入兜底：item 含 name（必填），可含 company/title/profile_url。"""
    added = updated = 0
    from datetime import datetime as _dt

    with session_scope() as session:
        for it in items or []:
            name = str(it.get("name", "") or "").strip()
            if not name:
                continue
            company = str(it.get("company", "") or "").strip()
            url = str(it.get("profile_url", "") or it.get("url", "") or "").strip()
            key = _dedup_key(name, company, url)
            row = session.exec(
                select(Contact).where(Contact.user_id == user_id, Contact.key == key)
            ).first()
            if row is None:
                session.add(
                    Contact(
                        user_id=user_id, key=key, name=name, company=company,
                        title=str(it.get("title", "") or "").strip(),
                        profile_url=url, source="import",
                    )
                )
                added += 1
            else:
                row.company = company or row.company
                row.title = str(it.get("title", "") or "").strip() or row.title
                row.synced_at = _dt.utcnow()
                session.add(row)
                updated += 1
    return {"added": added, "updated": updated}


async def sync_linkedin_connections(user_id: int, limit: int = 50) -> dict:
    """慢速拟人化抓取领英人脉列表，入库去重。返回 {found, added, updated}。"""
    limit = max(1, min(200, int(limit or 50)))
    async with open_page("linkedin", headless=False) as page:
        await page.goto(_CONNECTIONS_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(random.uniform(1.5, 3.0))

        low = page.url.lower()
        if any(k in low for k in ("/login", "/authwall", "/checkpoint", "/signup")):
            raise RuntimeError("领英未登录，请先在『远程浏览器』里登录领英再同步人脉")

        # 慢速滚动加载，直到抓够 limit 或不再增长
        collected: dict[str, dict] = {}
        stagnant = 0
        for _ in range(40):
            try:
                items = await page.evaluate(_EXTRACT_JS)
            except Exception:  # noqa: BLE001
                items = []
            before = len(collected)
            for it in items or []:
                url = (it.get("url") or "").strip()
                name = (it.get("name") or "").strip()
                if not name:
                    continue
                k = url or name
                if k not in collected:
                    collected[k] = it
            if len(collected) >= limit:
                break
            stagnant = stagnant + 1 if len(collected) == before else 0
            if stagnant >= 4:
                break  # 连续几轮无新增，判定已到底/加载不出更多
            # 拟人化：慢速滚一段 + 随机停顿
            await page.mouse.wheel(0, random.randint(500, 1000))
            await asyncio.sleep(random.uniform(1.2, 2.8))

        found = list(collected.values())[:limit]
        if not found:
            try:
                await page.screenshot(path=None)  # noqa: F841 - 触发一次渲染
            except Exception:  # noqa: BLE001
                pass
            log.warning("领英人脉未解析到任何联系人，URL=%s", page.url)
            return {"found": 0, "added": 0, "updated": 0, "error": "未解析到人脉，可能 DOM 改版或未登录"}

    # 入库（去重 upsert）
    added = updated = 0
    now_syncing = found
    with session_scope() as session:
        for it in now_syncing:
            name = (it.get("name") or "").strip()
            occ = (it.get("occupation") or "").strip()
            url = (it.get("url") or "").strip()
            company = _company_from_occupation(occ)
            key = _dedup_key(name, company, url)
            row = session.exec(
                select(Contact).where(Contact.user_id == user_id, Contact.key == key)
            ).first()
            if row is None:
                session.add(
                    Contact(
                        user_id=user_id, key=key, name=name, company=company,
                        title=occ, profile_url=url, source="linkedin",
                    )
                )
                added += 1
            else:
                row.name = name or row.name
                row.company = company or row.company
                row.title = occ or row.title
                row.profile_url = url or row.profile_url
                from datetime import datetime as _dt

                row.synced_at = _dt.utcnow()
                session.add(row)
                updated += 1
    log.info("领英人脉同步：解析 %d，新增 %d，更新 %d。", len(found), added, updated)
    return {"found": len(found), "added": added, "updated": updated}
