"""`scripts/clear_demo.py` 回归：dry-run 只统计不落删，正式执行只动 is_demo=1。

为什么值得单独测：这脚本是运维在真实库里跑的「删除」脚本，一次误删就是现场数据
无法对账。历史上 `_counts` 用 DELETE 语句去统计行数——sqlite3 的 DELETE 不返回行，
`fetchone()` 拿到 None 直接崩；若连接恰好是自动提交，`--dry-run` 会顺手把数据删光。
"""
from __future__ import annotations

import db
import pytest
from helpers import create_device, create_meter, create_node, create_point, link_point
from scripts import clear_demo

TABLES = ("node", "device", "point", "meter", "meter_point", "reading", "hourly", "daily")


def _counts() -> dict[str, int]:
    return {
        table: int(db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"])
        for table in TABLES
    }


def _seed() -> tuple[int, int]:
    """建一条演示链路（is_demo=1）与一条真实链路，返回 (演示表, 真实表)。"""
    real_node = create_node("现场车间")
    real_device = create_device(host="192.0.2.10")
    real_point = create_point(real_device, name="1号总表-累计电量")
    real_meter = create_meter(real_node, name="1号总表")
    link_point(real_meter, real_point)
    db.execute(
        "INSERT INTO reading (point_id, ts, value, quality) VALUES (?,?,?,0)",
        (real_point, 1767225600, 100.0),
    )
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,12)",
        (real_meter, 1767225600, 1_000_000),
    )
    db.execute(
        "INSERT INTO daily (meter_id, day_start, energy_wh, is_final) VALUES (?,?,?,1)",
        (real_meter, 1767225600, 1_000_000),
    )

    demo_node = create_node("模拟-A栋")
    demo_device = create_device(name="模拟-A栋网关")
    demo_point = create_point(demo_device, name="模拟-A栋-01号电表-累计电量")
    demo_meter = create_meter(demo_node, name="模拟-A栋-01号电表")
    link_point(demo_meter, demo_point)
    for table in ("node", "device", "point", "meter"):
        db.execute(f"UPDATE {table} SET is_demo=1 WHERE id=?", (
            {"node": demo_node, "device": demo_device,
             "point": demo_point, "meter": demo_meter}[table],
        ))
    db.execute(
        "INSERT INTO reading (point_id, ts, value, quality) VALUES (?,?,?,0)",
        (demo_point, 1767225600, 200.0),
    )
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,12)",
        (demo_meter, 1767225600, 2_000_000),
    )
    db.execute(
        "INSERT INTO daily (meter_id, day_start, energy_wh, is_final) VALUES (?,?,?,1)",
        (demo_meter, 1767225600, 2_000_000),
    )
    return demo_meter, real_meter


def test_dry_run_reports_without_deleting(ready_db, capsys):
    """--dry-run 必须报出待删行数，且一行都不许少。"""
    # Arrange
    _seed()
    before = _counts()

    # Act
    exit_code = clear_demo.main(["--db", str(db.DB_PATH), "--dry-run"])
    output = capsys.readouterr().out

    # Assert
    assert exit_code == 0
    assert "将清理" in output and "meter 1 行" in output
    assert "未删除任何数据" in output
    assert _counts() == before, "dry-run 不允许改动任何表"


def test_clear_keeps_real_meter_and_its_history(ready_db):
    """正式执行：演示链路连历史一起删，真实表与其读数/日报一条不动，并写事件日志。"""
    # Arrange
    demo_meter, real_meter = _seed()

    # Act
    exit_code = clear_demo.main(["--db", str(db.DB_PATH)])
    assert exit_code == 0

    # Assert：真实数据完整
    assert db.query_one("SELECT id FROM meter WHERE id=?", (real_meter,)) is not None
    assert int(db.query_one(
        "SELECT COUNT(*) AS c FROM reading WHERE point_id IN "
        "(SELECT point_id FROM meter_point WHERE meter_id=?)", (real_meter,)
    )["c"]) >= 1
    assert int(db.query_one(
        "SELECT energy_wh FROM daily WHERE meter_id=?", (real_meter,)
    )["energy_wh"]) == 1_000_000

    # 演示数据连历史一起清掉
    assert db.query_one("SELECT id FROM meter WHERE id=?", (demo_meter,)) is None
    assert int(db.query_one("SELECT COUNT(*) AS c FROM meter WHERE is_demo=1")["c"]) == 0
    assert int(db.query_one("SELECT COUNT(*) AS c FROM point WHERE is_demo=1")["c"]) == 0
    assert int(db.query_one("SELECT COUNT(*) AS c FROM node WHERE is_demo=1")["c"]) == 0
    assert int(db.query_one(
        "SELECT COUNT(*) AS c FROM hourly WHERE meter_id=?", (demo_meter,)
    )["c"]) == 0

    # 留痕
    logged = db.query_one(
        "SELECT message FROM event_log WHERE code='DEMO_CLEARED' ORDER BY id DESC LIMIT 1"
    )
    assert logged is not None and "meter" in str(logged["message"])


def test_clear_with_nothing_to_do_is_noop(ready_db, capsys):
    """库里没有演示数据时正常退出，不写 DEMO_CLEARED（避免事件日志噪音）。"""
    # Arrange：只留真实链路
    node = create_node("现场车间")
    meter = create_meter(node, name="1号总表")
    assert meter > 0

    # Act
    exit_code = clear_demo.main(["--db", str(db.DB_PATH)])
    output = capsys.readouterr().out

    # Assert
    assert exit_code == 0
    assert "没有需要清理的演示数据" in output
    assert db.query_one("SELECT id FROM event_log WHERE code='DEMO_CLEARED'") is None
