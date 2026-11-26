"""v2 周/月按需聚合验证：周一/自然月起算、与区间 daily 求和误差 0、过滤项、不落新表。"""
from __future__ import annotations

from datetime import datetime, timezone

import db
from aggregation import Aggregator, month_start, week_start

from helpers import create_meter, create_node

DAY = 86400


def day_ts(year: int, month: int, day: int) -> int:
    """构造某日 UTC 00:00 时间戳（= 本地 08:00 口径）。"""
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def add_daily(meter_id: int, ts: int, energy_wh: int, cost_cent: int,
              final: int = 1, peak: float = 10.0) -> None:
    db.execute(
        "INSERT OR REPLACE INTO daily "
        "(meter_id, day_start, energy_wh, peak_power, avg_power, cost_cent, is_final) "
        "VALUES (?,?,?,?,?,?,?)",
        (meter_id, ts, energy_wh, peak, 5.0, cost_cent, final),
    )


def test_week_start_and_month_start_boundaries(isolated_db):
    """Arrange/Act: 不同日期求周首/月首。Assert: 周一 UTC00:00、月 1 日 UTC00:00。"""
    assert week_start(day_ts(2026, 1, 5)) == day_ts(2026, 1, 5)   # 周一
    assert week_start(day_ts(2026, 1, 7)) == day_ts(2026, 1, 5)   # 周三 -> 本周一
    assert week_start(day_ts(2026, 1, 11)) == day_ts(2026, 1, 5)  # 周日 -> 本周一
    assert week_start(day_ts(2026, 1, 12)) == day_ts(2026, 1, 12) # 下周一

    assert month_start(day_ts(2026, 1, 15)) == day_ts(2026, 1, 1)
    assert month_start(day_ts(2026, 1, 31)) == day_ts(2026, 1, 1)
    assert month_start(day_ts(2026, 2, 3)) == day_ts(2026, 2, 1)
    assert day_ts(2026, 1, 5) % DAY == 0  # 起点为 UTC 00:00


def test_weekly_aggregation_matches_daily_sum_and_filters(isolated_db):
    """Arrange: 构造跨两周/两用途 daily。Act: weekly()。Assert: 求和误差 0、周首正确、过滤生效。"""
    node_a = create_node(name="A栋")
    node_b = create_node(name="B栋")
    meter_a = create_meter(node_a, name="生产表", purpose="production")
    meter_b = create_meter(node_b, name="空调表", purpose="hvac")

    add_daily(meter_a, day_ts(2026, 1, 5), 100, 10)   # 周一
    add_daily(meter_a, day_ts(2026, 1, 6), 200, 20)
    add_daily(meter_a, day_ts(2026, 1, 7), 300, 30)
    add_daily(meter_a, day_ts(2026, 1, 12), 400, 40) # 下周一
    add_daily(meter_b, day_ts(2026, 1, 6), 500, 50)

    agg = Aggregator()
    rows = agg.weekly(from_ts=day_ts(2026, 1, 1), to_ts=day_ts(2026, 1, 31))
    week1 = day_ts(2026, 1, 5)

    # 与区间内全部 daily 求和误差 0
    daily_sum = int(db.query_one(
        "SELECT SUM(energy_wh) AS s FROM daily WHERE day_start BETWEEN ? AND ?",
        (day_ts(2026, 1, 1), day_ts(2026, 1, 31)))["s"])
    assert sum(r["energy_wh"] for r in rows) == daily_sum == 1500
    assert sum(r["cost_cent"] for r in rows) == 150

    by_key = {(r["period_start"], r["meter_id"]): r for r in rows}
    assert by_key[(week1, meter_a)]["energy_wh"] == 600   # 100+200+300
    assert by_key[(week1, meter_b)]["energy_wh"] == 500
    assert by_key[(day_ts(2026, 1, 12), meter_a)]["energy_wh"] == 400
    assert all(r["period_start"] % DAY == 0 for r in rows)  # 周首=UTC00:00
    assert all(r["days"] >= 1 for r in rows)

    # purpose / node_id / meter_id 过滤
    prod = agg.weekly(from_ts=day_ts(2026, 1, 1), to_ts=day_ts(2026, 1, 31), purpose="production")
    assert {r["meter_id"] for r in prod} == {meter_a}
    assert sum(r["energy_wh"] for r in prod) == 1000
    assert sum(r["energy_wh"] for r in agg.weekly(node_id=node_b)) == 500
    assert sum(r["energy_wh"] for r in agg.weekly(meter_id=meter_a)) == 1000


def test_weekly_is_final_is_all_days_final(isolated_db):
    """Arrange: 同周内混合 final。Act: weekly。Assert: 全部 final 才置 1。"""
    meter = create_meter(create_node(), name="混合表")
    add_daily(meter, day_ts(2026, 1, 5), 100, 10, final=1)
    add_daily(meter, day_ts(2026, 1, 6), 200, 20, final=0)
    row = Aggregator().weekly(from_ts=day_ts(2026, 1, 1), to_ts=day_ts(2026, 1, 31))[0]
    assert row["is_final"] == 0
    assert row["days"] == 2


def test_monthly_aggregation_matches_daily_sum_and_month_boundary(isolated_db):
    """Arrange: 构造跨两月 daily。Act: monthly。Assert: 自然月 1 日为首、求和误差 0、过滤生效。"""
    node = create_node(name="A栋")
    meter = create_meter(node, name="1号表", purpose="total")
    add_daily(meter, day_ts(2026, 1, 5), 100, 10)
    add_daily(meter, day_ts(2026, 1, 20), 200, 20)
    add_daily(meter, day_ts(2026, 2, 3), 300, 30)

    rows = Aggregator().monthly()
    jan = next(r for r in rows if r["period_start"] == day_ts(2026, 1, 1))
    feb = next(r for r in rows if r["period_start"] == day_ts(2026, 2, 1))
    assert jan["energy_wh"] == 300
    assert feb["energy_wh"] == 300
    assert sum(r["energy_wh"] for r in rows) == 600
    assert sum(r["energy_wh"] for r in rows) == int(db.query_one(
        "SELECT SUM(energy_wh) AS s FROM daily")["s"])
    assert all(r["period_start"] % DAY == 0 for r in rows)
    assert sum(r["energy_wh"] for r in Aggregator().monthly(purpose="total")) == 600
    assert sum(r["energy_wh"] for r in Aggregator().monthly(meter_id=meter)) == 600


def test_weekly_monthly_do_not_create_materialized_tables(isolated_db):
    """Arrange/Act: 调用周/月聚合。Assert: 不新增 weekly/monthly 等物化表（仍 13 张）。"""
    tables_before = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    Aggregator().weekly()
    Aggregator().monthly()
    tables_after = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables_after == tables_before
    # 13 张业务表 + schema_version（迁移版本记录，不属于业务物化表）
    assert len(tables_after - {"schema_version"}) == 13
    assert not {"weekly", "monthly", "weekly_summary"} & tables_after
