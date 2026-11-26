"""v2 手动录入验证：单事务三表一致、重复覆盖审计、历史补录回填且保留 is_final、API 增查与鉴权。"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

import db
import main
from aggregation import aggregator
from manual import manual_service

from helpers import (
    add_reading, create_device, create_meter, create_node, create_point,
    create_tariff, fill_hourly, link_point, run,
)

# 补录窗口只允许最近 90 天，取「3 天前的 UTC 零点」既满足历史回填又把时钟依赖降到最低
DAY = (int(time.time()) // 86400) * 86400 - 3 * 86400


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _login(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/login", json={"password": "ems2026"})).status_code == 200


def _manual_point() -> int:
    """建一个 protocol=manual 的设备与机械排水点。"""
    device_id = int(db.execute(
        "INSERT INTO device (name, protocol, host, port, timeout_ms, retry, poll_interval, is_enabled) "
        "VALUES ('机械表设备','manual','127.0.0.1',502,3000,2,60,1)"))
    return create_point(device_id, name="冷却塔机械排水量", data_type="f32",
                        unit="m³", point_role="other")


def test_record_writes_manual_reading_reading_and_latest_in_one_txn(ready_db):
    """Arrange: 手动点。Act: record。Assert: manual_reading + reading(q=3) + reading_latest 三表一致。"""
    point_id = _manual_point()
    result = manual_service.record({
        "point_id": point_id, "ts": DAY, "value": 12.5,
        "operator": "operator-1", "remark": "首录",
    })

    assert result["overwritten"] is False
    assert result["point_id"] == point_id
    assert db.query_one(
        "SELECT value, operator, remark FROM manual_reading WHERE point_id=? AND ts=?",
        (point_id, DAY))["value"] == 12.5
    reading = db.query_one(
        "SELECT value, quality FROM reading WHERE point_id=? AND ts=?", (point_id, DAY))
    assert (reading["value"], reading["quality"]) == (12.5, 3)
    latest = db.query_one(
        "SELECT ts, value, quality FROM reading_latest WHERE point_id=?", (point_id,))
    assert (latest["ts"], latest["value"], latest["quality"]) == (DAY, 12.5, 3)


def test_duplicate_record_overwrites_and_logs_manual_overwrite(ready_db):
    """Arrange: 已录入一次。Act: 同 (point_id,ts) 再录。Assert: 覆盖且同事务写 MANUAL_OVERWRITE 审计。"""
    point_id = _manual_point()
    manual_service.record({"point_id": point_id, "ts": DAY, "value": 10.0,
                           "operator": "A", "remark": "first"})
    result = manual_service.record({"point_id": point_id, "ts": DAY, "value": 22.0,
                                    "operator": "B", "remark": "second"})

    assert result["overwritten"] is True
    assert int(db.query_one(
        "SELECT COUNT(*) AS c FROM manual_reading WHERE point_id=? AND ts=?",
        (point_id, DAY))["c"]) == 1
    assert db.query_one(
        "SELECT value, operator FROM manual_reading WHERE point_id=? AND ts=?",
        (point_id, DAY))["value"] == 22.0
    assert db.query_one(
        "SELECT value, quality FROM reading WHERE point_id=? AND ts=?",
        (point_id, DAY))["value"] == 22.0
    event = db.query_one(
        "SELECT source, code, ref_id FROM event_log WHERE code='MANUAL_OVERWRITE'")
    assert event is not None
    assert event["source"] == "user" and event["ref_id"] == point_id


def test_historical_backfill_recomputes_daily_and_keeps_is_final(ready_db):
    """Arrange: 历史日已 final 结算。Act: 补录该小时人工累计读数。Assert: hourly/daily 重算且 is_final 保留。"""
    node = create_node(name="A栋")
    device = create_device()
    reading_point = create_point(device, name="累计电量", reg_offset=0, reg_count=1,
                                 data_type="u16")
    meter = create_meter(node, name="1号表", purpose="total")
    link_point(meter, reading_point, "reading")
    create_tariff(price_cent=80, is_default=1)
    add_reading(reading_point, DAY, 100.0)
    add_reading(reading_point, DAY + 1800, 110.0)
    aggregator.rollup_hour(DAY)
    fill_hourly(meter, DAY, skip=(0,))  # 补齐 24 小时，满足定稿门槛
    aggregator.settle_day(DAY, is_final=1)
    before = db.query_one("SELECT energy_wh, is_final FROM daily WHERE day_start=?", (DAY,))
    assert (before["energy_wh"], before["is_final"]) == (10_000_000, 1)

    # 同小时补录更高的累计读数（quality=3），应被 rollup 采用
    manual_service.record({
        "point_id": reading_point, "ts": DAY + 2700, "value": 115.0,
        "operator": "operator-1", "remark": "补录",
    })

    hourly = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=? AND hour_start=?",
                          (meter, DAY))
    assert hourly["energy_wh"] == 15_000_000
    after = db.query_one("SELECT energy_wh, is_final FROM daily WHERE day_start=?", (DAY,))
    assert after["energy_wh"] == 15_000_000
    assert after["is_final"] == 1, "回填必须保留原 is_final"
    assert db.query_one(
        "SELECT quality FROM reading WHERE point_id=? AND ts=?",
        (reading_point, DAY + 2700))["quality"] == 3


def test_manual_api_post_get_and_auth_and_validation(ready_db):
    """Arrange: 登录/匿名客户端。Act: POST/GET /api/manual。Assert: 401/200、三表一致、非法参数 422。"""
    point_id = _manual_point()

    async def scenario():
        async with _client() as client:
            anon_post = await client.post("/api/manual", json={
                "point_id": point_id, "ts": DAY, "value": 1.0, "operator": "x"})
            anon_get = await client.get("/api/manual")
            assert anon_post.status_code == 401
            assert anon_get.status_code == 401

            await _login(client)
            bad = await client.post("/api/manual", json={
                "point_id": point_id, "ts": 0, "value": 1.0, "operator": "x"})
            assert bad.status_code in (400, 422) and bad.json()["ok"] is False
            missing = await client.post("/api/manual", json={
                "point_id": 999999, "ts": DAY, "value": 1.0, "operator": "x"})
            assert missing.status_code == 404

            created = await client.post("/api/manual", json={
                "point_id": point_id, "ts": DAY, "value": 8.5,
                "operator": "现场", "remark": "API 录入"})
            assert created.status_code == 200, created.text
            assert created.json()["ok"] is True
            assert created.json()["data"]["value"] == 8.5

            listed = await client.get(f"/api/manual?point_id={point_id}")
            assert listed.status_code == 200
            rows = listed.json()["data"]
            assert any(r["ts"] == DAY and r["value"] == 8.5 and r["operator"] == "现场"
                       for r in rows)

    run(scenario())


def test_manual_list_returns_most_recent_first(ready_db):
    """Arrange: 三个时间点录入。Act: list。Assert: 按 ts 倒序返回最近记录。"""
    point_id = _manual_point()
    for ts, value in ((DAY, 1.0), (DAY + 100, 2.0), (DAY + 200, 3.0)):
        manual_service.record({"point_id": point_id, "ts": ts, "value": value,
                               "operator": "op"})
    rows = manual_service.list({"point_id": point_id})
    assert [r["ts"] for r in rows] == [DAY + 200, DAY + 100, DAY]
