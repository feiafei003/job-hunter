from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置，从 .env 读取。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 大模型后端：cursor（Cursor SDK）或 deepseek
    llm_provider: str = "cursor"

    # Cursor SDK
    cursor_api_key: str = ""
    cursor_model: str = "auto"
    # 是否让 Cursor 请求走系统代理。默认 False=绕过代理直连。
    # 内网/容器若只有走公司代理才能出网（直连被防火墙挡），应设为 True。
    cursor_use_proxy: bool = False
    # 显式指定 Cursor 走的代理地址（如 http://proxy-host:port）。留空则用环境里的
    # HTTP(S)_PROXY。systemd 等不继承登录 shell 代理变量的场景建议在这里写死。
    cursor_proxy_url: str = ""

    # Cursor 中转(relay)：本机连不上 Cursor 云端时，把请求转发到一台能直连的服务器
    # （如阿里云 ECS）上运行的中转服务。设置后 Cursor 调用不再走本地 SDK，而是 HTTPS
    # POST 到 cursor_relay_url，全程经 cursor_proxy_url 出网。留空=仍用本地 SDK。
    cursor_relay_url: str = ""
    # 中转服务的鉴权 token（与中转端 RELAY_TOKEN 一致，须为强随机串）。
    cursor_relay_token: str = ""
    # 是否校验中转服务的 TLS 证书。中转端用自签证书时设为 False。
    cursor_relay_verify_tls: bool = False

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    # 是否让 DeepSeek 请求走系统代理(HTTP_PROXY/HTTPS_PROXY)。
    # 默认 False=绕过代理直连（公司代理常拦截 api.deepseek.com 返回 403）。
    deepseek_use_proxy: bool = False

    # 存储
    data_dir: str = "./data"

    # 抓取节奏
    scrape_min_delay: float = 2.0
    scrape_max_delay: float = 5.0
    max_jobs_per_run: int = 20
    scrape_headless: bool = True
    # 浏览器内核渠道：chrome=用系统安装的正版 Chrome（更难被反爬识别），
    # msedge=系统 Edge，空=Playwright 自带 Chromium。启动失败会自动回退。
    browser_channel: str = "chrome"

    # 抓取/登录用浏览器的出网代理。用于解决招聘网站按"出口 IP 所在地区"的封锁：
    # 当本机出口在境外（如公司网芬兰出口）被 BOSS/智联判定"地区不支持"时，配一个
    # 中国出口的 HTTP 代理（形如 http://ip:port），让浏览器经它出网即可拿到中国 IP。
    # 留空=浏览器直连，不走代理。注意：HTTPS 经代理是 CONNECT 隧道、端到端加密，
    # 代理看不到登录凭据内容，但公网代理稳定性自负，建议尽量用自己可控的。
    # 可填多个（逗号分隔）做故障转移轮转：按顺序探测，用第一个连得通的；当前代理
    # 失效时下一次抓取会自动换到下一个可用代理（见 browser.pick_working_proxy）。
    browser_proxy_url: str = ""

    # 直接复用你真实 Chrome 的登录态与信誉（过反爬成功率最高）。
    # 启用后抓取/登录都用你本机 Chrome 的用户数据目录，运行时必须先完全关闭 Chrome。
    use_real_chrome_profile: bool = False
    # 留空则自动用 Windows 默认路径 %LOCALAPPDATA%\Google\Chrome\User Data
    chrome_user_data_dir: str = ""
    # 使用哪个 Chrome 配置（Default / "Profile 1" 等）
    chrome_profile_dir: str = "Default"

    # 托管 debug Chrome（推荐）：开启后程序会在需要时自动检测/启动一个带调试端口
    # 的真实 Chrome（独立数据目录 data/chrome-debug），所有站点共用、各自登录、
    # 互不冲突；抓取用你真人登录好的会话绕过反爬。无需手动跑 start_chrome_debug.ps1。
    use_debug_chrome: bool = True
    chrome_debug_port: int = 9222
    # 把调试口经一个本机 TCP 转发暴露到局域网，供你从自己电脑直接打开
    # http://<本机IP>:<该端口> 看页面 / 登录 / 过人机验证（Chrome 调试口本身只肯绑
    # 127.0.0.1，这里用纯 Python L4 转发，Chrome 接受 IP 形式的 Host 头）。0=不暴露。
    chrome_debug_view_port: int = 9223

    # 高级：手动指定已运行的 Chrome 调试端点（覆盖上面的托管逻辑）。一般留空即可。
    cdp_endpoint: str = ""
    # 是否进入每个职位详情页抓取完整 JD/技能标签（更慢但信息更全）
    scrape_fetch_detail: bool = True
    # 详情页抓取节流（智联有 EdgeOne 反爬，必须极慢以免被限流）
    scrape_detail_max: int = 5  # 单次最多抓几条详情
    scrape_detail_min_delay: float = 30.0  # 每条之间最小间隔(秒)
    scrape_detail_max_delay: float = 60.0  # 每条之间最大间隔(秒)
    scrape_detail_block_giveup: int = 2  # 连续被拦多少条就熔断停手

    # 列表页抓取的组合间隔（秒，随机区间）：拉长可降低单位时间请求量，
    # 减少代理/站点因短时高频被封禁。0 表示不等待（背靠背抓）。
    scrape_list_min_delay: float = 5.0
    scrape_list_max_delay: float = 12.0

    # 会话心跳：定时给已登录站点发轻量请求刷新 cookie，尽量延长登录态、
    # 减少重新登录/过验证的次数。用纯 HTTP 请求（不渲染页面），反爬足迹最小。
    heartbeat_enabled: bool = True
    heartbeat_minutes: int = 20

    # AI 分析并发与单次上限（每条职位起子进程调 LLM，约 20 秒/条；
    # 并发可显著提速，但过高会吃 CPU/可能触发模型限流，3 较稳妥）。
    analyze_concurrency: int = 3
    analyze_max: int = 50
    analyze_after_crawl: bool = False

    # 求职者画像（用于 AI 匹配分析）
    candidate_profile: str = ""

    # 对外访问地址，用于生成邮件里的"管理订阅/退订"链接
    base_url: str = "http://127.0.0.1:8000"

    # 鉴权：用户登录用签名 Cookie 会话；管理员用单一口令进入后台。
    # secret_key 用于会话签名（务必在 .env 改成随机长串）；admin_password 为后台口令。
    secret_key: str = "change-me-please-set-a-random-secret-in-env"
    admin_password: str = "admin"

    # 订阅发送前提前多少分钟开始预抓取（给反爬失败的组合留出人工重跑时间）。
    crawl_lead_minutes: int = 60

    # 全局停用的站点（逗号分隔，Site 枚举值，如 linkedin）。停用后不参与
    # 抓取组合/基础预热，并从订阅站点选项中隐藏。代码保留，改回即恢复。
    disabled_sites: str = "linkedin"

    # 夜间基础预热：按六大类全角色 × 热门城市，只抓“列表页”灌入中央池，
    # 让新用户订阅后“今日匹配”立刻有内容。完整 JD/AI 分析留给按需点击或发送阶段。
    baseline_warmup_enabled: bool = True
    baseline_warmup_hour: int = 3        # 触发时刻（Asia/Shanghai）
    baseline_warmup_minute: int = 0
    # 预热覆盖的热门城市（逗号分隔，可在 .env 改）。
    baseline_cities: str = "北京,上海,深圳,广州,杭州,成都,青岛,济南"

    # 邮件发送（SMTP）。服务器与凭据等敏感信息放 .env；
    # 收件人列表、是否含分析等可在网页里配置并入库。
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    # 加密方式：ssl（465，推荐）/ starttls（587）/ none
    smtp_security: str = "ssl"
    # 发件人显示地址，留空则用 smtp_user
    smtp_from: str = ""
    smtp_from_name: str = "Job Hunter"
    # SMTP 出网代理（HTTP CONNECT 隧道）。本机直连 SMTP 不可达时用它转发；
    # 留空则自动复用 browser_proxy_url。形如 http://host:port
    smtp_proxy_url: str = ""

    @property
    def browser_proxy_list(self) -> list[str]:
        """浏览器出网代理候选列表（逗号分隔），按顺序做故障转移轮转。"""
        return [p.strip() for p in (self.browser_proxy_url or "").split(",") if p.strip()]

    @property
    def disabled_sites_set(self) -> set[str]:
        return {s.strip().lower() for s in (self.disabled_sites or "").split(",") if s.strip()}

    @property
    def baseline_cities_list(self) -> list[str]:
        return [c.strip() for c in (self.baseline_cities or "").split(",") if c.strip()]

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def browser_profile_dir(self) -> Path:
        """Playwright 持久化上下文目录，按站点再分子目录。"""
        p = self.data_path / "browser_profiles"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "jobhunter.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
