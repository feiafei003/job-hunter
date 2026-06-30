import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from .logging_config import setup_logging
from .scheduler import scheduler_service

WEB_DIR = Path(__file__).parent / "web"
_settings = get_settings()


def _app_role() -> str:
    """部署角色：all=单端口都开(默认)；user=仅用户端；admin=仅管理端。

    用法（两端口隔离）：
        APP_ROLE=user  PORT=8000 -> 对外的用户门户
        APP_ROLE=admin PORT=8001 -> 仅内网/Tailscale 的管理后台
    """
    return (os.environ.get("APP_ROLE", "all") or "all").strip().lower()


# 用户端口需要屏蔽的「管理端」路径：管理页 + 建立管理员会话的登录接口。
# 管理类 API 本身已用 require_admin 鉴权，封死 /api/admin/login 后，公网端口
# 无法建立管理员会话，等同于管理功能在用户端口不可达。
_ADMIN_BLOCK_PREFIXES = ("/admin", "/api/admin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    init_db()
    scheduler_service.start()
    try:
        yield
    finally:
        scheduler_service.shutdown()


app = FastAPI(title="Job Hunter Agent", lifespan=lifespan)

# 会话中间件：用签名 Cookie 存登录态（用户/管理员）。
app.add_middleware(SessionMiddleware, secret_key=_settings.secret_key)


@app.middleware("http")
async def _role_guard(request, call_next):
    """按 APP_ROLE 做端口级隔离。

    - user 端口：屏蔽管理页与管理员登录接口（404），管理功能彻底不可达；
    - admin 端口：屏蔽用户门户页（让 / 跳到 /admin），减少暴露面。
    """
    role = _app_role()
    path = request.url.path
    if role == "user" and path.startswith(_ADMIN_BLOCK_PREFIXES):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return await call_next(request)


# 注册 API 路由
from .api.routes import router as api_router  # noqa: E402

app.include_router(api_router, prefix="/api")


@app.get("/")
def index():
    """用户门户：登录/注册 + 我的订阅/自画像/今日职位。

    admin 端口下重定向到 /admin。
    """
    if _app_role() == "admin":
        return RedirectResponse(url="/admin")
    return FileResponse(WEB_DIR / "portal.html")


@app.get("/admin")
def admin_page():
    """管理后台：口令进入，站点预登录/抓取/配置。user 端口下不可达。"""
    if _app_role() == "user":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html")


@app.get("/subscribe")
def subscribe_page(token: str = ""):
    """订阅管理/退订页（邮件里的管理链接 /subscribe?token=xxx）。

    匿名自助创建入口已下线：不带 token 一律重定向到主门户（登录/注册）。
    带 token 时仍提供既有订阅的管理与退订（邮件链接必须可用）。
    """
    if not (token or "").strip():
        return RedirectResponse(url="/")
    return FileResponse(WEB_DIR / "subscribe.html")


@app.get("/browser")
def browser_view_page():
    """远程看屏查看器：把托管 Chrome 画面投到网页，可点击/扫码登录。

    属于管理功能，user 端口下不可达。
    """
    if _app_role() == "user":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return FileResponse(WEB_DIR / "browser.html")


def run() -> None:
    import os

    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
