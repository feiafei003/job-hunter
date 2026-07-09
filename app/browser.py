"""Playwright 持久化上下文管理。

每个站点使用独立的 user_data_dir，登录态（cookies/localStorage）保存在本地，
之后定时抓取直接复用，无需重复登录，也降低反爬风控概率。
"""

import asyncio
import glob
import logging
import os
import shutil as _shutil
import socket
import subprocess
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

# 用 rebrowser 补丁版 Playwright：消除 Runtime.enable 等 CDP 自动化特征，
# 以通过 BOSS 等强反爬站点对"调试器已挂载"的检测（普通 playwright 会被吐白板）。
os.environ.setdefault("REBROWSER_PATCHES_RUNTIME_FIX_MODE", "addBinding")

from rebrowser_playwright.async_api import BrowserContext, Page, async_playwright

from .config import get_settings

_settings = get_settings()

# 每个站点一把锁：同一份持久化 profile 不能被两个 Chromium 同时打开，
# 否则后启动的会因 single-instance 锁而立即退出（窗口"闪退"）。
_site_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# 托管 debug Chrome 的启动锁与进程句柄（避免并发抓取时重复拉起多个）
_debug_launch_lock = asyncio.Lock()
_debug_proc: subprocess.Popen | None = None

# 当前生效的出网代理（由 pick_working_proxy 选定并记录，供失效检测/状态展示）
_active_proxy: str = ""


def _proxy_alive(proxy_url: str, timeout: float = 6.0,
                 probe_host: str = "www.zhipin.com", probe_port: int = 443) -> bool:
    """探测一个 HTTP 代理是否能建立 HTTPS CONNECT 隧道（招聘站用 https）。

    直接做一次 CONNECT 握手，看代理是否回 200——这正是代理“挂掉”时会失败的环节
    （端口能连上但 CONNECT 被拒/超时）。不依赖外部库，轻量可靠。
    """
    from urllib.parse import urlparse

    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return False
    u = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    host, port = u.hostname, u.port or 8080
    if not host:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        req = (
            f"CONNECT {probe_host}:{probe_port} HTTP/1.1\r\n"
            f"Host: {probe_host}:{probe_port}\r\n\r\n"
        ).encode()
        s.sendall(req)
        resp = s.recv(128)
        first = resp.split(b"\r\n", 1)[0] if resp else b""
        return b" 200" in first
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


def pick_working_proxy(candidates: list[str]) -> str:
    """按顺序探测候选代理，返回第一个连得通的。

    都不通时退而返回第一个候选（降级使用，并记日志，便于人工排查），
    候选为空则返回 ""（直连）。
    """
    log = logging.getLogger("jobhunter.browser")
    cands = [c for c in (candidates or []) if c]
    if not cands:
        return ""
    for p in cands:
        if _proxy_alive(p):
            if p != cands[0]:
                log.warning("首选代理不可用，轮转到可用代理：%s", p)
            return p
    log.error("所有代理探测均失败：%s（仍按首个降级使用，请检查代理）", cands)
    return cands[0]


def active_proxy() -> str:
    """当前 Chrome 生效的出网代理（空=直连）。"""
    return _active_proxy


def _ensure_localhost_no_proxy() -> None:
    """公司代理会劫持发往本机的请求，导致连不上 Chrome 调试端口；这里绕过。"""
    no_proxy = os.environ.get("NO_PROXY", "")
    for host in ("localhost", "127.0.0.1", "::1"):
        if host not in no_proxy:
            no_proxy = f"{no_proxy},{host}" if no_proxy else host
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy


