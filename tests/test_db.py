"""db.py / schema.sql 验证：幂等建库、13 张业务表 + 版本表、默认值与迁移。"""
from __future__ import annotations

import db
from migrations import SCHEMA_VERSION

from helpers import create_device, create_point

EXPECTED_TABLES = {
    "node", "device", "point", "energy_category", "meter", "meter_point",
    "reading", "reading_latest", "hourly", "daily", "tariff", "event_log",
    "manual_reading",  # v2 第 13 张表
}
VERSION_TABLE = "schema_version"


def test_init_db_is_idempotent_and_creates_tables(isolated_db):
    # Arrange / Act：连续初始化两次
    db.init_db()
    db.init_db()
    rows = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )

    # Assert：13 张业务表 + 迁移版本表
    names = {row["name"] for row in rows}
    assert names == EXPECTED_TABLES | {VERSION_TABLE}
    assert len(names) == 14


def test_migrations_record_latest_version_once(isolated_db):
    """迁移必须登记到最新版本，且重复 init 不会重复应用。"""
    db.init_db()
    applied = db.query("SELECT version FROM schema_version ORDER BY version")
    assert [int(row["version"]) for row in applied] == list(range(1, SCHEMA_VERSION + 1))
    db.init_db()
    assert db.query_one("SELECT COUNT(*) AS c FROM schema_version")["c"] == SCHEMA_VERSION


def test_migration_adds_price_columns_to_legacy_daily(isolated_db):
    """旧库（daily 无 price_cent/tariff_snapshot）升级后自动补列。"""
    db.execute("DROP TABLE daily")
    db.execute(
        "CREATE TABLE daily (meter_id INTEGER NOT NULL, day_start INTEGER NOT NULL,"
        " energy_wh INTEGER NOT NULL, peak_power REAL, avg_power REAL, cost_cent INTEGER,"
        " is_final INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (meter_id, day_start)) WITHOUT ROWID"
    )
    db.execute("DELETE FROM schema_version WHERE version>=2")
    db.init_db()
    columns = {row["name"] for row in db.query("PRAGMA table_info(daily)")}
    assert {"price_cent", "tariff_snapshot"} <= columns


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
