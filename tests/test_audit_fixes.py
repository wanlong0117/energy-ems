"""2026-11-26 审计报告修复回归：阻断 5 项 + 高优 8 项各配一条能翻红的用例。

覆盖审计报告「测试与工程质量」一节点名的七类盲区：
live 链路端到端、超时落库质量码、并发事务、默认密码接口拦截、
接口文档鉴权、外键删除状态码、手动录入时间边界。
"""
from __future__ import annotations

import asyncio
import io
import threading
import time

import httpx
import pytest
from openpyxl import load_workbook

import auth
import db
import main
from aggregation import Aggregator, aggregator
from manual import manual_service
from reports import report_service
from settings import MILLI_PER_KWH, SECONDS_PER_DAY, SECONDS_PER_HOUR

from helpers import (
    add_reading, create_device, create_meter, create_node, create_point,
    fill_hourly, link_point, run, wait_until,
)

HOUR: int = SECONDS_PER_HOUR


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _login(client: httpx.AsyncClient, password: str = "ems2026") -> None:
    assert (await client.post("/api/login", json={"password": password})).status_code == 200


def _meter_with_point(name: str = "1号表") -> tuple[int, int]:
    """建 1 个节点 + 1 台设备 + 1 个累计电量点 + 1 块表，返回 (meter_id, point_id)。"""
    node = create_node(name="A栋")
    device = create_device()
    point = create_point(device, reg_offset=0, reg_count=1, data_type="u32")
    meter = create_meter(node, name=name)
    link_point(meter, point, "reading")
    return meter, point


# ---------- 阻断级（一）：live 模式小时汇总 ----------


def test_live_readings_flow_to_hourly_daily_and_report(ready_db):
    """Arrange: live 口径下连续两小时读数。Act: 收口小时 + 日结算。Assert: 小时/日/报表都有数。

    这条用例正是审计认定的最大盲区：旧用例只断言「起了哪些任务」，
    所以 live 模式缺小时汇总任务时测试依然全绿。
    """
    meter, point = _meter_with_point()
    now: int = int(time.time())
    hour_start: int = (now // HOUR) * HOUR
    # 上一小时：整点 100，半点 101；本小时：整点 101，现价 103.5
    add_reading(point, hour_start - HOUR, 100.0)
    add_reading(point, hour_start - HOUR + 1800, 101.0)
    add_reading(point, hour_start, 101.0)
    add_reading(point, now, 103.5)

    aggregator.rollup_recent_hours(now)

    hourly = db.query(
        "SELECT hour_start, energy_wh FROM hourly WHERE meter_id=? ORDER BY hour_start", (meter,)
    )
    assert [int(row["hour_start"]) for row in hourly] == [hour_start - HOUR, hour_start]
    assert [int(row["energy_wh"]) for row in hourly] == [
        int(round(1.0 * MILLI_PER_KWH)), int(round(2.5 * MILLI_PER_KWH))
    ]

    aggregator.settle_day(hour_start, is_final=0)
    daily = db.query_one("SELECT energy_wh FROM daily WHERE meter_id=?", (meter,))
    assert int(daily["energy_wh"]) == int(round(2.5 * MILLI_PER_KWH))
    rows = report_service.build("daily", {"from_ts": hour_start, "to_ts": hour_start})
    assert rows and rows[0]["meter_name"] == "1号表"


def test_live_mode_registers_hourly_rollup_task(ready_db, monkeypatch):
    """Arrange: data_mode=live，后台任务替换为探针。Act: 进入 lifespan。Assert: 小时汇总任务被创建。"""
    started: list[str] = []

    async def fake_hourly() -> None:
        started.append("hourly")
        await asyncio.sleep(0)

    async def fake_day_loop() -> None:
        started.append("day")
        await asyncio.sleep(0)

    async def fake_collector() -> None:
        await asyncio.sleep(0)

    config = auth.load_config()
    config["data_mode"] = "live"
    auth.save_config(config)
    monkeypatch.setattr(main.collector, "start", fake_collector)
    monkeypatch.setattr(main.aggregator, "run_hourly_loop", fake_hourly)
    monkeypatch.setattr(main.aggregator, "run_loop", fake_day_loop)

    async def scenario() -> None:
        async with main.lifespan(main.app):
            await asyncio.sleep(0.05)

    run(scenario())
    assert "hourly" in started, "live 模式必须启动小时汇总任务"
    assert "day" in started


# ---------- 阻断级（二）：服务端强制改密 ----------


def test_default_password_cannot_call_business_api(isolated_db):
    """Arrange: 默认密码登录。Act: 调业务接口与改密接口。Assert: 业务 403、改密 200。"""
    async def scenario():
        async with _client() as client:
            await _login(client)
            blocked = await client.post("/api/config/node", json={"name": "A栋"})
            assert blocked.status_code == 403, blocked.text
            assert "密码" in blocked.json()["error"]
            session = await client.get("/api/session")
            assert session.status_code == 200
            assert session.json()["data"]["must_change_password"] is True

            changed = await client.post(
                "/api/password",
                json={"old_password": "ems2026", "new_password": "NewPass2026"},
            )
            assert changed.status_code == 200, changed.text

            await _login(client, "NewPass2026")
            allowed = await client.post("/api/config/node", json={"name": "A栋"})
            assert allowed.status_code == 200, allowed.text

    run(scenario())


# ---------- 阻断级（三）：共享连接事务原子性 ----------


def test_rollback_in_transaction_does_not_commit_other_writer(isolated_db):
    """Arrange: 线程 A 开事务写入后回滚，线程 B 期间执行普通写。Act: 并发。Assert: A 无痕、B 留存。

    旧实现下 B 的隐式提交会把 A 未提交的写入一并提交，A 回滚已无法撤回。
    """
    def writer_a() -> None:
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO node (name) VALUES ('TX-A')")
                time.sleep(0.2)
                raise RuntimeError("模拟业务失败")
        except RuntimeError:
            pass

    thread_a = threading.Thread(target=writer_a)
    thread_a.start()
    time.sleep(0.05)  # 保证 A 已进入事务
    db.execute("INSERT INTO node (name) VALUES ('B')")
    thread_a.join(timeout=5)

    names = {row["name"] for row in db.query("SELECT name FROM node")}
    assert "TX-A" not in names, "A 已回滚的写入不得残留"
    assert "B" in names, "B 的提交不应被 A 的回滚连带撤销"


# ---------- 阻断级（四）：间隔法计量 ----------


def test_hourly_energy_uses_boundary_base_not_window_first(ready_db):
    """Arrange: 整点前一条 + 窗口内一条。Act: rollup。Assert: 电量含跨小时那一整个间隔。"""
    aggregator_local = Aggregator()
    meter, point = _meter_with_point()
    hour_start = (int(time.time()) // HOUR) * HOUR - HOUR
    add_reading(point, hour_start - 60, 100.0)          # 基准在窗口外
    add_reading(point, hour_start + 3540, 100.6)        # 收口在窗口内末尾

    aggregator_local.rollup_hour(hour_start)

    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter,))
    assert int(row["energy_wh"]) == int(round(0.6 * MILLI_PER_KWH)), "必须取窗口外基准，否则少计 0.6kWh"


