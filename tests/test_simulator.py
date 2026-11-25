"""v2 内置模拟器验证：基线规模（100 电表 + 5 电极×3 点 + 5 手动点）、真实表产数、
累计单调、电极量程、以及 simulate/live 后台任务互斥。"""
from __future__ import annotations

import asyncio

import db
import main
from simulator import Simulator

from helpers import run


def _counts() -> dict[str, int]:
    """从真实表读取模拟器相关对象数量。"""
    return {
        "sim_meters": int(db.query_one(
            "SELECT COUNT(*) AS c FROM meter WHERE name LIKE '模拟-%'")["c"]),
        "sim_points": int(db.query_one(
            "SELECT COUNT(*) AS c FROM point WHERE name LIKE '模拟-%'")["c"]),
        "sim_nodes": int(db.query_one(
            "SELECT COUNT(*) AS c FROM node WHERE name LIKE '模拟-%'")["c"]),
        "elec_points": int(db.query_one(
            "SELECT COUNT(*) AS c FROM point WHERE name LIKE '模拟-冷却塔T%-电导率'")["c"]),
        "manual_points": int(db.query_one(
            "SELECT COUNT(*) AS c FROM point WHERE name LIKE '模拟-冷却塔T%-机械排水量'")["c"]),
        "manual_device": int(db.query_one(
            "SELECT COUNT(*) AS c FROM device WHERE protocol='manual' AND name LIKE '模拟-%'")["c"]),
    }


def test_ensure_baseline_creates_full_inventory_and_is_idempotent(isolated_db):
    """Arrange: 空库。Act: 两次 ensure_baseline。Assert: 100 表/220 点/电极与手动点齐全且幂等。"""
    simulator = Simulator(300, {})
    simulator.ensure_baseline()

    counts = _counts()
    assert counts["sim_meters"] == 100  # 4 栋 × 25
    assert counts["sim_nodes"] == 5     # 4 栋 + 冷却塔
    # 100 表 × 2 点 + 5 组电极 × 3 点 + 5 机械排水点 = 220
    assert counts["sim_points"] == 220
    assert counts["elec_points"] == 5
    assert counts["manual_points"] == 5
    assert counts["manual_device"] >= 1

    # 电表用途覆盖 production/hvac/lighting/other
    purposes = {row["purpose"] for row in db.query(
        "SELECT DISTINCT purpose FROM meter WHERE name LIKE '模拟-%'")}
    assert {"production", "hvac", "lighting", "other"}.issubset(purposes)

    # 电极点位字段：电导率 f32/μS/cm、电流 f32/A、排污阀 u16、量程 0/1
    for suffix, dtype, unit in (
        ("电导率", "f32", "μS/cm"),
        ("电流", "f32", "A"),
    ):
        row = db.query_one(
            "SELECT data_type, unit, min_val, max_val FROM point "
            "WHERE name LIKE ? LIMIT 1", (f"模拟-冷却塔T%-{suffix}",))
        assert row["data_type"] == dtype
        assert row["unit"] == unit
        assert row["min_val"] is not None and row["max_val"] is not None
    valve = db.query_one(
        "SELECT data_type, min_val, max_val FROM point "
        "WHERE name LIKE '模拟-冷却塔T%-排污阀' LIMIT 1")
    assert valve["data_type"] == "u16"
    assert (valve["min_val"], valve["max_val"]) == (0.0, 1.0)

    # 幂等：第二次不新增任何对象
    simulator.ensure_baseline()
    assert _counts() == counts


def test_tick_writes_real_tables_and_accumulation_is_strictly_monotonic(isolated_db):
    """Arrange: 预置基线。Act: 两个递增 ts 各 tick 一次。Assert: reading/latest 落真实表且累计严格递增。"""
    simulator = Simulator(300, {})
    simulator.ensure_baseline()
    first_meter_point = sorted(simulator.meter_points.values())[0]
    base_ts = 1_760_000_000

    simulator.tick(ts_override=base_ts)
    first = db.query_one(
        "SELECT value FROM reading WHERE point_id=? AND ts=?", (first_meter_point, base_ts))
    assert first is not None, "tick 必须写真实 reading 表"

    simulator.tick(ts_override=base_ts + 300)
    second = db.query_one(
        "SELECT value FROM reading WHERE point_id=? AND ts=?", (first_meter_point, base_ts + 300))
    assert second is not None
    assert second["value"] > first["value"], "累计电量必须严格单调增"

    # reading_latest 被更新到最新时间点
    latest = db.query_one(
        "SELECT ts, value FROM reading_latest WHERE point_id=?", (first_meter_point,))
    assert latest["ts"] == base_ts + 300
    assert latest["value"] == second["value"]
    # 单次 tick 写入所有 200 电表点 + 15 电极点（不含 5 个手动点）
    written = int(db.query_one(
        "SELECT COUNT(*) AS c FROM reading WHERE ts=?", (base_ts,))["c"])
    assert written == 215


