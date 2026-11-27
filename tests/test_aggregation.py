"""aggregation.py 验证：小时汇总、08:00(UTC 00:00) 日结算、电费与 purpose 拆分。"""
from __future__ import annotations

import pytest

from datetime import datetime, timezone

import db
from aggregation import Aggregator
from settings import MILLI_PER_KWH
from helpers import (
    add_reading, create_device, create_meter, create_node, create_point,
    create_tariff, fill_hourly, link_point,
)

DAY_START = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())
HOUR_END = 3600


def _setup_meter(purpose: str, node_name: str, delta_kwh: float) -> tuple[int, int]:
    node_id = create_node(name=node_name)
    device_id = create_device(name=f"网关-{node_name}")
    reading_point = create_point(device_id, name=f"{node_name}电量", reg_offset=0,
                                 reg_count=1, data_type="u16")
    power_point = create_point(device_id, name=f"{node_name}功率", reg_offset=1,
                               reg_count=1, data_type="u16", point_role="power")
    meter_id = create_meter(node_id, name=f"{node_name}表", purpose=purpose)
    link_point(meter_id, reading_point, "reading")
    link_point(meter_id, power_point, "power")
    add_reading(reading_point, DAY_START, 100.0)
    add_reading(reading_point, DAY_START + 1800, 100.0 + delta_kwh)
    for index, value in enumerate((100.0, 200.0, 300.0)):
        add_reading(power_point, DAY_START + index * 600, value)
    return meter_id, reading_point


def test_rollup_hour_computes_energy_power_and_is_idempotent(isolated_db):
    aggregator = Aggregator()
    meter_id, point_id = _setup_meter("total", "A栋", 1.234)
    rows = db.query("SELECT COUNT(*) AS cnt FROM reading WHERE point_id=?", (point_id,))
    assert rows[0]["cnt"] == 2

    aggregator.rollup_hour(DAY_START)
    row = db.query_one("SELECT * FROM hourly WHERE meter_id=? AND hour_start=?",
                       (meter_id, DAY_START))
    assert row is not None
    assert row["energy_wh"] == 1_234_000
    assert row["avg_power"] == 200.0
    assert row["max_power"] == 300.0
    assert row["sample_cnt"] == 2

    aggregator.rollup_hour(DAY_START)
    assert len(db.query("SELECT 1 FROM hourly WHERE meter_id=? AND hour_start=?",
                        (meter_id, DAY_START))) == 1


def test_settle_day_uses_utc_midnight_final_flag_and_cost(isolated_db):
    aggregator = Aggregator()
    create_tariff(price_cent=80, is_default=1)
    meter_id, _ = _setup_meter("total", "A栋", 1.234)

    aggregator.rollup_hour(DAY_START)
    aggregator.settle_day(DAY_START)
    row = db.query_one("SELECT * FROM daily WHERE meter_id=? AND day_start=?",
                       (meter_id, DAY_START))
    assert row is not None
    assert row["energy_wh"] == 1_234_000
    assert row["cost_cent"] == round(1.234 * 80)
    assert row["peak_power"] == 300.0
    assert row["avg_power"] == 200.0
    assert DAY_START % 86400 == 0  # UTC 00:00 == 本地 08:00

    # 只有 1 小时数据：表覆盖 1/1 达标，但单表小时覆盖 1/24 不足 90%，
    # 必须保留未结算并按表留痕（定稿门槛是按表判的，不是按全局小时并集）
    assert row["is_final"] == 0
    assert db.query_one(
        "SELECT 1 FROM event_log WHERE code='SETTLE_METER_INCOMPLETE'"
    ) is not None

    # 补齐 24 小时后重结算：定稿，且不重复写行
    fill_hourly(meter_id, DAY_START)
    aggregator.settle_day(DAY_START)
    assert len(db.query("SELECT 1 FROM daily WHERE meter_id=? AND day_start=?",
                        (meter_id, DAY_START))) == 1
    assert db.query_one(
        "SELECT is_final FROM daily WHERE meter_id=? AND day_start=?", (meter_id, DAY_START)
    )["is_final"] == 1
    assert db.query_one("SELECT 1 FROM event_log WHERE code='SETTLE_DONE'") is not None


def test_split_by_purpose_and_node(isolated_db):
    aggregator = Aggregator()
    create_tariff(price_cent=80, is_default=1)
    meter_a, _ = _setup_meter("total", "A栋", 1.234)
    meter_b, _ = _setup_meter("production", "B栋", 0.5)
    for meter_id in (meter_a, meter_b):
        aggregator.rollup_hour(DAY_START)
    aggregator.settle_day(DAY_START)

    summary = aggregator.split_by_purpose(DAY_START)
    assert len(summary) == 2
    by_purpose = {item["purpose"]: item for item in summary}
    assert by_purpose["total"]["energy_wh"] == 1_234_000
    assert by_purpose["total"]["cost_cent"] == 99
    assert by_purpose["production"]["energy_wh"] == 500_000
    assert by_purpose["production"]["cost_cent"] == 40
    assert by_purpose["total"]["node_id"] != by_purpose["production"]["node_id"]