def test_hourly_rollup_falls_back_and_logs_when_base_missing(ready_db):
    """Arrange: 只有窗口内读数（无基准）。Act: rollup。Assert: 回退首条且留 ROLLUP_NO_BASE。"""
    meter, point = _meter_with_point()
    hour_start = (int(time.time()) // HOUR) * HOUR - HOUR
    add_reading(point, hour_start + 600, 10.0)
    add_reading(point, hour_start + 3000, 11.0)

    Aggregator().rollup_hour(hour_start)

    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter,))
    assert int(row["energy_wh"]) == int(round(1.0 * MILLI_PER_KWH))
    assert db.query_one("SELECT 1 FROM event_log WHERE code='ROLLUP_NO_BASE'") is not None


# ---------- 阻断级（五）：接口文档鉴权 ----------


def test_docs_and_openapi_require_login(ready_db):
    """Arrange/Act: 匿名与登录后分别访问 /docs、/openapi.json。Assert: 401 / 200。"""
    async def scenario():
        async with _client() as client:
            for path in ("/docs", "/openapi.json"):
                anonymous = await client.get(path)
                assert anonymous.status_code == 401, f"{path} 不应匿名可读"
            # redoc 直接关闭，落到静态看板后返回 404，同样不可读
            assert (await client.get("/redoc")).status_code != 200

            await _login(client)
            docs = await client.get("/docs")
            assert docs.status_code == 200
            assert "swagger" in docs.text.lower()
            spec = await client.get("/openapi.json")
            assert spec.status_code == 200
            assert "/api/daily" in spec.text

    run(scenario())


# ---------- 高优（一）：读超时落库 ----------


