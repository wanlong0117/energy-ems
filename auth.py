"""极简认证：一个固定密码 + 内存 session cookie。单厂单用户场景，不做角色。

在原骨架的 login/logout/current_user 基础上，新增 FastAPI 依赖 require_auth，
供除 /api/login、/api/session 之外的所有 /api/* 路由复用。
未登录统一抛 HTTPException(401)，由 main.py 的异常处理器转成 {ok:false,error}。

v2：CONFIG_PATH 走 settings.app_dir()；load_config() 为旧配置（仅 password）
    补全 data_mode / simulate_interval_sec / simulate_rollup_sec，且不覆盖用户已有键。
"""
from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from db import log_event
from settings import (
    DATA_MODE_DEFAULT,
    DATA_MODES,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SEC,
    SIMULATE_INTERVAL_SEC,
    SIMULATE_ROLLUP_SEC,
    app_dir,
)

CONFIG_PATH = app_dir() / "config.json"

# token -> 过期时间戳
_sessions: dict[str, float] = {}
SESSION_TTL: int = SESSION_TTL_SEC  # 兼容原有命名，7 天
COOKIE_NAME: str = SESSION_COOKIE_NAME
DEFAULT_PASSWORD: str = "ems2026"
PASSWORD_ENV: str = "EMS_PASSWORD"          # 首次生成配置时的初始密码（仅首次生效）
PASSWORD_MIN_LEN: int = 8

CONFIG_DEFAULTS: dict[str, Any] = {
    "password": DEFAULT_PASSWORD,
    "data_mode": DATA_MODE_DEFAULT,
    "simulate_interval_sec": SIMULATE_INTERVAL_SEC,
    "simulate_rollup_sec": SIMULATE_ROLLUP_SEC,
}


def _default_config() -> dict[str, Any]:
    """返回一份全新的默认配置副本；EMS_PASSWORD 仅在首次生成配置时生效。"""
    config: dict[str, Any] = dict(CONFIG_DEFAULTS)
    env_password: str = os.environ.get(PASSWORD_ENV, "").strip()
    if env_password:
        config["password"] = env_password
    return config


def load_config() -> dict[str, Any]:
    """读取配置并用默认值补全缺失键；不覆盖用户已有键。

    旧 config.json 仅含 password 时会自动补 data_mode/simulate_* 并回写，
    这样 /api/mode、lifespan 与模拟器都能拿到一致的运行时配置。
    """
    config: dict[str, Any] = _default_config()
    if CONFIG_PATH.exists():
        try:
            loaded: Any = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            # 用户已有键优先；仅对缺失键填默认值
            for key, value in loaded.items():
                if value is not None:
                    config[key] = value
    config["data_mode"] = str(config.get("data_mode", DATA_MODE_DEFAULT)).lower()
    if config["data_mode"] not in DATA_MODES:
        config["data_mode"] = DATA_MODE_DEFAULT
    for key in ("simulate_interval_sec", "simulate_rollup_sec"):
        try:
            config[key] = max(1, int(config[key]))
        except (TypeError, ValueError):
            config[key] = int(CONFIG_DEFAULTS[key])

    # 仅在文件缺失或缺键时回写，保持用户原有键不变
    need_write: bool = not CONFIG_PATH.exists()
    if not need_write:
        try:
            existing: Any = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            need_write = not isinstance(existing, dict) or any(
                key not in existing for key in CONFIG_DEFAULTS
            )
        except (OSError, ValueError):
            need_write = True
    if need_write:
        save_config(config)
    return config


def save_config(config: dict[str, Any]) -> None:
    """整体写回配置（调用方负责合并已有键）。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def set_data_mode(mode: str) -> str:
    """切换 data_mode：写配置并返回规范化后的模式值（重启后生效）。"""
    normalized: str = str(mode).lower()
    if normalized not in DATA_MODES:
        raise ValueError(f"data_mode 仅支持 {', '.join(DATA_MODES)}")
    config: dict[str, Any] = load_config()
    config["data_mode"] = normalized
    save_config(config)
    log_event("info", "user", "MODE_SWITCH", f"data_mode 切换为 {normalized}（待重启生效）")
    return normalized


def load_password() -> str:
    """读取 config.json 中的固定密码；首次运行生成默认配置。"""
    return str(load_config().get("password", DEFAULT_PASSWORD))


def must_change_password() -> bool:
    """是否仍在用默认密码：是则前端强制引导改密。"""
    config: dict[str, Any] = load_config()
    if bool(config.get("password_changed")):
        return False
    return str(config.get("password", DEFAULT_PASSWORD)) == DEFAULT_PASSWORD


def change_password(old_password: str, new_password: str) -> JSONResponse:
    """校验原密码并设置新密码；成功后清空全部会话，强制重新登录。"""
    if old_password != load_password():
        log_event("warn", "user", "PASSWORD_CHANGE_FAIL", "改密失败：原密码错误")
        return JSONResponse({"ok": False, "error": "原密码错误"}, status_code=401)
    candidate: str = str(new_password or "")
    if len(candidate) < PASSWORD_MIN_LEN:
        return JSONResponse(
            {"ok": False, "error": f"新密码至少 {PASSWORD_MIN_LEN} 位"}, status_code=400
        )
    if candidate == DEFAULT_PASSWORD:
        return JSONResponse({"ok": False, "error": "新密码不能与默认密码相同"}, status_code=400)
    config: dict[str, Any] = load_config()
    config["password"] = candidate
    config["password_changed"] = True
    save_config(config)
    _sessions.clear()
    log_event("warn", "user", "PASSWORD_CHANGED", "管理员密码已修改，所有会话失效")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


def login(password: str, response: Response) -> JSONResponse:
    """校验固定密码，成功则签发 token 并写入 httponly cookie。"""
    if password != load_password():
        log_event("warn", "api", "LOGIN_FAIL", "登录密码错误")
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    resp = JSONResponse({"ok": True, "must_change_password": must_change_password()})
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    log_event("info", "api", "LOGIN", "登录成功")
    return resp


def logout(request: Request) -> JSONResponse:
    """清除内存会话与浏览器 cookie。"""
    token: str | None = request.cookies.get(COOKIE_NAME)
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


def current_user(request: Request) -> bool:
    """判断请求是否携带有效且未过期的会话 token。"""
    token: str | None = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    exp: float | None = _sessions.get(token)
    if not exp or exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def require_auth(request: Request) -> bool:
    """FastAPI 依赖守卫：未登录返回 401（统一响应由 main.py 异常处理器格式化）。"""
    if not current_user(request):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return True
