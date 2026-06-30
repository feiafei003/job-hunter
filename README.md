# Job Hunter Agent

本地常驻的智能求职助手：用持久化浏览器会话定时抓取招聘网站（智联招聘、领英）上可配置的职位，调用 DeepSeek 做匹配度分析并给出求职建议，结果在本地网页查看。

## 功能

- **可配置职位搜索**：网页里增删搜索任务（站点 / 关键词如 C++ / 城市 / 频率），后端按周期自动执行。
- **登录态保持**：基于 Playwright 持久化上下文，首次手动登录一次后，登录态保存在本地，后续无需重复登录，降低反爬风控。
- **定时抓取 + 去重**：APScheduler 周期调度，按 URL 指纹去重入库（SQLite）。
- **AI 分析建议**：DeepSeek 结合你的「求职画像」给出匹配度、亮点、风险与投递建议。
- **插件式架构**：新增站点（猎聘 / BOSS 直聘等）只需加一个 scraper 插件。

## 架构

```
浏览器(localhost) → FastAPI → SQLite
                       ├── APScheduler 定时器 → 抓取插件 → Playwright 持久化会话 → 智联/领英
                       └── DeepSeek 分析器
```

## 安装

需要 Python 3.10+。

```bash
cd job-hunter
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
# 安装 Playwright 浏览器内核（首次必须）
python -m playwright install chromium
```

## 配置

复制 `.env.example` 为 `.env`，填入你的 DeepSeek API key：

```bash
copy .env.example .env   # Windows
# cp .env.example .env    # macOS/Linux
```

关键项：

- `DEEPSEEK_API_KEY`：在 https://platform.deepseek.com 申请。
- `SCRAPE_MIN_DELAY` / `SCRAPE_MAX_DELAY`：动作间随机延迟（秒），越大越安全。
- `MAX_JOBS_PER_RUN`：单次最多抓取条数，建议保持较小以降低风控。
- `SCRAPE_HEADLESS`：抓取是否无头，默认 `true`；遇到风控可改 `false`。
- `CANDIDATE_PROFILE`：你的技能/经历画像（也可在网页里填）。

## 运行

```bash
python -m app.main
```

打开浏览器访问 http://127.0.0.1:8000

## 首次使用流程

1. 点击「登录智联 / 登录领英」，会弹出一个真实浏览器窗口，在里面手动登录（扫码 / 账号密码 / 验证码均可）。登录成功后窗口会在几分钟后自动关闭，登录态已保存。
2. 在「我的求职画像」里填写你的背景（如 `5 年 C++ 后端，熟悉 Linux 网络编程，期望 30K+`），保存。
3. 新建搜索任务（站点 + 关键词 + 城市 + 频率），添加。
4. 点「立即跑」可马上抓取并分析；之后系统会按设定频率自动执行。
5. 在右侧查看职位与 AI 建议，点匹配度/「查看建议」展开详情。

## 注意事项（重要）

- 智联、领英的服务条款通常**限制自动化抓取**。本工具定位为**个人本地、低频、自用**，请合理控制频率，自行承担合规风险。
- 站点页面结构会不定期改版，若抓不到数据，多半是选择器失效：
  - 智联：调整 `app/scrapers/zhilian.py` 中的 `_CARD_SELECTORS` 等选择器。
  - 领英：调整 `app/scrapers/linkedin.py` 中的对应选择器。
- 领英反爬最严格，必须先登录；若被重定向到登录页会提示重新登录。遇到风控时把 `SCRAPE_HEADLESS=false`、增大延迟、降低 `MAX_JOBS_PER_RUN`。
- 数据与浏览器会话默认存放在 `./data`（`browser_profiles/` 为登录态，`jobhunter.db` 为数据库），请勿提交到版本库。

## 扩展新站点

1. 在 `app/models.py` 的 `Site` 枚举里加上新站点。
2. 在 `app/scrapers/` 新建 `xxx.py`，继承 `BaseScraper`，设置 `site` 和 `login_url`，实现 `_search()`，并用 `@register` 装饰。
3. 在 `app/scrapers/__init__.py` 导入它即可。

## Linux 服务器部署（无桌面 / 容器云主机）

代码已经做了跨平台适配，Linux 服务器上完整可跑。和 Windows 的差异：

- **Chrome / Chromium 来源**：服务器一般没有真实 Chrome 可复用。程序按
  `系统 chromium → Playwright 自带 chromium` 顺序自动查找；强烈建议直接用
  Playwright 内置版（**不需要 sudo**）。
- **`USE_REAL_CHROME_PROFILE` 必须保持 `false`**：服务器没有真人登录过的
  Chrome 配置可复制。
- **`USE_DEBUG_CHROME=true` 仍是首选**：程序按需启动一个开了 9222 调试端口
  的 Chromium，所有抓取都共用这一个；遇到风控时你可以通过 SSH 隧道远程
  接管它。

### 1. 安装依赖

> **注意**：如果你之前在 Windows 创建过 `.venv` 并同步到了 Linux，目录结构是
> `Lib / Scripts / Include`（无 `bin`），**这种 venv 在 Linux 上不能用**，
> 必须先 `rm -rf .venv` 再重建。

Python 版本要求：

- **`LLM_PROVIDER=deepseek`**：Python ≥ 3.9 即可。
- **`LLM_PROVIDER=cursor`（默认）**：依赖 `cursor-sdk`，**需要 Python ≥ 3.10**。
  系统只有 3.9 时，`requirements.txt` 会自动跳过 `cursor-sdk`，此时要么改用
  deepseek 后端，要么用下方 `uv` 装一个用户级的新 Python（**不需要 sudo**）。