def test_read_failure_writes_quality_1_row(ready_db):
    """Arrange: 已有历史读数 + 指向关闭端口。Act: 跑一轮采集。Assert: 落 quality=1 且值保持上一值。"""
    device = create_device(host="127.0.0.1", port=1, timeout_ms=50, retry=0)
    point = create_point(device, reg_offset=0, reg_count=1, data_type="u16")
    db.execute(
        "INSERT INTO reading_latest (point_id, ts, value, quality) VALUES (?,?,?,0)",
        (point, int(time.time()) - 60, 42.0),
    )

    async def scenario() -> None:
        task = asyncio.create_task(main.collector.poll_device(
            dict(db.query_one("SELECT * FROM device WHERE id=?", (device,)))))
        try:
            assert await wait_until(lambda: db.query_one(
                "SELECT 1 FROM reading WHERE point_id=? AND quality=1", (point,)) is not None)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    row = db.query_one("SELECT value, quality FROM reading WHERE point_id=?", (point,))
    assert (float(row["value"]), int(row["quality"])) == (42.0, 1)


# ---------- 高优（二）：覆盖率定稿 ----------


def test_settle_day_requires_hour_and_meter_coverage(ready_db):
    """Arrange: 1 小时数据 + 2 块表只到 1 块。Act: settle。Assert: 先不定稿，补齐后定稿。"""
    meter, _ = _meter_with_point()
    create_node(name="B栋")
    node_b = int(db.query_one("SELECT id FROM node WHERE name='B栋'")["id"])
    meter_b = create_meter(node_b, name="2号表")
    day_start = (int(time.time()) // SECONDS_PER_DAY) * SECONDS_PER_DAY - SECONDS_PER_DAY
    fill_hourly(meter, day_start, energy_wh=1_000_000)  # 只有 A 表、且只有 24 小时 -> 表覆盖 1/2

    aggregator.settle_day(day_start)
    assert db.query_one(
        "SELECT is_final FROM daily WHERE meter_id=?", (meter,))["is_final"] == 0
    assert db.query_one("SELECT 1 FROM event_log WHERE code='SETTLE_INCOMPLETE'") is not None

    fill_hourly(meter_b, day_start, energy_wh=500_000)
    aggregator.settle_day(day_start)
    assert db.query_one(
        "SELECT is_final FROM daily WHERE meter_id=?", (meter,))["is_final"] == 1


# ---------- 高优（三）：异常脱敏与外键状态码 ----------


def test_delete_referenced_point_returns_409_and_missing_fk_returns_400(ready_db):
    """Arrange: 被计量表引用的点位。Act: 删除该点位；创建指向不存在设备的点位。Assert: 409 / 400。"""
    _meter, point = _meter_with_point()

    async def scenario():
        async with _client() as client:
            await _login(client)
            deleted = await client.delete(f"/api/config/point/{point}")
            assert deleted.status_code == 409, deleted.text
            assert "FOREIGN KEY" not in deleted.text, "不得回显数据库原文"

            bad_fk = await client.post("/api/config/point", json={
                "name": "孤儿点位", "device_id": 999999, "slave_id": 1, "func_code": 3,
                "reg_offset": 0, "reg_count": 1, "data_type": "u16", "byte_order": "ABCD",
                "scale": 1.0, "offset_const": 0.0, "unit": "kWh", "point_role": "energy",
            })
            assert bad_fk.status_code == 400, bad_fk.text
            assert "不存在" in bad_fk.json()["error"]

    run(scenario())


# ---------- 高优（四）：90 天清理 ----------


def test_purge_removes_readings_older_than_retention(ready_db):
    """Arrange: 一条 100 天前读数 + 一条今天的。Act: purge。Assert: 仅超期被删。"""
    _meter, point = _meter_with_point()
    now = int(time.time())
    add_reading(point, now - 100 * SECONDS_PER_DAY, 1.0)
    add_reading(point, now - 10, 2.0)

    removed = Aggregator().purge_old_readings(now)

    assert removed == 1
    remaining = db.query("SELECT ts FROM reading WHERE point_id=?", (point,))
    assert len(remaining) == 1 and int(remaining[0]["ts"]) == now - 10


# ---------- 高优（五）：登录失败锁定与口令哈希 ----------


def test_login_locks_after_repeated_failures(isolated_db):
    """Arrange: 同来源连错 5 次。Act: 第 6 次用正确密码。Assert: 429 且事件留痕。"""
    auth._fail_state.clear()
    for _ in range(auth.LOGIN_FAIL_LIMIT):
        assert auth.login("wrong", None, client="10.0.0.9").status_code == 401
    locked = auth.login("ems2026", None, client="10.0.0.9")
    assert locked.status_code == 429
    assert db.query_one("SELECT 1 FROM event_log WHERE code='LOGIN_LOCKED'") is not None
    # 其它来源不受影响
    assert auth.login("ems2026", None, client="10.0.0.10").status_code == 200


def test_password_is_stored_hashed_after_login(isolated_db):
    """Arrange: 默认明文配置。Act: 登录一次。Assert: 配置里是 pbkdf2 哈希，且仍能校验。"""
    assert auth.is_hashed(auth.load_password()) is False
    assert auth.login("ems2026", None).status_code == 200
    stored = auth.load_password()
    assert auth.is_hashed(stored) is True
    assert "ems2026" not in stored
    assert auth.verify_login_password("ems2026") is True


# ---------- 高优（六）：手动录入时间边界 ----------


def test_manual_record_rejects_out_of_window_timestamps(ready_db):
    """Arrange: 手动点位。Act: 1970 / 未来 / 正常三种 ts。Assert: 前两者 ValueError，正常通过。"""
    node = create_node(name="A栋")
    device = int(db.execute(
        "INSERT INTO device (name, protocol, host, port, timeout_ms, retry, poll_interval, is_enabled) "
        "VALUES ('机械表设备','manual','127.0.0.1',502,3000,2,60,1)"))
    point = create_point(device, name="机械排水量", data_type="f32", unit="m³",
                         point_role="other")
    assert node > 0

    with pytest.raises(ValueError):
        manual_service.record({"point_id": point, "ts": 0, "value": 1.0, "operator": "operator-1"})
    with pytest.raises(ValueError):
        manual_service.record({
            "point_id": point, "ts": int(time.time()) + 3600, "value": 1.0, "operator": "operator-1",
        })
    result = manual_service.record({
        "point_id": point, "ts": int(time.time()) - SECONDS_PER_DAY, "value": 1.0,
        "operator": "operator-1",
    })
    assert result["overwritten"] is False


# ---------- 高优（七）：报表默认区间与行数上限 ----------


def test_report_export_defaults_to_recent_window_and_caps_span(ready_db):
    """Arrange: 90 天前的日汇总。Act: 不带时间范围导出。Assert: 默认 31 天不含老数据；超长跨度 400。"""
    node = create_node(name="A栋")
    meter = create_meter(node, name="1号表")
    old_day = (int(time.time()) // SECONDS_PER_DAY) * SECONDS_PER_DAY - 90 * SECONDS_PER_DAY
    db.execute(
        "INSERT OR REPLACE INTO daily "
        "(meter_id, day_start, energy_wh, peak_power, avg_power, cost_cent, is_final) "
        "VALUES (?,?,?,?,?,?,1)",
        (meter, old_day, 1_000_000, 10.0, 5.0, 80),
    )

    async def scenario():
        async with _client() as client:
            await _login(client)
            # 服务层不设默认区间，默认窗口由接口层负责
            assert report_service.build("daily", {}), "服务层应返回全部数据"
            exported = await client.get("/api/report/export?kind=daily&fmt=xlsx")
            assert exported.status_code == 200
            sheet = load_workbook(io.BytesIO(exported.content)).active
            periods = [
                str(row[0]) for row in sheet.iter_rows(values_only=True)
                if row and row[0] and str(row[0])[:4] == "20"
            ]
            assert periods == [], f"默认 31 天窗口不应包含 90 天前的数据: {periods}"
            too_wide = await client.get(
                f"/api/report/export?kind=daily&from={old_day - 400 * SECONDS_PER_DAY}"
                f"&to={int(time.time())}")
            assert too_wide.status_code == 400
            assert "区间" in too_wide.json()["error"]

    run(scenario())


# ---------- 高优（八）：聚合不阻塞事件循环 ----------


def test_simulate_loop_uses_thread_for_db_work(ready_db, monkeypatch):
    """Arrange: 替换 rollup_hour 记录线程名。Act: 跑一轮模拟汇总。Assert: 不在事件循环线程执行。"""
    seen: list[str] = []

    async def fake_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    def fake_rollup(_hour_start: int) -> None:
        seen.append(threading.current_thread().name)

    monkeypatch.setattr(aggregator, "rollup_hour", fake_rollup)
    monkeypatch.setattr(aggregator, "settle_day", lambda *a, **k: None)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await aggregator.run_simulate_loop()

    run(scenario())
    assert seen, "模拟汇总应调用 rollup_hour"
    assert all("ThreadPoolExecutor" in name or name != "MainThread" for name in seen)
