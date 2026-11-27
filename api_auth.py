"""认证路由：POST /api/login、POST /api/logout、GET /api/session。

除 login/session 外，其它 API 路由均在各自 router 上挂 require_auth 依赖。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

import auth
from auth import require_auth
from schemas import LoginIn, PasswordIn, ok

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response) -> JSONResponse:
    """固定密码登录，成功写 ems_session cookie；连续失败会被临时锁定。"""
    return auth.login(body.password, response, auth.client_from_request(request))


@router.post("/logout", dependencies=[Depends(require_auth)])
def logout(request: Request) -> JSONResponse:
    """登出：清除内存会话与 cookie（需登录）。"""
    return auth.logout(request)


@router.post("/password", dependencies=[Depends(require_auth)])
def change_password(body: PasswordIn) -> JSONResponse:
    """修改登录密码（需登录）；成功后所有会话失效。"""
    return auth.change_password(body.old_password, body.new_password)


@router.get("/session")
def session(request: Request) -> dict:
    """会话状态查询（无需登录，始终 ok:true）。

    配置损坏时不抛 503：前端仍要知道「服务活着但登录被禁用」，
    否则只会看到一个语焉不详的失败提示。
    """
    try:
        return ok({
            "authenticated": auth.current_user(request),
            "must_change_password": auth.must_change_password(),
        })
    except auth.ConfigError:
        return ok({
            "authenticated": False,
            "must_change_password": False,
            "config_error": True,
        })


__all__ = ["router", "require_auth"]
