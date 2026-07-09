"""邮件推送：把职位（含完整 JD）与可选 AI 分析发送到配置的收件人列表。

SMTP 服务器与凭据来自 .env（敏感信息），收件人/开关来自数据库 EmailSetting。
发信用标准库 smtplib，支持 ssl(465) / starttls(587) / none 三种方式。
"""

from __future__ import annotations

import logging
import re
import smtplib
import socket
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape
from typing import Any, Iterable
from urllib.parse import urlparse

from ..config import get_settings

log = logging.getLogger("jobhunter.notifier")
_settings = get_settings()


def _smtp_proxy() -> str:
    """SMTP 出网代理：优先 smtp_proxy_url，留空则复用浏览器代理。

    browser_proxy_url 现在可能是逗号分隔的多代理（用于浏览器轮转），这里只取一个：
    取第一个连得通的（都不通则取第一个），避免把整串当成一个代理导致解析报错、发信失败。
    """
    raw = (_settings.smtp_proxy_url or _settings.browser_proxy_url or "").strip()
    if not raw:
        return ""
    cands = [p.strip() for p in raw.split(",") if p.strip()]
    if len(cands) <= 1:
        return cands[0] if cands else ""
    try:
        from ..browser import pick_working_proxy

        return pick_working_proxy(cands) or cands[0]
    except Exception:  # noqa: BLE001
        return cands[0]


def _proxy_tunnel(proxy_url: str, host: str, port: int, timeout: int = 30) -> socket.socket:
    """通过 HTTP CONNECT 代理建立到 host:port 的 TCP 隧道，返回已连通的裸 socket。"""
    pu = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    phost, pport = pu.hostname, pu.port or 8080
    sock = socket.create_connection((phost, pport), timeout)
    try:
        req = (
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n"
        )
        if pu.username:
            import base64

            cred = base64.b64encode(
                f"{pu.username}:{pu.password or ''}".encode()
            ).decode()
            req = req.replace("\r\n\r\n", f"\r\nProxy-Authorization: Basic {cred}\r\n\r\n")
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                break
            resp += chunk
        first = resp.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200 " not in first and not first.endswith(" 200"):
            raise EmailError(f"代理 CONNECT 失败: {first or '无响应'}")
        return sock
    except Exception:
        sock.close()
        raise


def _make_smtp_via_proxy(proxy_url: str, host: str, port: int, security: str, timeout: int = 30):
    """经代理隧道创建 smtplib 客户端（ssl/starttls/none 均支持）。"""
    raw = _proxy_tunnel(proxy_url, host, port, timeout)
    client = smtplib.SMTP()
    client._host = host  # starttls 的 SNI 需要
    if security == "ssl":
        ctx = ssl.create_default_context()
        raw = ctx.wrap_socket(raw, server_hostname=host)
    client.sock = raw
    client.file = None
    code, msg = client.getreply()
    if code != 220:
        client.close()
        raise EmailError(f"SMTP 握手失败: {code} {msg!r}")
    client.ehlo_or_helo_if_needed()
    if security == "starttls":
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
    return client


class EmailError(Exception):
    pass


def smtp_configured() -> bool:
    s = _settings
    return bool(s.smtp_host and s.smtp_user and s.smtp_password)


def parse_recipients(raw: str) -> list[str]:
    """支持逗号/分号/空格/换行分隔。"""
    parts = re.split(r"[,\s;]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def send_email(subject: str, html: str, recipients: Iterable[str]) -> int:
    """同步发送一封 HTML 邮件，返回收件人数量。阻塞调用，建议放线程里。"""
    recipients = [r for r in recipients if r]
    if not smtp_configured():
        raise EmailError(
            "未配置 SMTP，请在 .env 设置 SMTP_HOST / SMTP_USER / SMTP_PASSWORD"
        )
    if not recipients:
        raise EmailError("收件人列表为空")

    s = _settings
    from_addr = s.smtp_from or s.smtp_user
    msg = MIMEMultipart("alternative")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr((str(Header(s.smtp_from_name, "utf-8")), from_addr))
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(html, "html", "utf-8"))

    security = (s.smtp_security or "ssl").lower()
    proxy = _smtp_proxy()
    try:
        if proxy:
            # 本机直连 SMTP 不可达时，经 HTTP CONNECT 代理隧道发信
            srv = _make_smtp_via_proxy(proxy, s.smtp_host, s.smtp_port, security)
            try:
                srv.login(s.smtp_user, s.smtp_password)
                srv.sendmail(from_addr, recipients, msg.as_string())
            finally:
                try:
                    srv.quit()
                except Exception:  # noqa: BLE001
                    pass
        elif security == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=30) as srv:
                srv.login(s.smtp_user, s.smtp_password)
                srv.sendmail(from_addr, recipients, msg.as_string())
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as srv:
                if security == "starttls":
                    srv.starttls(context=ssl.create_default_context())
                srv.login(s.smtp_user, s.smtp_password)
                srv.sendmail(from_addr, recipients, msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            f"SMTP 认证失败（检查用户名/授权码）: {exc}"
        ) from exc
    except EmailError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EmailError(f"发送失败: {exc}") from exc

    log.info("已发送邮件 '%s' 给 %d 个收件人%s", subject, len(recipients), "（经代理）" if proxy else "")
    return len(recipients)


def _nl2br(text: str) -> str:
    return escape(text or "").replace("\n", "<br>")


def _sub_section(title: str, text: str | None) -> str:
    """分析卡片里的子区块（需学技能 / 简历改进），无内容则不渲染。"""
    if not (text or "").strip():
        return ""
    return (
        f'<div style="margin-top:8px;">'
        f'<div style="color:#6d28d9;font-weight:600;font-size:12px;margin-bottom:2px;">{escape(title)}</div>'
        f'<div style="color:#475569;font-size:12px;line-height:1.6;">{_nl2br(text)}</div></div>'
    )