def _find_playwright_chromium() -> str | None:
    """查 Playwright/rebrowser 自带的 Chromium 可执行文件。

    无系统 Chrome 的环境（多数 Linux 容器/云主机）下做兜底，
    需要先跑过 `python -m playwright install chromium` 或
    `python -m rebrowser_playwright install chromium`。
    """
    search_roots: list[str] = []
    for env_key in ("PLAYWRIGHT_BROWSERS_PATH", "REBROWSER_BROWSERS_PATH"):
        val = os.environ.get(env_key)
        if val and val not in {"0", ""}:
            search_roots.append(val)
    home = os.path.expanduser("~")
    search_roots += [
        os.path.join(home, ".cache", "ms-playwright"),
        os.path.join(home, ".cache", "rebrowser-playwright"),
        "/ms-playwright",
    ]
    if sys.platform == "win32":
        binaries = ("chrome.exe",)
        sub_dirs = ("chrome-win",)
    elif sys.platform == "darwin":
        binaries = ("Chromium.app/Contents/MacOS/Chromium",
                    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
        sub_dirs = ("chrome-mac",)
    else:
        binaries = ("chrome",)
        sub_dirs = ("chrome-linux",)
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for sub in sub_dirs:
            for b in binaries:
                pattern = os.path.join(root, "chromium-*", sub, b)
                matches = sorted(glob.glob(pattern))
                if matches:
                    return matches[-1]
    return None


def _find_chrome() -> str | None:
    """跨平台找一个可用的 Chrome / Chromium。

    优先顺序：系统已安装的真实 Chrome > Playwright 自带 Chromium。
    没有系统 Chrome 的纯 Linux 服务器会走兜底（更常见的部署场景）。
    """
    candidates: list[str] = []
    if sys.platform == "win32":
        candidates += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                r"Google\Chrome\Application\chrome.exe",
            ),
        ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        ]
    else:
        candidates += [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/opt/google/chrome/chrome",
        ]
        for name in (
            "google-chrome", "google-chrome-stable",
            "chromium", "chromium-browser",
        ):
            p = _shutil.which(name)
            if p and p not in candidates:
                candidates.append(p)

    for c in candidates:
        if c and os.path.exists(c):
            return c

    return _find_playwright_chromium()


def _port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


def _ipv4_loopback(endpoint: str) -> str:
    """把 localhost 形式的 CDP 端点强制成 IPv4 127.0.0.1。

    部分机器 localhost 优先解析到 IPv6 ::1，而托管 Chrome 只监听 127.0.0.1，
    用 localhost 连接会得到 connect ECONNREFUSED ::1。实测该 Chromium 接受
    Host: 127.0.0.1（/json/version 返回 200），故统一走 IPv4 回环。
    """
    return (endpoint or "").replace("localhost", "127.0.0.1")


def cdp_enabled() -> bool:
    """是否启用了接管真实 Chrome（手动端点或托管 debug Chrome）。"""
    return bool((_settings.cdp_endpoint or "").strip() or _settings.use_debug_chrome)


def debug_chrome_running() -> bool:
    """托管的 debug Chrome 当前是否在运行（端口可连）。"""
    return _port_open(_settings.chrome_debug_port)