```bash
cd job-hunter
rm -rf .venv                       # 如有 Windows 风格的旧 venv 务必先删

# 系统已有 Python ≥ 3.10：直接用它建 venv
python3 -m venv .venv
source .venv/bin/activate

# 系统只有 3.9 且想用 cursor 后端：用 uv 装独立 Python（产物落在项目内，无需 sudo）
#   pip install uv
#   export UV_PYTHON_INSTALL_DIR="$PWD/.uv/python" UV_CACHE_DIR="$PWD/.uv/cache"
#   uv python install 3.12
#   uv venv --python 3.12 .venv && source .venv/bin/activate

pip install -U pip
pip install -r requirements.txt
# 下载 Chromium 内核（用户级，不需 sudo）。装到项目内 .ms-playwright，程序启动时
# 会自动设置 PLAYWRIGHT_BROWSERS_PATH 指向它，无需手动 export。
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.ms-playwright"
python -m playwright install chromium
python -m rebrowser_playwright install chromium  # 反爬补丁版
```

> 提示：浏览器内核也可以装到默认的 `~/.cache/ms-playwright`（省掉上面的
> `export`）；程序两种位置都会自动查找。装到项目内 `.ms-playwright` 的好处是
> 整个工程自包含、可整体打包迁移。

如系统较老缺 `dbus`、`fonts-noto-cjk` 等渲染依赖，必要时让运维补上；
绝大多数 RHEL 9 / Ubuntu 22 默认已具备。

### 2. 配置 `.env`

```ini
DATA_DIR=./data
USE_DEBUG_CHROME=true
CHROME_DEBUG_PORT=9222
USE_REAL_CHROME_PROFILE=false
SCRAPE_HEADLESS=true
BROWSER_CHANNEL=          # 留空，让 Playwright 用自己的 chromium
```

### 3. 启动

```bash
# 仅监听本机（推荐：通过 SSH 隧道访问）
python -m app.main

# 或直接对外暴露（仅在受信任的内网用，外网请加反向代理 + HTTPS + 鉴权）
HOST=0.0.0.0 PORT=8000 python -m app.main
```

### 4. 远程访问 / 风控介入

从你的笔记本或手机：

```bash
# 一条 SSH 同时转发 Web UI（8000）和 Chrome DevTools（9222）
ssh -L 8000:localhost:8000 -L 9222:localhost:9222 root@<server>
```

然后：

- 浏览器开 `http://localhost:8000` 进入 Job Hunter Web UI。
- 风控弹出时，浏览器开 `http://localhost:9222` 看到 Chrome DevTools，
  点对应标签 → "inspect" → 在 DevTools 里手动滑滑块 / 输验证码。
- 解完后程序会自动继续。

### 5. 后台常驻 / 自启动

先判断你的机器是「正常 VM」还是「受限容器」：

```bash
# 任一条命中 → 受限容器，systemd 用不了，走方案 B
touch /etc/systemd/system/.t 2>/dev/null && rm -f /etc/systemd/system/.t || echo "/etc 只读"
systemctl --user status >/dev/null 2>&1 || echo "无 user bus"
```

#### 方案 A：正常机器 —— systemd（推荐，真正开机自启）

项目已带好模板 `deploy/jobhunter.service`：

```bash
# 按需改 User / WorkingDirectory / ExecStart 路径后：
sudo cp deploy/jobhunter.service /etc/systemd/system/jobhunter.service
sudo systemctl daemon-reload
sudo systemctl enable --now jobhunter.service
systemctl status jobhunter.service
journalctl -u jobhunter -f          # 看日志
```

#### 方案 B：受限容器（/etc 只读、sudo 不可用、无 user bus）—— 守护脚本

这类环境装不了 systemd，也写不了 crontab。用项目自带的 `run_server.sh`，
纯用户态，进程崩溃会自动退避重启：

```bash
./run_server.sh start     # 后台启动（带自动重启循环）
./run_server.sh status    # 查看状态
./run_server.sh restart   # 重启
./run_server.sh stop      # 停止
./run_server.sh fg        # 前台运行（调试）

# 对外开放：
HOST=0.0.0.0 PORT=8000 ./run_server.sh start
```

> 局限：守护脚本能在**进程崩溃**后自动拉起，但**无法在机器/容器重启后**
> 自动运行（容器重启由其自身 entrypoint 决定）。容器重启后手动再跑一次
> `./run_server.sh start` 即可。

#### 方案 C：最简临时跑

```bash
nohup python -m app.main > server_out.log 2> server_err.log &
```

### 6. 手动跑调试 Chrome（可选，等价于 Windows 的 ps1）

如果想脱离主程序单独起一个调试 Chrome 自己玩：

```bash
./start_chrome_debug.sh           # 默认 headless + 9222
HEADLESS=0 ./start_chrome_debug.sh   # 有桌面环境时弹窗口
```

### 常见问题

| 现象 | 处理 |
|------|------|
| `未找到任何可用的 Chrome / Chromium` | `python -m playwright install chromium` |
| `--no-sandbox` 报权限错误 | 这是受限容器/root 跑的特征，代码已自动加该参数 |
| 远程打不开 9222 | 公司防火墙拦了，改走 SSH 隧道（强烈推荐） |
| 智联 / BOSS 抓详情被拦 | 调小 `MAX_JOBS_PER_RUN`、加大 `SCRAPE_DETAIL_*_DELAY` |
```
