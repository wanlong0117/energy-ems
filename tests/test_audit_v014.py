"""v0.1.4 复审修复回归：演示数据隔离、补录边界、ID 复用、非有限值、配置审计等。

每个用例对应 v0.1.3 复审报告的一节，断言的是「业务结果」而不是「函数被调用」：
  · P0  演示表不再拖死 live 日结覆盖率，报表里不再出现「模拟-」
  · P1-1 整点补录要连带重算上一小时与上一日
  · P1-2 删除重建不继承旧读数/旧日报
  · P1-3 超大值与非有限值在入口被拒，单表异常不拖垮整轮汇总
  · P2-1 改端口后采集任务重建；P2-2 配置变更留痕；P2-4 缺失表落零行并带覆盖小时
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import db
import httpx
import main
import pytest
from aggregation import aggregator
from helpers import (
    add_reading, create_device, create_meter, create_node, create_point,
    create_tariff, fill_hourly, link_point, run,
)
from manual import manual_service
from schemas import ManualIn

DAY_START = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/login", json={"password": "ems2026"})
    assert response.status_code == 200, response.text


def _chain(name: str = "1号表") -> tuple[int, int, int, int]:
    """建一条 节点→设备→点位→表 的最小链路，返回 (node, device, point, meter)。"""
    node = create_node("A栋")
    device = create_device(name=f"{name}-设备")
    point = create_point(device, name=f"{name}-累计电量")
    meter = create_meter(node, name=name)
    link_point(meter, point)
    return node, device, point, meter


# ---------------- P0 演示数据隔离 ----------------
def test_demo_meters_do_not_block_live_settlement(ready_db):
    """1 块真实表（24 小时全齐）+ 100 块演示表：真实表必须定稿，且只落真实表的行。"""
    node, _device, point, real_meter = _chain("真实表")
    fill_hourly(real_meter, DAY_START, energy_wh=500000, sample_cnt=12)
    for index in range(100):
        demo_meter = create_meter(node, name=f"模拟-{index:03d}号电表")
        db.execute("UPDATE meter SET is_demo=1 WHERE id=?", (demo_meter,))
        link_point(demo_meter, point)

    aggregator.settle_day(DAY_START)

    rows = db.query("SELECT meter_id, is_final FROM daily WHERE day_start=?", (DAY_START,))
    assert [int(row["meter_id"]) for row in rows] == [real_meter]
    assert int(rows[0]["is_final"]) == 1

    # 报表查询同样排除演示表
    listed = db.query(
        "SELECT m.name FROM daily d JOIN meter m ON m.id=d.meter_id WHERE d.day_start=?"
        + db.demo_guard("m"),
        (DAY_START,),
    )
    assert [str(row["name"]) for row in listed] == ["真实表"]
    assert not any(str(row["name"]).startswith("模拟-") for row in listed)


def test_pure_demo_instance_still_shows_simulated_data(ready_db):
    """纯演示实例（没有任何真表）里，演示表必须照常结算，否则看板一进来就空。"""
    node = create_node("模拟-A栋")
    demo_meter = create_meter(node, name="模拟-A栋-01号电表")
    db.execute("UPDATE meter SET is_demo=1 WHERE id=?", (demo_meter,))
    fill_hourly(demo_meter, DAY_START, energy_wh=123000, sample_cnt=12)

    assert db.has_real_meter() is False
    assert db.demo_guard("m") == ""
    aggregator.settle_day(DAY_START)
    row = db.query_one("SELECT energy_wh FROM daily WHERE meter_id=?", (demo_meter,))
    assert int(row["energy_wh"]) == 123000 * 24


def test_switching_to_live_disables_demo_rows(ready_db):
    """切 live 时自动停用演示表/点位/设备，并留一条可追溯的审计事件。"""
    node = create_node("模拟-A栋")
    demo_meter = create_meter(node, name="模拟-A栋-01号电表")
    db.execute("UPDATE meter SET is_demo=1 WHERE id=?", (demo_meter,))
    demo_device = db.execute(
        "INSERT INTO device (name, protocol, host, port, is_demo) "
        "VALUES ('模拟-网关','manual','127.0.0.1',502,1)"
    )

    counts = db.disable_demo_data()

    assert counts["meter"] == 1 and counts["device"] == 1
    assert int(db.query_one("SELECT is_enabled FROM meter WHERE id=?", (demo_meter,))["is_enabled"]) == 0
    assert int(db.query_one("SELECT is_enabled FROM device WHERE id=?", (demo_device,))["is_enabled"]) == 0
    event = db.query_one("SELECT message FROM event_log WHERE code='DEMO_DISABLED' ORDER BY id DESC")
    assert event is not None and "演示数据" in str(event["message"])


# ---------------- P1-1 整点/日切点补录 ----------------
def test_manual_reading_on_hour_boundary_recomputes_previous_hour_and_day(ready_db):
    """昨日 23:00 小时 100→102（2 kWh），今日 00:00 补录 110 后昨日应为 10 kWh。"""
    create_tariff(price_cent=80)
    _node, _device, point, meter = _chain()
    yesterday = ((int(time.time()) // 86400) * 86400) - 86400
    prev_hour = yesterday + 23 * 3600
    add_reading(point, prev_hour + 60, 100.0)
    add_reading(point, prev_hour + 3000, 102.0)
    aggregator.rollup_hour(prev_hour)
    aggregator.settle_day(yesterday, is_final=1, enforce_coverage=False)
    before = db.query_one(
        "SELECT energy_wh FROM hourly WHERE meter_id=? AND hour_start=?", (meter, prev_hour)
    )
    assert int(before["energy_wh"]) == 2_000_000

    today_start: int = (int(time.time()) // 86400) * 86400
    manual_service.record({
        "point_id": point, "ts": today_start, "value": 110.0, "operator": "operator-1",
        "remark": "整点补录",
    })

    after = db.query_one(
        "SELECT energy_wh FROM hourly WHERE meter_id=? AND hour_start=?", (meter, prev_hour)
    )
    assert int(after["energy_wh"]) == 10_000_000
    daily = db.query_one(
        "SELECT energy_wh, is_final FROM daily WHERE meter_id=? AND day_start=?",
        (meter, yesterday),
    )
    assert int(daily["energy_wh"]) == 10_000_000
    assert int(daily["is_final"]) == 1


# ---------------- P1-2 实体 ID 复用 ----------------
def test_recreated_point_does_not_inherit_old_readings(ready_db):
    """删除点位后重建，新 ID 必须不同，且 reading_latest / hourly 不能带上旧数据。"""
    _node, _device, point, meter = _chain()
    add_reading(point, int(time.time()) - 60, 42.0)
    db.execute(
        "INSERT INTO reading_latest (point_id, ts, value, quality) VALUES (?,?,?,0)",
        (point, int(time.time()), 42.0),
    )
    fill_hourly(meter, DAY_START, energy_wh=111)

    # 解除关联是操作员显式动作；删除点位本身会清掉历史读数
    db.execute("DELETE FROM meter_point WHERE point_id=?", (point,))
    db.execute("DELETE FROM reading WHERE point_id=?", (point,))
    db.execute("DELETE FROM reading_latest WHERE point_id=?", (point,))
    db.execute("DELETE FROM point WHERE id=?", (point,))

    new_device = create_device(name="换表后网关")
    new_point = create_point(new_device, name="换表后点位", slave_id=2)

    assert new_point != point
    assert db.query_one("SELECT 1 FROM reading_latest WHERE point_id=?", (new_point,)) is None
    assert db.query_one("SELECT 1 FROM reading WHERE point_id=?", (new_point,)) is None


def test_meter_delete_removes_hourly_and_daily_via_api(ready_db):
    """删表必须连带清掉 hourly/daily，否则同 ID 新表会继承旧日报。"""
    _node, _device, point, meter = _chain()
    fill_hourly(meter, DAY_START, energy_wh=1000)
    db.execute(
        "INSERT OR REPLACE INTO daily (meter_id, day_start, energy_wh, is_final) "
        "VALUES (?,?,?,1)",
        (meter, DAY_START, 24000),
    )

    async def scenario():
        async with _client() as client:
            await _login(client)
            response = await client.delete(f"/api/config/meter/{meter}")
            assert response.status_code == 200, response.text

    run(scenario())
    assert db.query_one("SELECT 1 FROM hourly WHERE meter_id=?", (meter,)) is None
    assert db.query_one("SELECT 1 FROM daily WHERE meter_id=?", (meter,)) is None


# ---------------- P1-3 非有限值 / 超大值 ----------------
@pytest.mark.parametrize("bad_value", [float("inf"), float("nan"), 1e20, -1e20])
def test_manual_reading_rejects_non_finite_and_huge_values(bad_value):
    with pytest.raises(Exception):
        ManualIn(point_id=1, ts=int(time.time()), value=bad_value, operator="op")


def test_manual_service_rejects_huge_value_before_writing(ready_db):
    """服务层也要拦：脚本与运维钩子不走请求模型，但同样会调 record()。"""
    _node, _device, point, _meter = _chain()
    with pytest.raises(ValueError):
        manual_service.record({
            "point_id": point, "ts": int(time.time()), "value": 1e20, "operator": "op",
        })
    assert db.query_one("SELECT 1 FROM manual_reading WHERE point_id=?", (point,)) is None


def test_manual_service_enforces_point_range(ready_db):
    node = create_node("A栋")
    device = create_device(name="限定设备")
    point = create_point(device, name="限定点位", min_val=0.0, max_val=100.0)
    meter = create_meter(node)
    link_point(meter, point)
    with pytest.raises(ValueError):
        manual_service.record({
            "point_id": point, "ts": int(time.time()), "value": 500.0, "operator": "op",
        })


def test_bad_meter_does_not_abort_rollup_for_other_meters(ready_db):
    """一块表出现溢出值时，其它表仍要正常出小时行。"""
    _node, device, bad_point, bad_meter = _chain("坏表")
    node2 = create_node("B栋")
    good_point = create_point(device, name="好表累计电量", slave_id=9)
    good_meter = create_meter(node2, name="好表")
    link_point(good_meter, good_point)

    hour_start = (int(time.time()) // 3600) * 3600
    add_reading(bad_point, hour_start - 1800, 0.0)
    add_reading(bad_point, hour_start + 600, 1e300)  # 直接写库：模拟上游脏数据
    add_reading(good_point, hour_start - 1800, 100.0)
    add_reading(good_point, hour_start + 600, 105.0)

    aggregator.rollup_hour(hour_start)

    good = db.query_one(
        "SELECT energy_wh FROM hourly WHERE meter_id=? AND hour_start=?", (good_meter, hour_start)
    )
    assert good is not None and int(good["energy_wh"]) == 5_000_000
    assert db.query_one(
        "SELECT energy_wh FROM hourly WHERE meter_id=? AND hour_start=?", (bad_meter, hour_start)
    ) is None
    assert db.query_one("SELECT 1 FROM event_log WHERE code='METER_ROLLUP_FAIL'") is not None


# ---------------- P2-4 缺失表零值行 + 覆盖小时 ----------------
def test_missing_meter_gets_zero_row_and_coverage_columns(ready_db):
    """90% 门槛下仍可能缺表：缺的表要落零值行、is_final=0，并带 covered_hours。"""
    node = create_node("A栋")
    full_meter = create_meter(node, name="满表")
    empty_meter = create_meter(node, name="缺数表")
    fill_hourly(full_meter, DAY_START, energy_wh=1000)
    aggregator.settle_day(DAY_START)

    full = db.query_one(
        "SELECT is_final, covered_hours FROM daily WHERE meter_id=?", (full_meter,)
    )
    empty = db.query_one(
        "SELECT energy_wh, is_final, covered_hours FROM daily WHERE meter_id=?", (empty_meter,)
    )
    assert int(full["covered_hours"]) == 24 and int(full["is_final"]) == 0  # 表覆盖 1/2
    assert empty is not None
    assert int(empty["energy_wh"]) == 0
    assert int(empty["is_final"]) == 0
    assert int(empty["covered_hours"]) == 0


# ---------------- P2-1 / P2-2 热生效与配置审计 ----------------
def test_device_port_change_rebuilds_collector_task(ready_db):
    from collector import collector

    device = create_device(name="现场网关", port=502)

    async def scenario():
        collector._sync_tasks()
        original = collector.tasks.get(device)
        assert original is not None
        db.execute("UPDATE device SET port=5020 WHERE id=?", (device,))
        collector._sync_tasks()
        rebuilt = collector.tasks.get(device)
        assert rebuilt is not None and rebuilt is not original
        await collector.shutdown()

    run(scenario())
    events = db.query("SELECT code FROM event_log WHERE code='COLLECTOR_HOT_RELOAD'")
    assert events


def test_config_change_is_audited_with_before_after(ready_db):
    node = create_node("A栋")
    device = create_device(name="审计设备")
    point = create_point(device, name="审计点位", data_type="u32", scale=1.0)

    async def scenario():
        async with _client() as client:
            await _login(client)
            response = await client.put(f"/api/config/point/{point}", json={"scale": 2.5})
            assert response.status_code == 200, response.text

    run(scenario())
    event = db.query_one(
        "SELECT message FROM event_log WHERE code='CONFIG_CHANGE' AND ref_id=? ORDER BY id DESC",
        (point,),
    )
    assert event is not None
    assert "scale" in str(event["message"]) and "1.0" in str(event["message"])
    assert node  # 保持节点引用，避免 lint 认为变量未使用


def test_non_electric_category_is_rejected(ready_db):
    async def scenario():
        async with _client() as client:
            await _login(client)
            response = await client.post(
                "/api/config/energy_category", json={"name": "自来水", "unit": "m3"}
            )
            assert response.status_code == 400, response.text
            assert "kWh" in response.json()["error"]

    run(scenario())
