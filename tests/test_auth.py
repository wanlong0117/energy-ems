"""auth.py 验证：固定密码、会话 cookie、守卫与登出失效。"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException, Request, Response

import auth
import db


def _request(cookie: str | None = None, path: str = "/") -> Request:
    headers = [] if cookie is None else [(b"cookie", cookie.encode())]
    return Request({
        "type": "http", "method": "GET", "path": path, "headers": headers,
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
    assert json.loads(response.body) == {"ok": True, "must_change_password": True}
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
    # 仍用默认密码：业务路径必须 403，只有改密/登出/会话查询放行
    with pytest.raises(HTTPException) as gate:
        auth.require_auth(_request(f"ems_session={token}"))
    assert gate.value.status_code == 403
    assert auth.require_auth(_request(f"ems_session={token}", path="/api/password")) is True

    # 改密后可正常访问业务路径
    assert auth.change_password("ems2026", "NewPass2026").status_code == 200
    relogin = auth.login("NewPass2026", Response())
    assert auth.require_auth(_request(f"ems_session={_cookie_value(relogin)}")) is True


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


# ---------- 失败计数的空闲回收（v0.1.10 / 复审 R-02） ----------


def test_sparse_login_failures_are_reclaimed_after_idle(isolated_db):
    """每个来源只错 1~4 次的零散记录，也必须在空闲窗口后被回收。

    回归 v0.1.9 的遗漏：_prune_sessions 原来只清「until 过期 **且** count<=0」。
    内网扫描器或写错的脚本每个 IP 只试 1~4 次，永远到不了 5 次锁定线，于是
    count>0 且 until==0，一条都清不掉——扫一个 /24 段就是 256 条永久驻留，
    扫十遍就是两千多条，而且没有任何日志提示。
    """
    auth._fail_state.clear()
    auth._last_prune = 0.0
    for index in range(3):
        auth._record_login_failure(f"10.1.0.{index}")  # 各错 1 次，都没到锁定线

    for state in auth._fail_state.values():
        assert state["until"] == 0.0 and state["count"] == 1.0, "前提：这些记录没被锁定"
        state["last_at"] = time.time() - auth.LOGIN_FAIL_IDLE_SEC - 1

    auth._prune_sessions(time.time())
    assert auth._fail_state == {}, "零散失败记录必须被回收，否则每扫一遍网段就涨一份内存"


def test_prune_keeps_fresh_and_locked_records(isolated_db):
    """回收不能误伤：刚失败的、以及仍在锁定期的记录都必须留着。

    否则「改个源 IP 重来」或「等 60 秒锁一到期计数就没了」就绕开了阈值——阈值本身
    成了摆设，比内存泄漏更糟。
    """
    auth._fail_state.clear()
    auth._last_prune = 0.0
    for _ in range(auth.LOGIN_FAIL_LIMIT):
        auth._record_login_failure("10.2.0.1")  # 达阈值 → 进入锁定
    auth._record_login_failure("10.2.0.2")      # 刚错一次

    now = time.time()
    auth._prune_sessions(now)
    assert "10.2.0.1" in auth._fail_state, "仍在锁定期，不能回收"
    assert "10.2.0.2" in auth._fail_state, "刚发生的失败要保留计数"

    # 锁定刚过期也不能立刻丢：most-recent 取 until，已远超空闲窗口才算真闲置
    auth._last_prune = 0.0
    auth._prune_sessions(now + auth.LOGIN_LOCK_SEC + 1)
    assert "10.2.0.1" in auth._fail_state, "解封后立刻回收等于把锁定变成 60 秒一次的重置"

    # 真闲置（锁定到期后再过一整个空闲窗口）才回收
    auth._last_prune = 0.0
    auth._prune_sessions(now + auth.LOGIN_LOCK_SEC + auth.LOGIN_FAIL_IDLE_SEC + 1)
    assert "10.2.0.1" not in auth._fail_state
