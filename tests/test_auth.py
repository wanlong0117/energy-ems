"""auth.py 验证：固定密码、会话 cookie、守卫与登出失效。"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException, Request, Response

import auth
import db


def _request(cookie: str | None = None) -> Request:
    headers = [] if cookie is None else [(b"cookie", cookie.encode())]
    return Request({
        "type": "http", "method": "GET", "path": "/", "headers": headers,
        "query_string": b"", "client": ("127.0.0.1", 12345),
    })


def _cookie_value(response) -> str:
    raw = response.headers["set-cookie"]
    return raw.split(";")[0].split("=", 1)[1]


def test_load_password_generates_config_in_temp_dir(isolated_db):
    assert auth.load_password() == "ems2026"
    assert auth.CONFIG_PATH.exists()
    assert json.loads(auth.CONFIG_PATH.read_text(encoding="utf-8"))["password"] == "ems2026"


def test_login_success_sets_httponly_session_cookie(isolated_db):
    # Arrange / Act
    response = auth.login("ems2026", Response())

    # Assert
    assert response.status_code == 200
    assert json.loads(response.body) == {"ok": True}
    raw = response.headers["set-cookie"].lower()
    assert "ems_session=" in raw
    assert "httponly" in raw
    assert "samesite=lax" in raw
    token = _cookie_value(response)
    assert token in auth._sessions
    assert auth.current_user(_request(f"ems_session={token}")) is True


def test_login_wrong_password_returns_401_and_logs_event(isolated_db):
    response = auth.login("wrong-password", Response())
    assert response.status_code == 401
    assert json.loads(response.body) == {"ok": False, "error": "密码错误"}
    assert auth._sessions == {}
    rows = db.query("SELECT code FROM event_log WHERE code='LOGIN_FAIL'")
    assert len(rows) == 1


def test_require_auth_guards_unauthenticated_request(isolated_db):
    assert auth.current_user(_request()) is False
    with pytest.raises(HTTPException) as exc:
        auth.require_auth(_request())
    assert exc.value.status_code == 401

    response = auth.login("ems2026", Response())
    token = _cookie_value(response)
    assert auth.require_auth(_request(f"ems_session={token}")) is True


def test_expired_session_is_rejected_and_removed(isolated_db):
    response = auth.login("ems2026", Response())
    token = _cookie_value(response)
    auth._sessions[token] = time.time() - 1
    assert auth.current_user(_request(f"ems_session={token}")) is False
    assert token not in auth._sessions


def test_logout_invalidates_session(isolated_db):
    response = auth.login("ems2026", Response())
    token = _cookie_value(response)
    assert auth.current_user(_request(f"ems_session={token}")) is True

    logout_response = auth.logout(_request(f"ems_session={token}"))
    assert json.loads(logout_response.body) == {"ok": True}
    assert "ems_session=" in logout_response.headers["set-cookie"]
    assert token not in auth._sessions
    assert auth.current_user(_request(f"ems_session={token}")) is False