def _score_color(score: int) -> str:
    if score >= 75:
        return "#059669"
    if score >= 50:
        return "#d97706"
    return "#64748b"


_SITE_NAME = {
    "zhilian": "智联招聘",
    "linkedin": "领英",
    "boss": "BOSS直聘",
    "liepin": "猎聘",
    "job51": "前程无忧",
}


def _job_card_html(item: dict[str, Any], include_analysis: bool) -> str:
    job = item.get("job", {})
    analysis = item.get("analysis")

    title = escape(job.get("title") or "(无标题)")
    url = job.get("url") or ""
    title_html = (
        f'<a href="{escape(url)}" style="color:#1d4ed8;text-decoration:none;">{title}</a>'
        if url
        else title
    )
    meta = " · ".join(
        escape(x) for x in [job.get("company"), job.get("salary"), job.get("location")] if x
    )
    sub = " · ".join(
        escape(x) for x in [job.get("experience"), job.get("education")] if x
    )
    site = _SITE_NAME.get(job.get("site"), job.get("site") or "")

    tags = [t for t in (job.get("tags") or "").split(", ") if t]
    tags_html = "".join(
        f'<span style="display:inline-block;background:#eff6ff;color:#1d4ed8;'
        f'font-size:12px;padding:2px 8px;border-radius:6px;margin:2px 4px 2px 0;">{escape(t)}</span>'
        for t in tags
    )

    desc = job.get("description") or ""
    desc_html = (
        f'<div style="margin-top:10px;"><div style="color:#475569;font-weight:600;'
        f'font-size:13px;margin-bottom:4px;">职位描述</div>'
        f'<div style="color:#334155;font-size:13px;line-height:1.6;background:#f8fafc;'
        f'border:1px solid #e2e8f0;border-radius:8px;padding:10px;">{_nl2br(desc)}</div></div>'
        if desc.strip()
        else '<div style="margin-top:8px;color:#94a3b8;font-size:12px;">（未抓到完整职位描述）</div>'
    )

    analysis_html = ""
    if include_analysis and analysis:
        score = int(analysis.get("match_score") or 0)
        analysis_html = (
            f'<div style="margin-top:12px;padding:10px;background:#f5f3ff;'
            f'border:1px solid #ddd6fe;border-radius:8px;">'
            f'<div style="font-size:13px;font-weight:600;color:#6d28d9;margin-bottom:4px;">'
            f'AI 匹配度 '
            f'<span style="background:{_score_color(score)};color:#fff;padding:1px 8px;'
            f'border-radius:6px;">{score}</span></div>'
            f'<div style="color:#334155;font-size:13px;line-height:1.6;">{_nl2br(analysis.get("summary") or "")}</div>'
            f'<div style="color:#475569;font-size:12px;line-height:1.6;margin-top:6px;">'
            f'{_nl2br(analysis.get("advice") or "")}</div>'
            + _sub_section("需要补强的技能", analysis.get("skills_to_learn"))
            + _sub_section("简历改进建议", analysis.get("resume_tips"))
            + "</div>"
        )

    score_badge = ""
    if analysis is not None:
        sc = int(analysis.get("match_score") or 0)
        score_badge = (
            f'<span style="float:right;font-size:11px;color:#fff;'
            f'background:{_score_color(sc)};padding:2px 8px;border-radius:6px;'
            f'font-weight:600;margin-left:6px;">匹配 {sc}</span>'
        )

    return (
        '<div style="border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:14px;">'
        f'<div style="font-size:16px;font-weight:700;color:#0f172a;">{title_html}'
        f'<span style="float:right;font-size:11px;color:#64748b;background:#f1f5f9;'
        f'padding:2px 8px;border-radius:6px;font-weight:400;">{escape(site)}</span>'
        f'{score_badge}</div>'
        f'<div style="color:#475569;font-size:13px;margin-top:4px;">{meta}</div>'
        + (f'<div style="color:#94a3b8;font-size:12px;margin-top:2px;">{sub}</div>' if sub else "")
        + (f'<div style="margin-top:6px;">{tags_html}</div>' if tags_html else "")
        + desc_html
        + analysis_html
        + "</div>"
    )


def build_jobs_html(
    items: list[dict[str, Any]],
    include_analysis: bool,
    manage_url: str | None = None,
) -> str:
    cards = "".join(_job_card_html(it, include_analysis) for it in items)
    if manage_url:
        manage_html = (
            '<div style="color:#94a3b8;font-size:12px;text-align:center;margin-top:8px;">'
            f'<a href="{escape(manage_url)}" style="color:#64748b;">管理订阅</a> · '
            f'<a href="{escape(manage_url)}" style="color:#64748b;">退订</a><br>'
            "由 Job Hunter Agent 自动发送</div>"
        )
    else:
        manage_html = (
            '<div style="color:#94a3b8;font-size:12px;text-align:center;margin-top:8px;">'
            "由 Job Hunter Agent 自动发送</div>"
        )
    return (
        '<div style="max-width:680px;margin:0 auto;font-family:-apple-system,'
        'Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#f1f5f9;padding:20px;">'
        '<div style="background:#0f172a;color:#fff;border-radius:12px;padding:16px 20px;margin-bottom:16px;">'
        '<div style="font-size:18px;font-weight:700;">Job Hunter · 职位推送</div>'
        f'<div style="color:#94a3b8;font-size:13px;margin-top:2px;">本次共 {len(items)} 个职位'
        + ("（含 AI 分析建议）" if include_analysis else "")
        + "</div></div>"
        + cards
        + manage_html
        + "</div>"
    )
