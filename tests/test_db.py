"""db.py / schema.sql 验证：幂等建库、13 张表（v2 增 manual_reading）、默认值与事件日志。"""
from __future__ import annotations

import db

from helpers import create_device, create_point

EXPECTED_TABLES = {
    "node", "device", "point", "energy_category", "meter", "meter_point",
    "reading", "reading_latest", "hourly", "daily", "tariff", "event_log",
    "manual_reading",  # v2 第 13 张表
}


def test_init_db_is_idempotent_and_creates_thirteen_tables(isolated_db):
    # Arrange / Act：连续初始化两次
    db.init_db()
    db.init_db()
    rows = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )

    # Assert
    assert {row["name"] for row in rows} == EXPECTED_TABLES
    assert len(rows) == 13


def test_init_db_seeds_three_energy_categories(isolated_db):
    rows = db.query("SELECT id, name, unit FROM energy_category ORDER BY id")
    assert [(row["id"], row["name"], row["unit"]) for row in rows] == [
        (1, "电", "kWh"), (2, "水", "m3"), (3, "天然气", "m3"),
    ]


def test_point_byte_order_default_is_abcd(isolated_db):
    info = {
        row["name"]: row for row in db.query("PRAGMA table_info(point)")
    }
    assert info["byte_order"]["dflt_value"] == "'ABCD'"

    device_id = create_device()
    point_id = db.execute(
        "INSERT INTO point (name, device_id, slave_id, reg_offset, reg_count, data_type) "
        "VALUES ('默认字序', ?, 1, 10, 1, 'u16')",
        (device_id,),
    )
    row = db.query_one("SELECT byte_order FROM point WHERE id=?", (point_id,))
    assert row["byte_order"] == "ABCD"


def test_execute_and_log_event(isolated_db):
    assert db.execute("INSERT INTO node (name) VALUES (?)", ("测试节点",)) > 0
    db.log_event("warn", "api", "LOGIN_FAIL", "登录密码错误", 7)
    row = db.query_one("SELECT * FROM event_log ORDER BY id DESC LIMIT 1")
    assert row["level"] == "warn"
    assert row["source"] == "api"
    assert row["code"] == "LOGIN_FAIL"
    assert row["ref_id"] == 7


def test_reading_primary_key_is_point_id_and_ts(isolated_db):
    device_id = create_device()
    point_id = create_point(device_id)
    db.execute(
        "INSERT INTO reading (point_id, ts, value) VALUES (?,?,?)", (point_id, 100, 1.0)
    )
    db.execute(
        "INSERT OR REPLACE INTO reading (point_id, ts, value) VALUES (?,?,?)",
        (point_id, 100, 2.0),
    )
    rows = db.query("SELECT value FROM reading WHERE point_id=? AND ts=?", (point_id, 100))
    assert len(rows) == 1
    assert rows[0]["value"] == 2.0
