"""aggregation.py 验证：小时汇总、08:00(UTC 00:00) 日结算、电费与 purpose 拆分。"""
from __future__ import annotations

from datetime import datetime, timezone

import db
from aggregation import Aggregator
from helpers import (
    add_reading, create_device, create_meter, create_node, create_point,
    create_tariff, link_point,
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
    assert row["is_final"] == 1
    assert row["cost_cent"] == round(1.234 * 80)
    assert row["peak_power"] == 300.0
    assert row["avg_power"] == 200.0
    assert DAY_START % 86400 == 0  # UTC 00:00 == 本地 08:00

    aggregator.settle_day(DAY_START)
    assert len(db.query("SELECT 1 FROM daily WHERE meter_id=? AND day_start=?",
                        (meter_id, DAY_START))) == 1
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
