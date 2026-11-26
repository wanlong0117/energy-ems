"""密码策略：默认密码强制改密、改密后会话失效、环境变量初始密码。"""
from __future__ import annotations

import json

import httpx
from fastapi import Response

import auth
import main
from helpers import run


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


def test_default_password_flags_must_change(isolated_db):
    assert auth.must_change_password() is True
    response = auth.login("ems2026", Response())
    assert json.loads(response.body)["must_change_password"] is True


def test_change_password_via_api_invalidates_sessions(isolated_db):
    async def scenario():
        async with _client() as client:
            login = await client.post("/api/login", json={"password": "ems2026"})
            assert login.status_code == 200
            assert login.json()["must_change_password"] is True

            changed = await client.post(
                "/api/password",
                json={"old_password": "ems2026", "new_password": "NewPass2026"},
            )
            assert changed.status_code == 200 and changed.json()["ok"] is True

            session = (await client.get("/api/session")).json()["data"]
            assert session["authenticated"] is False
            assert session["must_change_password"] is False

            assert (await client.post("/api/login", json={"password": "ems2026"})).status_code == 401
            assert (await client.post("/api/login", json={"password": "NewPass2026"})).status_code == 200
    run(scenario())


def test_change_password_rejects_bad_input(isolated_db):
    assert auth.change_password("wrong", "NewPass2026").status_code == 401
    assert auth.change_password("ems2026", "short").status_code == 400
    assert auth.change_password("ems2026", "ems2026").status_code == 400


def test_env_password_used_on_first_boot(isolated_db, monkeypatch):
    auth.CONFIG_PATH.unlink(missing_ok=True)
    monkeypatch.setenv("EMS_PASSWORD", "EnvPass2026")
    assert auth.verify_login_password("EnvPass2026") is True
    assert auth.verify_login_password("ems2026") is False
    # 环境变量指定的初始密码不是公开默认值，不应强制改密
    assert auth.must_change_password() is False
