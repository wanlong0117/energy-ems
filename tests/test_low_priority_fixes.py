"""2026-11-26 审计报告「中低 12 项」修复回归。

逐项对应：分时电价、电价快照、迁移机制、日切点可配置、串口波特率、
Modbus 报文校验、采集任务复活、配置损坏失败关闭、健康检查/备份、
会话过期跳登录、依赖锁定与 CI 门槛、文档一致性。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

import auth
import db
import main
import settings
from aggregation import Aggregator, aggregator
from collector import Collector, collector
from modbus import ModbusError
from scripts.backup import backup, main as backup_main, prune
from settings import MILLI_PER_KWH, SECONDS_PER_DAY, SECONDS_PER_HOUR

from helpers import (
    add_reading, create_device, create_meter, create_node, create_point,
    create_tariff, fill_hourly, link_point, run,
)

ROOT: Path = Path(__file__).resolve().parent.parent


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _login(client: httpx.AsyncClient, password: str = "ems2026") -> None:
    assert (await client.post("/api/login", json={"password": password})).status_code == 200


def _meter_with_point(name: str = "1号表") -> tuple[int, int]:
    node = create_node(name="A栋")
    device = create_device()
    point = create_point(device, reg_offset=0, reg_count=1, data_type="u32")
    meter = create_meter(node, name=name)
    link_point(meter, point, "reading")
    return meter, point


def _yesterday() -> int:
    return (int(time.time()) // SECONDS_PER_DAY) * SECONDS_PER_DAY - SECONDS_PER_DAY


# ---------- 1/2. 分时电价 + 电价快照 ----------


def test_settle_day_applies_time_of_use_tariff(ready_db):
    """Arrange: 峰段（本地 10-12 点）120 分、平价 60 分。Act: 结算全天 24 kWh。

    Assert: 2 小时按 120 分、22 小时按 60 分计费，且 snapshot 记录时段明细。
    旧实现按日总量乘单一电价，峰平谷字段完全没生效。
    """
    meter, _point = _meter_with_point()
    day_start = _yesterday()
    create_tariff(price_cent=120, is_default=0, name="峰", start_hour=10, end_hour=12)
    create_tariff(price_cent=60, is_default=1, name="平")
    fill_hourly(meter, day_start, energy_wh=MILLI_PER_KWH)  # 每小时 1 kWh

    aggregator.settle_day(day_start)

    row = db.query_one(
        "SELECT energy_wh, cost_cent, price_cent, tariff_snapshot FROM daily WHERE meter_id=?",
        (meter,),
    )
    assert int(row["energy_wh"]) == 24 * MILLI_PER_KWH
    assert int(row["cost_cent"]) == 2 * 120 + 22 * 60, "峰段两小时必须按 120 分计"
    assert float(row["price_cent"]) == pytest.approx(1560 / 24)
    snapshot = json.loads(str(row["tariff_snapshot"]))
    assert snapshot["mode"] == "tou"
    assert snapshot["segments"][0]["start_hour"] == 10
    assert snapshot["uncovered_hours"] == 0


def test_settle_day_flat_tariff_snapshot_is_recorded(ready_db):
    """单一电价也要落快照，否则改价后无法解释历史电费。"""
    meter, _point = _meter_with_point()
    day_start = _yesterday()
    create_tariff(price_cent=80, is_default=1, name="平价")
    fill_hourly(meter, day_start, energy_wh=MILLI_PER_KWH)

    aggregator.settle_day(day_start)

    row = db.query_one(
        "SELECT cost_cent, price_cent, tariff_snapshot FROM daily WHERE meter_id=?", (meter,)
    )
    assert int(row["cost_cent"]) == 24 * 80
    assert float(row["price_cent"]) == pytest.approx(80.0)
    assert json.loads(str(row["tariff_snapshot"]))["mode"] == "flat"


def test_reprice_of_final_day_is_logged(ready_db):
    """已定稿日按新电价重算必须留 TARIFF_REPRICE 事件，不能悄悄改历史。"""
    meter, _point = _meter_with_point()
    day_start = _yesterday()
    tariff_id = create_tariff(price_cent=60, is_default=1, name="平价")
    fill_hourly(meter, day_start, energy_wh=MILLI_PER_KWH)
    aggregator.settle_day(day_start)

    db.execute("UPDATE tariff SET price_cent=100 WHERE id=?", (tariff_id,))
    aggregator.settle_day(day_start)

    row = db.query_one("SELECT cost_cent, price_cent FROM daily WHERE meter_id=?", (meter,))
    assert int(row["cost_cent"]) == 24 * 100
    assert float(row["price_cent"]) == pytest.approx(100.0)
    assert db.query_one("SELECT 1 FROM event_log WHERE code='TARIFF_REPRICE'") is not None


def test_tou_segment_validation_rejects_bad_range(ready_db):
    """时段电价非法（start ≥ end）必须在配置接口被 400 拒绝。"""
    async def scenario():
        async with _client() as client:
            await _login(client)
            bad = await client.post("/api/config/tariff", json={
                "name": "坏时段", "price_cent": 90, "start_hour": 12, "end_hour": 12,
            })
            assert bad.status_code == 400, bad.text
            ok = await client.post("/api/config/tariff", json={
                "name": "峰", "price_cent": 90, "start_hour": 8, "end_hour": 12,
            })
            assert ok.status_code == 200, ok.text

    run(scenario())


# ---------- 4. 日切点可配置 ----------


def test_day_floor_follows_configurable_boundary(monkeypatch):
    """默认日切点 UTC 00:00 与旧行为一致；改到 04:00 UTC 后整体平移。"""
    ts = 1_700_003_000
    monkeypatch.setattr(settings, "DAY_BOUNDARY_UTC_HOUR", 0)
    assert settings.day_floor(ts) == (ts // SECONDS_PER_DAY) * SECONDS_PER_DAY

    monkeypatch.setattr(settings, "DAY_BOUNDARY_UTC_HOUR", 4)
    offset = 4 * SECONDS_PER_HOUR
    assert settings.day_floor(ts) == ((ts - offset) // SECONDS_PER_DAY) * SECONDS_PER_DAY + offset


def test_local_hour_uses_configured_timezone(monkeypatch):
    """分时电价按本地小时匹配：UTC 02:00 在 UTC+8 下是 10 点。"""
    ts = 1_700_000_000
    monkeypatch.setattr(settings, "TZ_OFFSET_HOURS", 8)
    assert settings.local_hour(ts) == ((ts // SECONDS_PER_HOUR) + 8) % 24


# ---------- 5. 串口波特率 ----------


def test_serial_baudrate_is_not_silently_rewritten(isolated_db):
    """4800 等低波特率必须原样使用；0 则直接报错，不再悄悄回退 9600。"""
    service = Collector()
    client = service._client_for(
        {"protocol": "modbus_rtu_serial", "host": "COM3", "port": 4800})
    assert client.baudrate == 4800
    with pytest.raises(ModbusError):
        service._client_for({"protocol": "modbus_rtu_serial", "host": "COM3", "port": 0})


def test_device_api_rejects_invalid_serial_baudrate(ready_db):
    async def scenario():
        async with _client() as client:
            await _login(client)
            bad = await client.post("/api/config/device", json={
                "name": "串口表", "protocol": "modbus_rtu_serial", "host": "COM3", "port": 0,
            })
            assert bad.status_code == 400, bad.text
            good = await client.post("/api/config/device", json={
                "name": "串口表", "protocol": "modbus_rtu_serial", "host": "COM3", "port": 4800,
            })
            assert good.status_code == 200, good.text

    run(scenario())


# ---------- 7. 采集任务复活 ----------


def test_collector_revives_dead_task_and_logs(ready_db):
    """采集任务因未捕获异常结束后，巡检必须重建并留 COLLECTOR_REVIVE。"""
    device = create_device(host="127.0.0.1", port=1, timeout_ms=20, retry=0, poll_interval=3600)

    async def scenario():
        collector.tasks.clear()

        async def boom() -> None:
            raise RuntimeError("模拟未捕获异常")

        task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        collector.tasks[device] = task

        collector._revive_dead_tasks()

        revived = collector.tasks.get(device)
        assert revived is not None and revived is not task
        assert not revived.done(), "复活后的任务必须在运行"
        revived.cancel()
        await asyncio.gather(revived, return_exceptions=True)
        collector.tasks.clear()

    run(scenario())
    assert db.query_one("SELECT 1 FROM event_log WHERE code='COLLECTOR_REVIVE'") is not None


# ---------- 8. 配置损坏失败关闭 ----------


def test_broken_config_refuses_login_and_never_falls_back(isolated_db):
    """config.json 损坏时登录必须 503，绝不能回退到公开默认密码 ems2026。"""
    auth.save_config(auth.load_config())
    auth.CONFIG_PATH.write_text("{ broken json", encoding="utf-8")

    with pytest.raises(auth.ConfigError):
        auth.load_config()
    assert auth.login("ems2026", None).status_code == 503

    async def scenario():
        async with _client() as client:
            response = await client.post("/api/login", json={"password": "ems2026"})
            assert response.status_code == 503, response.text
            session = await client.get("/api/session")
            assert session.json()["data"]["config_error"] is True

    run(scenario())
    assert db.query_one("SELECT 1 FROM event_log WHERE code='CONFIG_PARSE_FAIL'") is not None


# ---------- 10. 健康检查与备份 ----------


def test_healthz_is_public_and_health_requires_login(ready_db):
    async def scenario():
        async with _client() as client:
            public = await client.get("/healthz")
            assert public.status_code == 200
            payload = public.json()
            assert payload["status"] in ("ok", "degraded")
            assert "uptime_sec" in payload and "version" in payload
            assert "collector" not in payload, "匿名探活不暴露运行细节"

            assert (await client.get("/api/health")).status_code == 401
            await _login(client)
            detailed = await client.get("/api/health")
            assert detailed.status_code == 200
            data = detailed.json()
            assert data["data_mode"] in ("simulate", "live")
            assert int(data["schema_version"]) >= 1

    run(scenario())


def test_backup_script_creates_and_prunes_snapshots(ready_db, tmp_path):
    """备份必须产出可打开的完整数据库，并按保留份数清理旧文件。"""
    db.execute("INSERT INTO node (name) VALUES ('备份验证')")
    out = tmp_path / "backups"
    target = backup(db.DB_PATH, out)
    assert target.exists() and target.stat().st_size > 0

    import sqlite3

    connection = sqlite3.connect(target)
    try:
        assert connection.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 1
    finally:
        connection.close()

    for index in range(3):
        (out / f"energy-2020010{index}-000000.db").write_bytes(b"old")
    removed = prune(out, keep=2)
    assert len(removed) == 2
    assert backup_main(["--db", str(db.DB_PATH), "--out", str(out), "--keep", "5"]) == 0


# ---------- 11/9/12. 看板、依赖与文档一致性 ----------


def test_dashboard_uses_kwh_and_redirects_on_401():
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    # KPI 口径是「今日」（按后端日切点算），不是「全部历史求和」
    assert "今日电量(kWh)" in html and "今日电费(元)" in html
    assert "区间电量(毫度)" not in html and "区间电费(分)" not in html
    assert "fmtKwh" in html and "fmtYuan" in html
    # 时间窗由前端显式传给接口，避免接口退回默认窗导致 KPI 口径漂移
    assert "state.todayStart" in html and "?from=" in html
    assert "status === 401" in html and "session_expired" in html


def test_requirements_are_pinned():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#") or entry.startswith("-r"):
                continue
            assert "==" in entry, f"{name}: {entry} 未锁定版本"


def test_ci_enforces_lint_and_coverage_gate():
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "ruff check" in workflow
    assert "--cov" in workflow
    coverage_config = (ROOT / ".coveragerc").read_text(encoding="utf-8")
    assert "fail_under" in coverage_config


def test_readme_documents_new_capabilities():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for keyword in ("kWh", "分时电价", "healthz", "backup.py"):
        assert keyword in readme, f"README 缺少 {keyword} 说明"


# ---------- 6. Modbus 校验（补充：读超时仍重试） ----------


def test_timeout_still_retries(ready_db):
    """异常码不重试，但网络超时仍按 retry 退避重试。"""
    from helpers import FakeGateway
    from modbus import ModbusClient, ModbusTimeoutError

    async def scenario():
        async with FakeGateway(behavior="drop", delay=0.2) as gateway:
            client = ModbusClient("127.0.0.1", gateway.port, timeout_ms=30, retry=1)
            with pytest.raises(ModbusTimeoutError):
                await client.read_registers(1, 3, 0, 2)
            assert gateway.connects == 2

    run(scenario())


def test_manual_recompute_keeps_price_snapshot(ready_db):
    """人工补录触发的小时重算也要写价格快照（不能只在自动结算时写）。"""
    meter, point = _meter_with_point()
    create_tariff(price_cent=70, is_default=1, name="平价")
    ts = int(time.time()) - SECONDS_PER_HOUR
    add_reading(point, ts, 100.0)
    add_reading(point, ts + 600, 101.0)

    Aggregator().rollup_hour((ts // SECONDS_PER_HOUR) * SECONDS_PER_HOUR)
    aggregator.settle_day(settings.day_floor(ts), is_final=0)

    row = db.query_one("SELECT price_cent FROM daily WHERE meter_id=?", (meter,))
    assert row is not None and row["price_cent"] is not None