def test_calc_cost_without_tariff_returns_zero(isolated_db):
    assert Aggregator().calc_cost(1_234_000, DAY_START) == 0


def test_meter_reset_is_clamped_and_logged(isolated_db):
    aggregator = Aggregator()
    meter_id, point_id = _setup_meter("total", "A栋", 1.234)
    add_reading(point_id, DAY_START + 2700, 1.0)  # 累计值回退
    aggregator.rollup_hour(DAY_START)
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 0  # 累计值回退按 0 计
    assert db.query_one("SELECT 1 FROM event_log WHERE code='METER_RESET'") is not None


# ---------------------------------------------------------------- 满量程翻转 / 换表
def _meter_with_readings(readings: list[tuple[int, float]], *, max_kwh: float | None = None) -> tuple[int, int]:
    """建一块表 + 一个累计电量点，按 (ts 偏移, 读数) 灌读数，可选设置量程。"""
    node_id = create_node(name="翻转楼")
    device_id = create_device(name="翻转网关")
    point_id = create_point(device_id, name="翻转电量", reg_offset=0, reg_count=2, data_type="u32")
    meter_id = create_meter(node_id, name="翻转表")
    link_point(meter_id, point_id, "reading")
    if max_kwh is not None:
        db.execute("UPDATE meter SET max_reading_kwh=? WHERE id=?", (max_kwh, meter_id))
    for offset, value in readings:
        add_reading(point_id, DAY_START + offset, value)
    return meter_id, point_id


def test_rollover_uses_configured_max_reading(isolated_db):
    """配置量程后，999→5 这类满量程归零必须按 (量程-旧)+新 补回，而不是记 0。"""
    # Arrange：量程 1000 kWh，本小时从 980 走到 5（翻过 1000 归零）
    meter_id, _ = _meter_with_readings(
        [(-600, 980.0), (600, 992.0), (1800, 5.0)], max_kwh=1000.0
    )
    # Act
    Aggregator().rollup_hour(DAY_START)
    # Assert：跨量程真实用量 = (1000-980) + 5 = 25 kWh
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 25 * MILLI_PER_KWH
    event = db.query_one("SELECT message FROM event_log WHERE code='METER_ROLLOVER'")
    assert event is not None and "1000" in event["message"]


def test_rollover_ignored_when_max_not_configured(isolated_db):
    """没配量程时不得擅自猜测：维持按 0 计，但事件里要给出现场可行动的提示。"""
    meter_id, _ = _meter_with_readings([(-600, 980.0), (1800, 5.0)])
    Aggregator().rollup_hour(DAY_START)
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 0
    event = db.query_one("SELECT message FROM event_log WHERE code='METER_RESET'")
    assert event is not None
    assert "max_reading_kwh" in event["message"], "提示必须告诉现场该填哪个字段"


def test_rollover_rejects_out_of_range_readings(isolated_db):
    """读数超出量程（量程填小了）时不做翻转修正，避免算出假电量。"""
    meter_id, _ = _meter_with_readings([(-600, 1200.0), (1800, 5.0)], max_kwh=1000.0)
    Aggregator().rollup_hour(DAY_START)
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 0


def test_meter_swap_uses_first_reading_in_window(isolated_db):
    """换表：旧表 5000 被拆，新表底数 3，本小时走 3→12，应记 9 kWh 而不是 0。"""
    meter_id, _ = _meter_with_readings([(-600, 5000.0), (600, 3.0), (1800, 12.0)])
    Aggregator().rollup_hour(DAY_START)
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 9 * MILLI_PER_KWH
    assert db.query_one("SELECT 1 FROM event_log WHERE code='METER_SWAP'") is not None


def test_meter_swap_never_writes_negative_energy(isolated_db):
    """读数在一小时内继续往下掉时，换表分支必须退化为 0，绝不写负电量。"""
    meter_id, _ = _meter_with_readings([(-600, 5000.0), (1200, 30.0), (1800, 12.0)])
    Aggregator().rollup_hour(DAY_START)
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 0
    assert db.query_one("SELECT 1 FROM event_log WHERE code='METER_RESET'") is not None


@pytest.mark.parametrize("bad_max", [0.0, -5.0])
def test_invalid_max_reading_falls_back_to_conservative_path(isolated_db, bad_max):
    """量程被写成 0/负数（绕过 API 直接改库）时，按「未配置」处理。"""
    meter_id, _ = _meter_with_readings([(-600, 980.0), (1800, 5.0)], max_kwh=bad_max)
    Aggregator().rollup_hour(DAY_START)
    row = db.query_one("SELECT energy_wh FROM hourly WHERE meter_id=?", (meter_id,))
    assert row["energy_wh"] == 0, f"量程 {bad_max} 不应触发翻转修正"
