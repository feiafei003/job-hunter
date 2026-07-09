# 后续扩展计划（Backlog）

记录已规划但暂未实现的功能。当前已实现：抓取（智联/领英/BOSS/猎聘/前程无忧）、CDP 接管真实
Chrome、订阅系统、预抓取/重跑失败/到点发送三段式批次、订阅自助管理（管理链接 + 邮箱找回）。

---

## 1. 账号体系（邮箱 + 密码）

**目标**：订阅者注册/登录后，在一个"我的订阅"面板里统一查看与管理自己的所有订阅，
不再依赖一条订阅一个管理链接。

**为什么暂缓**：现状用 magic-link（管理链接）+ 邮箱找回已够用；账号体系改动较大。

**实现要点**：
- `models.py`：新增 `User` 表（`email` 唯一、`password_hash`、`created_at`）；
  `Subscription` 增加 `user_id`（可空外键）。
- `db.py`：迁移补列 `("subscription", "user_id", "INTEGER")`。
- 新增 `app/auth.py`：用标准库 `hashlib.pbkdf2_hmac` + `secrets` 做密码哈希/校验
  （格式如 `pbkdf2_sha256$iter$salt$hash`），无需额外依赖。
- 会话：`starlette.middleware.sessions.SessionMiddleware`（**需新增依赖 `itsdangerous`**，
  当前 venv 未安装）；`config.py`/`.env` 增加 `SESSION_SECRET`。
- 接口：`POST /auth/register`、`/auth/login`、`/auth/logout`、`GET /auth/me`、
  `POST /auth/change-password`；登录/注册时把**同邮箱、user_id 为空的历史订阅自动关联**到该账号。
- 受保护接口：`GET /my/subscriptions`（按 user_id 列出）、`POST /my/subscriptions`（创建并挂到账号）；
  编辑/退订/试发可复用现有 token 接口。
- 前端：`subscribe.html` 顶部按登录态切换"登录/注册"卡片或"我的订阅"列表；或单开 `account.html`。
- 安全可选项：邮箱验证（防止用他人邮箱注册抢占订阅）；忘记密码（仍走邮件重置链接）。

**替代方案**：邮箱免密登录（magic-link 登录）——输入邮箱收登录链接，点击即建立会话，
免密码、复用邮件、改动更小。若日后觉得密码账号太重可改走这条。

---

## 2. "我的订阅"页直接显示职位（不只邮件）

**目标**：在订阅管理页（或登录后的面板）里，直接看到每条订阅**匹配到的职位列表 +
完整 JD + AI 匹配建议**，不必等邮件。

**为什么暂缓**：当前选择 settings-only，订阅页只管理设置。

**实现要点**：
- 接口：`GET /api/subscriptions/{token}/jobs`（或 `/my/subscriptions/{id}/jobs`）：
  用 `matching.match_job_ids(sub)` 取匹配职位，关联 `Analysis`（按 `profile_hash` 缓存）返回
  `{job, analysis}` 列表；可选 `?include_delivered=true` 把已推送过的也带上。
- 前端：在 manage 模式下加一个"匹配到的职位"区块，复用 `index.html` 的职位卡片样式
  （标题/公司/薪资/标签/JD 折叠/匹配度与建议）。
- 注意：实时计算可能触发 AI 分析耗时；可只展示已缓存的分析，未分析的按需触发或留空。

---

## 3. Linux 服务器部署（2 核 2G）

**目标**：把服务从 Windows 本机挪到 Linux 服务器常驻运行。

**为什么暂缓**：先在 Windows 打磨功能。

**实现要点**：
- 免登录站点（51job/猎聘）可 headless 跑；登录态站点（智联/BOSS/领英）在无人值守服务器上
  难维持，考虑只在服务器跑免登录站点，或保留一台有人值守的机器做登录态抓取。
- `Dockerfile`（playwright python 基镜像）+ `docker-compose.yml` + `.env.example`；
  Chromium 调优：`--disable-dev-shm-usage`、`--no-sandbox`、`--disable-gpu`、屏蔽图片/字体；
  加 swap 缓解 2G 内存。
- 部署细节：时区 `TZ`（影响 cron 时段）、`BASE_URL`（管理链接/邮件）、nginx + HTTPS 反代。

---

## 4. 其他小项

- **发送阶段职位时间窗过滤**：`run_subscription_digest` 目前对整个中央池做匹配；可加"只用最近
  一次批次/最近 N 天抓到的职位"选项，避免把历史老职位再翻出来。
- **失败批次告警**：预抓取后若有 blocked/failed 组合，给管理员发一封提醒邮件（含失败原因），
  而不仅在后台面板显示。
- **订阅页站点登录态对用户透明**：已确认由服务端维护，订阅用户无需感知（已实现，留作回归注意点）。
