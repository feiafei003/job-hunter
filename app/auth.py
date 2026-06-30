"""鉴权工具：密码哈希 + 会话依赖。

- 密码哈希用标准库 pbkdf2_hmac（不引第三方依赖）。
- 登录态存在 Starlette SessionMiddleware 的签名 Cookie 里：
  - 用户：session["user_id"]
  - 管理员：session["is_admin"] = True
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlmodel import select

from .db import session_scope
from .models import User

_ALGO = "sha256"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """生成 'pbkdf2$算法$迭代$盐$摘要' 格式的哈希串。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        _ALGO, password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    )
    return f"pbkdf2${_ALGO}${_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与存储哈希是否匹配（防时序攻击用 compare_digest）。"""
    try:
        scheme, algo, iters, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac(
            algo, password.encode("utf-8"), bytes.fromhex(salt), int(iters)
        )
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:  # noqa: BLE001
        return False


def current_user(request: Request) -> Optional[User]:
    """从会话取当前登录用户；未登录返回 None。"""
    uid = request.session.get("user_id")
    if not uid:
        return None
    with session_scope() as session:
        return session.get(User, uid)


def require_user(request: Request) -> User:
    """依赖：要求已登录用户，否则 401。"""
    user = current_user(request)
    if user is None:
        raise HTTPException(401, "请先登录")
    return user


def require_admin(request: Request) -> None:
    """依赖：要求管理员会话，否则 401。"""
    if not request.session.get("is_admin"):
        raise HTTPException(401, "需要管理员权限")


# 便于在路由里以 Depends 引用
CurrentUser = Depends(require_user)
AdminOnly = Depends(require_admin)
