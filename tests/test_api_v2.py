"""v2 新增 API 鉴权与统一响应格式：所有新端点未登录 401、{ok,...}、非法参数 400/422。"""
from __future__ import annotations

import httpx
import pytest

import main

from helpers import run


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test")


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/weekly", None),
    ("GET", "/api/monthly", None),
    ("GET", "/api/mode", None),
    ("POST", "/api/mode/switch", {"data_mode": "live"}),
    ("GET", "/api/report/export", None),
    ("POST", "/api/manual", {"point_id": 1, "ts": 1, "value": 1.0, "operator": "x"}),
    ("GET", "/api/manual", None),
])
def test_new_endpoints_require_auth(isolated_db, method, path, body):
    """Arrange: 匿名客户端。Act: 请求 7 个新增端点。Assert: 一律 401 + {ok:false,error}。"""
    async def scenario():
        async with _client() as client:
            resp = await client.request(method, path, json=body)
            assert resp.status_code == 401, f"{method} {path}: {resp.text}"
            payload = resp.json()
            assert payload["ok"] is False
            assert isinstance(payload.get("error"), str) and payload["error"]

    run(scenario())


def test_mode_endpoints_return_unified_envelope(isolated_db):
    """Arrange: 登录。Act: GET /api/mode 与 POST /api/mode/switch。Assert: {ok,data}，非法模式 400/422。"""
    async def scenario():
        async with _client() as client:
            assert (await client.post(
                "/api/login", json={"password": "ems2026"})).status_code == 200

            mode = await client.get("/api/mode")
            assert mode.status_code == 200 and mode.json()["ok"] is True
            assert mode.json()["data"]["data_mode"] == "simulate"
            assert isinstance(mode.json()["data"]["simulate_interval_sec"], int)

            switched = await client.post("/api/mode/switch", json={"data_mode": "live"})
            assert switched.status_code == 200
            assert switched.json()["data"] == {"data_mode": "live", "restart_required": True}
            assert (await client.get("/api/mode")).json()["data"]["data_mode"] == "live"

            bad = await client.post("/api/mode/switch", json={"data_mode": "bogus"})
            assert bad.status_code in (400, 422)
            assert bad.json()["ok"] is False and "error" in bad.json()

    run(scenario())


@pytest.mark.parametrize("path", [
    "/api/weekly?meter_id=abc",
    "/api/monthly?from=notint",
    "/api/report/export?kind=daily&fmt=pdf&to=bad",
    "/api/manual?limit=abc",
])
def test_new_endpoints_reject_bad_query_types(isolated_db, path):
    """Arrange: 登录 + 非法查询参数类型。Act: 请求。Assert: 400/422 且统一错误格式。"""
    async def scenario():
        async with _client() as client:
            await client.post("/api/login", json={"password": "ems2026"})
            resp = await client.get(path)
            assert resp.status_code in (400, 422), f"{path}: {resp.text}"
            assert resp.json()["ok"] is False and "error" in resp.json()

    run(scenario())


def test_weekly_monthly_empty_range_returns_ok_empty_array(isolated_db):
    """Arrange: 空库。Act: 登录查周/月。Assert: {ok:true,data:[]} 而非报错。"""
    async def scenario():
        async with _client() as client:
            await client.post("/api/login", json={"password": "ems2026"})
            for path in ("/api/weekly", "/api/monthly"):
                resp = await client.get(path)
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"ok": True, "data": []}

    run(scenario())
