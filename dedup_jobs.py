"""一次性清理 JobPosting 历史重复行：按"规范化指纹"(去掉 URL 查询参数)合并，
每组保留一条（优先已分析、其次最新），删除其余及其 Analysis/Delivery/JobFavorite，
并把保留行的 fingerprint 更新为规范化值，避免后续再生成重复。
运行：.venv/bin/python dedup_jobs.py
"""
import hashlib
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import delete
from sqlmodel import select

from app.db import session_scope
from app.models import Analysis, Delivery, JobFavorite, JobPosting


def norm_fp(site: str, url: str, title: str, company: str) -> str:
    u = (url or "").strip()
    if u:
        try:
            p = urlsplit(u)
            u = urlunsplit((p.scheme, p.netloc, p.path, "", "")) or u
        except Exception:
            pass
    basis = u or f"{site}|{title}|{company}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def chunked(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def main():
    with session_scope() as s:
        analyzed = {a.job_id for a in s.exec(select(Analysis)).all()}
        jobs = list(s.exec(select(JobPosting)).all())
        print("总职位:", len(jobs), "| 已分析职位:", len(analyzed))

        groups: dict[str, list] = {}
        for j in jobs:
            fp = norm_fp(j.site.value, j.url, j.title, j.company)
            groups.setdefault(fp, []).append(j)

        keepers = {}          # fp -> keeper job
        delete_ids: list[int] = []
        for fp, members in groups.items():
            # 保留：优先有分析的，其次 scraped_at 最新
            members.sort(
                key=lambda j: (j.id in analyzed, j.scraped_at or 0), reverse=True
            )
            keeper = members[0]
            keepers[fp] = keeper
            delete_ids.extend(m.id for m in members[1:])

        print("规范化后唯一职位:", len(groups), "| 将删除重复行:", len(delete_ids))

        # 删除依赖 + 重复职位（分批）
        for tbl in (Analysis, Delivery, JobFavorite):
            cnt = 0
            for ch in chunked(delete_ids):
                res = s.exec(delete(tbl).where(tbl.job_id.in_(ch)))
                cnt += res.rowcount or 0
            print(f"删除 {tbl.__name__}: {cnt}")
        jd = 0
        for ch in chunked(delete_ids):
            res = s.exec(delete(JobPosting).where(JobPosting.id.in_(ch)))
            jd += res.rowcount or 0
        print("删除 JobPosting:", jd)

        # 把保留行的指纹更新为规范化值（防止后续再长重复）
        fixed = 0
        for fp, keeper in keepers.items():
            if keeper.fingerprint != fp:
                keeper.fingerprint = fp
                s.add(keeper)
                fixed += 1
        print("更新保留行指纹:", fixed)
        s.commit()
        remain = len(list(s.exec(select(JobPosting)).all()))
        print("清理后职位总数:", remain)


if __name__ == "__main__":
    main()