async def ensure_debug_chrome(port: int) -> str:
    """确保托管 debug Chrome 在运行：未运行则自动启动，返回可连接的 CDP 端点。"""
    log = logging.getLogger("jobhunter.browser")
    endpoint = f"http://127.0.0.1:{port}"
    if _port_open(port):
        await _ensure_debug_view_proxy(port)
        return endpoint

    async with _debug_launch_lock:
        if _port_open(port):  # 等锁期间别的协程已拉起
            await _ensure_debug_view_proxy(port)
            return endpoint
        chrome = _find_chrome()
        if not chrome:
            hint_install = (
                "运行 `python -m playwright install chromium` "
                "（或 `python -m rebrowser_playwright install chromium`）下载内置 Chromium，"
                "或安装系统 Chrome / Chromium 后重试。"
            )
            raise RuntimeError(
                "未找到任何可用的 Chrome / Chromium，无法启动托管调试浏览器。"
                + hint_install
                + " 也可在 .env 里关闭 USE_DEBUG_CHROME=false。"
            )
        data_dir = str(_settings.data_path / "chrome-debug")
        os.makedirs(data_dir, exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={data_dir}",
            # 关键：默认 UA 会暴露 "HeadlessChrome/..."（且是 Linux），BOSS 等强反爬
            # 站点据此判定异常 → 提示「该地区不支持」/吐白板。查看器开的标签直接走
            # /json/new，不经过 playwright 伪装，故必须在进程层面伪装成正常桌面 Chrome。
            f"--user-agent={_USER_AGENT}",
            "--lang=zh-CN",
            "--accept-lang=zh-CN,zh;q=0.9,en;q=0.8",
            "--disable-blink-features=AutomationControlled",
            # 服务器无桌面密钥环时，Chrome 尝试连接 OS keyring 会触发崩溃/卡死；
            # basic 走内置明文存储，配合关闭崩溃上报，避免启动后 SIGTRAP 退出。
            "--password-store=basic",
            "--disable-breakpad",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        # 出网代理：让托管 Chrome 经中国出口代理访问招聘网站，绕过按地区的 IP 封锁。
        # 多个候选按顺序探测，用第一个连得通的；本机回环（CDP 调试口、看屏转发）必须
        # 绕过代理，否则连不上自己。
        global _active_proxy
        proxy = pick_working_proxy(_settings.browser_proxy_list)
        _active_proxy = proxy
        if proxy:
            args.append(f"--proxy-server={proxy}")
            args.append("--proxy-bypass-list=127.0.0.1;localhost;[::1]")
            log.info("托管 Chrome 将经代理出网：%s", proxy)
        # 非 Windows 服务器常见情况：以 root / 容器 / 无桌面运行，必须放沙箱并默认走
        # 新版 headless（仍支持 CDP 远程调试，远程通过 9222 浏览页面 / 解风控）。
        if sys.platform != "win32":
            args += [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            if not os.environ.get("DISPLAY") and "--headless" not in " ".join(args):
                # 没有 X 显示器就走 headless（仍可通过 CDP 远程操作）
                args.append("--headless=new")
        args.append("about:blank")
        log.info("托管 debug Chrome 未运行，正在启动（端口 %d，binary=%s）...", port, chrome)
        global _debug_proc
        _debug_proc = subprocess.Popen(args)
        for _ in range(60):  # 最多等 ~30 秒
            await asyncio.sleep(0.5)
            if _port_open(port):
                log.info("托管 debug Chrome 已就绪：%s", endpoint)
                await asyncio.sleep(1.0)  # 给 CDP /json 端点一点初始化时间
                await _ensure_debug_view_proxy(port)
                return endpoint
        raise RuntimeError("启动 debug Chrome 后无法连接其调试端口，请重试或检查 Chrome。")


def stop_debug_chrome() -> None:
    """关闭托管 debug Chrome（用于换代理后重启）。

    优先终止本进程拉起的句柄；再兜底按调试端口杀掉任何残留（可能是外部拉起的）。
    pkill 默认排除自身 PID，且本进程 argv 不含该模式，故不会误杀自己。
    """
    log = logging.getLogger("jobhunter.browser")
    global _debug_proc
    try:
        if _debug_proc and _debug_proc.poll() is None:
            _debug_proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    _debug_proc = None
    try:
        subprocess.run(
            ["pkill", "-f", f"remote-debugging-port={_settings.chrome_debug_port}"],
            check=False,
        )
    except Exception:  # noqa: BLE001
        pass
    log.info("已请求关闭托管 debug Chrome（端口 %d）。", _settings.chrome_debug_port)


async def ensure_working_proxy_chrome() -> str:
    """抓取前调用：确保托管 Chrome 在线，且其出网代理可用。

    - 未配代理：仅确保 Chrome 在线，返回 ""。
    - 配了代理：若 Chrome 在线且当前代理仍可用，原样复用；否则挑一个可用代理，
      （必要时）重启 Chrome 让新代理生效。返回当前生效代理。

    用 CDP 托管模式时才有意义；其余情况直接返回。
    """
    log = logging.getLogger("jobhunter.browser")
    port = _settings.chrome_debug_port
    if not cdp_enabled() or not _settings.use_debug_chrome:
        return _active_proxy

    candidates = _settings.browser_proxy_list
    if not candidates:
        await ensure_debug_chrome(port)  # 无代理：只保证 Chrome 在
        return ""

    running = _port_open(port)
    if running and _active_proxy and _proxy_alive(_active_proxy):
        return _active_proxy  # 现状良好，直接复用

    fresh = pick_working_proxy(candidates)
    # Chrome 在跑但代理需要更换 → 必须重启 Chrome 才能换 --proxy-server
    if running and fresh != _active_proxy:
        log.warning("当前代理 %s 不可用，换用 %s 并重启 Chrome。", _active_proxy or "(无)", fresh)
        stop_debug_chrome()
        for _ in range(20):
            if not _port_open(port):
                break
            await asyncio.sleep(0.3)
    await ensure_debug_chrome(port)  # 内部会再次 pick 并记录 _active_proxy
    return _active_proxy


# 把只绑 127.0.0.1 的 Chrome 调试口，经本机 L4 TCP 转发暴露到 0.0.0.0:<view_port>，
# 供从局域网其他机器直接打开 http://<IP>:<view_port> 看页面 / 登录。仅启动一次。
_view_proxy_server: "asyncio.AbstractServer | None" = None


async def _ensure_debug_view_proxy(target_port: int) -> None:
    global _view_proxy_server
    view_port = int(getattr(_settings, "chrome_debug_view_port", 0) or 0)
    if view_port <= 0 or _view_proxy_server is not None:
        return
    log = logging.getLogger("jobhunter.browser")

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            t_reader, t_writer = await asyncio.open_connection("127.0.0.1", target_port)
        except Exception:  # noqa: BLE001
            writer.close()
            return

        async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(_pipe(reader, t_writer), _pipe(t_reader, writer))

    try:
        _view_proxy_server = await asyncio.start_server(_handle, "0.0.0.0", view_port)
        log.info(
            "调试浏览器已暴露到局域网：http://<本机IP>:%d （转发到 127.0.0.1:%d）",
            view_port,
            target_port,
        )
    except OSError as exc:  # 端口被占用等
        log.warning("暴露调试浏览器到 :%d 失败：%s", view_port, exc)
        _view_proxy_server = None


# 各站点“已登录”的标志性 cookie（任一存在且非空即视为已登录）。基于真实抓包确定。
_LOGIN_COOKIES = {
    "zhilian": ("at", "rt"),               # 智联登录后的访问/刷新令牌
    "boss": ("wt2", "zp_at", "bst"),       # BOSS 登录态
    "linkedin": ("li_at",),                # 领英登录态
    "liepin": ("lt_auth", "c_flag"),       # 猎聘登录态（lt_auth 为登录令牌，c_flag 兜底）
    "job51": ("uid", "51"),                # 前程无忧登录态：www 的 uid、.51job.com 的 51(cuid)
}
_SITE_COOKIE_DOMAIN = {
    "zhilian": "zhaopin.com",
    "boss": "zhipin.com",
    "linkedin": "linkedin",
    "liepin": "liepin.com",
    "job51": "51job.com",
}

# 心跳目标 URL（尽量选需要登录态的会员/首页，让服务端刷新会话）。
_HEARTBEAT_URL = {
    "zhilian": "https://i.zhaopin.com/",
    "boss": "https://www.zhipin.com/",
    "linkedin": "https://www.linkedin.com/feed/",
    "liepin": "https://www.liepin.com/",
    "job51": "https://my.51job.com/",
}


async def site_login_status() -> dict:
    """读取托管 Chrome 的 cookie，判断各站点是否已登录。"""
    result = {s: False for s in _LOGIN_COOKIES}
    if not cdp_enabled() or not _port_open(_settings.chrome_debug_port):
        return result
    _ensure_localhost_no_proxy()
    endpoint = _ipv4_loopback(
        (_settings.cdp_endpoint or "").strip()
        or f"http://127.0.0.1:{_settings.chrome_debug_port}"
    )
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=10000)
            # 不要 browser.close()：那会关掉真实 Chrome。读完 cookie 让 pw 退出即断连。
            cookies = (
                await browser.contexts[0].cookies() if browser.contexts else []
            )
    except Exception:  # noqa: BLE001
        return result

    for site, names in _LOGIN_COOKIES.items():
        dom = _SITE_COOKIE_DOMAIN[site]
        for c in cookies:
            if (
                dom in (c.get("domain") or "")
                and c.get("name") in names
                and (c.get("value") or "")
            ):
                result[site] = True
                break
    return result


async def heartbeat() -> dict:
    """给已登录站点发送轻量 HTTP 请求，刷新会话 cookie，尽量延长登录态。

    用 context.request（携带浏览器 cookie 的纯 HTTP 请求，不渲染页面、不跑 JS），
    反爬足迹最小；收到的 Set-Cookie 会写回浏览器上下文，延长会话 TTL。
    返回 {site: 状态码/err} 便于排查。仅在接管真实 Chrome 时生效。
    """
    log = logging.getLogger("jobhunter.browser")
    result: dict[str, str] = {}
    if not cdp_enabled() or not _port_open(_settings.chrome_debug_port):
        return result
    _ensure_localhost_no_proxy()
    endpoint = _ipv4_loopback(
        (_settings.cdp_endpoint or "").strip()
        or f"http://127.0.0.1:{_settings.chrome_debug_port}"
    )

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(endpoint, timeout=10000)
            # 绝不 browser.close()：会关掉真实 Chrome。pw 退出即断连，不杀进程。
            if not browser.contexts:
                return result
            context = browser.contexts[0]
            try:
                cookies = await context.cookies()
            except Exception:  # noqa: BLE001
                cookies = []

            for site, names in _LOGIN_COOKIES.items():
                dom = _SITE_COOKIE_DOMAIN[site]
                logged_in = any(
                    dom in (c.get("domain") or "")
                    and c.get("name") in names
                    and (c.get("value") or "")
                    for c in cookies
                )
                url = _HEARTBEAT_URL.get(site)
                if not logged_in or not url:
                    continue
                try:
                    resp = await context.request.get(
                        url,
                        headers={"User-Agent": _USER_AGENT, "Referer": url},
                        timeout=15000,
                    )
                    result[site] = str(resp.status)
                    log.info("会话心跳 %s：%s -> %s", site, url, resp.status)
                except Exception as exc:  # noqa: BLE001
                    result[site] = "err"
                    log.warning("会话心跳 %s 失败：%s", site, str(exc)[:80])
    except Exception as exc:  # noqa: BLE001
        log.warning("会话心跳连接调试 Chrome 失败：%s", str(exc)[:80])
    return result


async def _resolve_cdp_endpoint() -> str:
    """解析最终要连接的 CDP 端点：手动端点优先，否则按需托管启动。"""
    manual = (_settings.cdp_endpoint or "").strip()
    if manual:
        return manual
    if _settings.use_debug_chrome:
        return await ensure_debug_chrome(_settings.chrome_debug_port)
    return ""

# 一个相对真实的桌面 UA，减少被识别为自动化
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 隐藏 navigator.webdriver 等自动化特征
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
"""


def _profile_dir(site: str) -> str:
    d = _settings.browser_profile_dir / site
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _default_chrome_user_data_dir() -> str:
    """跨平台返回当前用户的真实 Chrome 数据目录。

    - Windows: %LOCALAPPDATA%\\Google\\Chrome\\User Data
    - macOS:  ~/Library/Application Support/Google/Chrome
    - Linux:  ~/.config/google-chrome 或 ~/.config/chromium

    Linux 服务器一般装的是 Playwright 自带 chromium，没有真实 Chrome
    可复制；这种情况下返回的目录可能不存在，调用方需自行处理。
    """
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "")
        return os.path.join(base, "Google", "Chrome", "User Data")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Google", "Chrome")
    for cand in (
        os.path.join(home, ".config", "google-chrome"),
        os.path.join(home, ".config", "chromium"),
    ):
        if os.path.isdir(cand):
            return cand
    return os.path.join(home, ".config", "google-chrome")


def _prepare_real_profile_copy() -> str:
    """把真实 Chrome 配置里的登录态/Cookie 复制到独立目录后返回该目录。

    新版 Chrome 禁止对"默认用户数据目录"开启远程调试（Playwright 必需），
    因此不能直接用原目录；复制一份到非默认目录即可绕过，同时继承登录与反爬信誉。
    只复制关键数据（Cookie/本地存储/登录信息），跳过庞大的缓存，速度快。
    需在 Chrome 已关闭时进行，否则 Cookie 库被占用。
    """
    import shutil
    from pathlib import Path

    src_root = Path(_settings.chrome_user_data_dir or _default_chrome_user_data_dir())
    if not src_root.exists():
        raise RuntimeError(
            f"USE_REAL_CHROME_PROFILE=true 但找不到真实 Chrome 数据目录：{src_root}。"
            " 在 Linux 服务器上一般没有真人登录过的 Chrome 配置，请关闭该开关"
            "（USE_REAL_CHROME_PROFILE=false）改用 USE_DEBUG_CHROME 托管模式。"
        )
    prof = _settings.chrome_profile_dir or "Default"
    dst_root = _settings.browser_profile_dir / "_chrome_copy"
    dst_prof = dst_root / prof
    dst_prof.mkdir(parents=True, exist_ok=True)

    # Local State 含 Cookie 加密密钥，必须一起复制
    try:
        shutil.copy2(src_root / "Local State", dst_root / "Local State")
    except Exception:  # noqa: BLE001
        pass

    src_prof = src_root / prof
    # 只复制关键登录数据，跳过缓存等大目录
    items = [
        "Network",  # 新版 Cookie 在 Network/Cookies
        "Cookies",  # 旧版
        "Local Storage",
        "Session Storage",
        "Preferences",
        "Login Data",
        "Web Data",
    ]
    for name in items:
        s = src_prof / name
        d = dst_prof / name
        try:
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            elif s.is_file():
                shutil.copy2(s, d)
        except Exception:  # noqa: BLE001
            continue

    return str(dst_root)


@asynccontextmanager
async def open_context(
    site: str, headless: bool | None = None
) -> AsyncIterator[BrowserContext]:
    """打开（或复用）某站点的持久化浏览器上下文。

    headless=None 时使用配置默认值；登录场景应显式传 headless=False。
    """
    if headless is None:
        headless = _settings.scrape_headless

    log = logging.getLogger("jobhunter.browser")

    # CDP 模式：接管托管/手动启动的真实 Chrome（最强反爬绕过）。
    cdp = await _resolve_cdp_endpoint()
    if cdp:
        _ensure_localhost_no_proxy()
        # 统一走 IPv4 回环：localhost 可能优先解析 IPv6 ::1，而托管 Chrome 仅监听 127.0.0.1
        cdp = _ipv4_loopback(cdp)
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(cdp, timeout=15000)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"无法连接到调试 Chrome（{cdp}）。原始错误：{exc}"
                ) from exc
            created_ctx = None
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = await browser.new_context()
                created_ctx = context
            # CDP 模式同样注入反检测脚本（隐藏 navigator.webdriver 等），
            # 否则程序新开/导航的页面会被 BOSS 等强反爬站点识破吐成白板。
            try:
                await context.add_init_script(_STEALTH_JS)
            except Exception:  # noqa: BLE001
                pass
            log.info("已通过 CDP 接管真实 Chrome（%s），复用你登录好的会话。", cdp)
            try:
                yield context
            finally:
                # 只清理我们自己新建的临时 context；绝不调用 browser.close()，
                # 因为对 connect_over_cdp 接管的浏览器，close() 会把真实 Chrome 整个关掉。
                # 退出 async_playwright() 时会自动断开 CDP 连接，但不会终止 Chrome 进程。
                if created_ctx is not None:
                    try:
                        await created_ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
        return

    use_real = _settings.use_real_chrome_profile
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]

    if use_real:
        # 复制真实 Chrome 配置到独立目录（绕过"默认目录禁止远程调试"），继承登录与信誉
        user_data_dir = _prepare_real_profile_copy()
        args.append(f"--profile-directory={_settings.chrome_profile_dir}")
        lock_key = "__real_chrome__"  # 同一份副本全站共用一把锁
    else:
        user_data_dir = _profile_dir(site)
        lock_key = site

    launch_kwargs = dict(
        user_data_dir=user_data_dir,
        headless=headless,
        user_agent=_USER_AGENT,
        viewport={"width": 1366, "height": 850},
        locale="zh-CN",
        args=args,
        timeout=60000,
    )
    # 出网代理：同托管模式，多候选探测取第一个可用的，绕过招聘网站地区封锁。
    _bproxy = pick_working_proxy(_settings.browser_proxy_list)
    if _bproxy:
        launch_kwargs["proxy"] = {"server": _bproxy}

    async with _site_locks[lock_key]:
        async with async_playwright() as pw:
            channel = (_settings.browser_channel or "").strip()
            if use_real and not channel:
                channel = "chrome"  # 用真实 profile 必须配真 Chrome

            context = None
            if channel:
                try:
                    context = await pw.chromium.launch_persistent_context(
                        channel=channel, **launch_kwargs
                    )
                except Exception as exc:  # noqa: BLE001
                    if use_real:
                        raise RuntimeError(
                            "用你的 Chrome 配置副本启动失败。请确认：①已彻底关闭 Chrome"
                            "（含后台托盘）后再跑，以便完整复制登录态；②CHROME_PROFILE_DIR "
                            "是你登录智联用的那个配置。" + f"原始错误：{exc}"
                        ) from exc
                    log.warning("用渠道 '%s' 启动失败，回退自带 Chromium：%s", channel, exc)
            if context is None:
                context = await pw.chromium.launch_persistent_context(**launch_kwargs)
            await context.add_init_script(_STEALTH_JS)
            try:
                yield context
            finally:
                await context.close()


@asynccontextmanager
async def open_page(
    site: str, headless: bool | None = None
) -> AsyncIterator[Page]:
    async with open_context(site, headless=headless) as context:
        # CDP 模式下，另开新标签抓取，避免抢占/打断你正在用的标签页（并发安全）。
        if cdp_enabled():
            page = await context.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
            return
        page = context.pages[0] if context.pages else await context.new_page()
        yield page


# 仍停留在"登录/验证墙"页面的 URL 特征（命中即视为尚未登录完成）
_LOGIN_URL_HINTS = (
    "login",
    "passport",
    "authwall",
    "uas/",
    "checkpoint",
    "signin",
    "signup",
    "cold-join",
    "/customer/",
)


def _still_on_login(url: str) -> bool:
    if not url or url.startswith("about:"):
        return True
    u = url.lower()
    return any(h in u for h in _LOGIN_URL_HINTS)


async def interactive_login(
    site: str,
    login_url: str,
    wait_seconds: int = 180,
    wait_for_manual_close: bool = False,
) -> None:
    """以可见模式打开站点登录页，给用户手动登录（扫码/输验证码）。

    轮询检测：一旦页面跳离登录/验证墙页面即视为登录成功，短暂等待让
    Cookie 落盘后立即关窗，不再死等满 wait_seconds。用户手动关掉窗口也会
    立刻结束。wait_seconds 仅作为兜底上限。

    全程捕获异常并写日志，避免后台任务静默失败导致窗口"闪退"。
    """
    import time

    log = logging.getLogger("jobhunter.browser")
    is_cdp = cdp_enabled()
    log.info("开始 %s 登录流程%s...", site, "（在托管 Chrome 中新开登录标签）" if is_cdp else "，打开可见浏览器窗口")

    def _gone() -> bool:
        """登录页/标签是否已被用户关闭。"""
        if is_cdp:
            return page.is_closed()
        return not context.pages

    def _current_url() -> str | None:
        try:
            if is_cdp:
                return None if page.is_closed() else page.url
            return context.pages[-1].url
        except Exception:  # noqa: BLE001  页面正在跳转
            return None

    login_cookies = _LOGIN_COOKIES.get(site)
    cookie_domain = _SITE_COOKIE_DOMAIN.get(site, "")

    async def _logged_in_by_cookie() -> bool:
        """按站点登录态 cookie 判断是否已登录，比 URL 判断更可靠。

        有些站点登录页/首页 URL 不含 login 等关键词（如猎聘 login_url 是首页），
        纯 URL 判断会在用户登录前就误判成功并关掉标签；用 cookie 判断可避免。
        """
        if not login_cookies:
            return False
        try:
            cookies = await context.cookies()
        except Exception:  # noqa: BLE001
            return False
        for c in cookies:
            if (
                cookie_domain in (c.get("domain") or "")
                and c.get("name") in login_cookies
                and (c.get("value") or "")
            ):
                return True
        return False

    async def _cleanup() -> None:
        # CDP 模式只关我们新开的登录标签，绝不关整个浏览器；非 CDP 由 open_context 收尾。
        if is_cdp:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        async with open_context(site, headless=False) as context:
            if is_cdp:
                page = await context.new_page()
            else:
                page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:  # noqa: BLE001
                log.warning("打开登录页失败（标签保持打开，可手动导航）: %s", exc)

            if wait_for_manual_close:
                log.info(
                    "登录标签已打开。请完成『安全验证』并扫码/登录，"
                    "完成后【手动关闭该标签】即保存登录态。最多等待 %d 秒。",
                    wait_seconds,
                )
                deadline = time.monotonic() + wait_seconds
                while time.monotonic() < deadline:
                    await asyncio.sleep(1.5)
                    if _gone():
                        log.info("%s 登录标签被关闭，登录态已保存。", site)
                        return
                log.info("%s 登录等待超时（登录态已保存）。", site)
                await _cleanup()
                return

            log.info(
                "登录标签已打开，请完成登录（扫码/账号密码）。检测到登录成功会自动关闭，"
                "最多等待 %d 秒。",
                wait_seconds,
            )
            deadline = time.monotonic() + wait_seconds
            logged_in = False
            while time.monotonic() < deadline:
                await asyncio.sleep(1.5)
                if _gone():
                    log.info("%s 登录标签被关闭，结束等待（登录态已保存）。", site)
                    return
                # 优先用登录态 cookie 判断；没有定义 cookie 的站点回退到 URL 判断。
                if login_cookies:
                    if await _logged_in_by_cookie():
                        logged_in = True
                        log.info("检测到 %s 登录态 cookie，登录成功。", site)
                        break
                else:
                    cur = _current_url()
                    if cur and not _still_on_login(cur):
                        logged_in = True
                        log.info("检测到 %s 已登录（跳转到 %s）。", site, cur)
                        break

            if logged_in:
                await asyncio.sleep(3)  # 给 Cookie 落盘留时间
                log.info("%s 登录成功（登录态已保存）。", site)
            else:
                log.info("%s 登录等待超时。", site)
            await _cleanup()
    except Exception as exc:  # noqa: BLE001
        log.exception("%s 登录流程异常: %s", site, exc)
        raise