def test_tick_electrode_values_stay_in_defined_ranges(isolated_db):
    """Arrange/Act: 多次 tick 电极点。Assert: 电导率 200~3000、电流 0~50、阀门 0/1。"""
    simulator = Simulator(300, {})
    simulator.ensure_baseline()
    base_ts = 1_760_000_000
    for step in range(6):
        simulator.tick(ts_override=base_ts + step * 300)

    for point_ids, low, high in (
        (simulator.conductivity_points, 200.0, 3000.0),
        (simulator.current_points, 0.0, 50.0),
    ):
        for point_id in point_ids:
            rows = db.query("SELECT value FROM reading WHERE point_id=?", (point_id,))
            assert rows and all(low <= float(r["value"]) <= high for r in rows)
    for point_id in simulator.valve_points:
        rows = db.query("SELECT value FROM reading WHERE point_id=?", (point_id,))
        assert rows and all(float(r["value"]) in (0.0, 1.0) for r in rows)


def test_manual_points_are_not_produced_by_simulator(isolated_db):
    """Arrange/Act: tick 产数。Assert: 机械排水点只由人工录入写，模拟器不产数。"""
    simulator = Simulator(300, {})
    simulator.ensure_baseline()
    simulator.tick(ts_override=1_760_000_000)
    for point_id in simulator.manual_points:
        assert db.query_one("SELECT 1 FROM reading WHERE point_id=?", (point_id,)) is None


def test_lifespan_starts_only_simulator_in_simulate_mode(isolated_db, monkeypatch):
    """Arrange: data_mode=simulate。Act: 进入 lifespan。Assert: 只启模拟器与模拟汇总，不启采集器。"""
    calls: list[str] = []

    class FakeSimulator:
        def __init__(self, interval_sec, config):
            calls.append("simulator.__init__")

        def ensure_baseline(self):
            calls.append("simulator.ensure_baseline")

        def start(self):
            calls.append("simulator.start")
            return _noop()

        def shutdown(self):
            pass

    async def _noop():
        await asyncio.sleep(3600)  # 常驻直到 lifespan 取消

    async def fake_collector_start():
        calls.append("collector.start")
        await _noop()

    async def fake_run_loop():
        calls.append("agg.run_loop")
        await _noop()

    async def fake_run_simulate_loop():
        calls.append("agg.run_simulate_loop")
        await _noop()

    monkeypatch.setattr(auth_mod(), "load_config", lambda: {
        "data_mode": "simulate", "simulate_interval_sec": 300, "simulate_rollup_sec": 600})
    monkeypatch.setattr(main, "Simulator", FakeSimulator)
    monkeypatch.setattr(main.collector, "start", fake_collector_start)
    monkeypatch.setattr(main.aggregator, "run_loop", fake_run_loop)
    monkeypatch.setattr(main.aggregator, "run_simulate_loop", fake_run_simulate_loop)

    async def scenario():
        async with main.lifespan(main.app):
            await asyncio.sleep(0.02)  # 让 create_task 的协程体真正执行

    run(scenario())
    assert "simulator.start" in calls
    assert "simulator.ensure_baseline" in calls
    assert "agg.run_simulate_loop" in calls
    assert "agg.run_loop" in calls
    assert "collector.start" not in calls, "simulate 模式绝不能启动 Collector"


def test_lifespan_starts_only_collector_in_live_mode(isolated_db, monkeypatch):
    """Arrange: data_mode=live。Act: 进入 lifespan。Assert: 只启采集器，不建模拟器、不启模拟汇总。"""
    calls: list[str] = []

    class FakeSimulator:
        def __init__(self, interval_sec, config):
            calls.append("simulator.__init__")
        def ensure_baseline(self):
            pass
        def start(self):
            calls.append("simulator.start")
            return _noop()
        def shutdown(self):
            pass

    async def _noop():
        await asyncio.sleep(3600)

    async def fake_collector_start():
        calls.append("collector.start")
        await _noop()

    async def fake_run_loop():
        calls.append("agg.run_loop")
        await _noop()

    async def fake_run_simulate_loop():
        calls.append("agg.run_simulate_loop")
        await _noop()

    monkeypatch.setattr(auth_mod(), "load_config", lambda: {"data_mode": "live"})
    monkeypatch.setattr(main, "Simulator", FakeSimulator)
    monkeypatch.setattr(main.collector, "start", fake_collector_start)
    monkeypatch.setattr(main.aggregator, "run_loop", fake_run_loop)
    monkeypatch.setattr(main.aggregator, "run_simulate_loop", fake_run_simulate_loop)

    async def scenario():
        async with main.lifespan(main.app):
            await asyncio.sleep(0.02)

    run(scenario())
    assert "collector.start" in calls
    assert "simulator.__init__" not in calls, "live 模式绝不能创建 Simulator"
    assert "simulator.start" not in calls
    assert "agg.run_simulate_loop" not in calls
    assert "agg.run_loop" in calls


def auth_mod():
    """返回 main 模块实际引用的 auth 模块对象（便于 monkeypatch 同一实例）。"""
    import auth
    return auth
