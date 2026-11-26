"""迁移回归：把「v0.1.3 老库」升级到 v0.1.4，验证补列、重建与数据不丢。

老库由当前 schema.sql 反向退化得到（去掉 is_demo / covered_hours / AUTOINCREMENT），
这样用例不依赖 git 历史，且能真实模拟「ALTER TABLE 追加列后列序与新建库不同」的现场状态。
"""
from __future__ import annotations

import re
import sqlite3

import db
import migrations
from db import SCHEMA_PATH


def _legacy_schema_text() -> str:
    """把当前 schema 退化成 v0.1.3 的形态。"""
    text: str = SCHEMA_PATH.read_text(encoding="utf-8")
    text = text.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER PRIMARY KEY")
    lines: list[str] = [
        line for line in text.splitlines()
        if "is_demo" not in line and "covered_hours" not in line
    ]
    return "\n".join(lines)


def _build_legacy_db(path) -> None:
    """建一个 v0.1.3 形态的库，带 schema_version 1~4 与样本数据。"""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_legacy_schema_text())
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at INTEGER NOT NULL)"
        )
        for version in (1, 2, 3, 4):
            conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?,?,0)",
                (version, f"legacy {version}"),
            )
        # 演示数据（靠名字前缀，迁移后应被回填 is_demo=1）
        conn.execute(
            "INSERT INTO node (id, name, kind, sort_order, is_active, remark) "
            "VALUES (1, '模拟-A栋', 'area', 0, 1, '演示节点')"
        )
        conn.execute(
            "INSERT INTO node (id, name, kind, sort_order, is_active, remark) "
            "VALUES (2, '车间栋', 'area', 0, 1, '现场节点')"
        )
        conn.execute(
            "INSERT INTO device (id, name, node_id, protocol, host, port) "
            "VALUES (1, '模拟-A栋网关', 1, 'manual', '127.0.0.1', 502)"
        )
        conn.execute(
            "INSERT INTO device (id, name, node_id, protocol, host, port) "
            "VALUES (2, '现场网关', 2, 'modbus_tcp', '192.0.2.10', 502)"
        )
        conn.execute(
            "INSERT INTO point (id, name, device_id, slave_id, func_code, reg_offset, "
            "reg_count, data_type, byte_order, remark) "
            "VALUES (1, '模拟-A栋-01号电表-累计电量', 1, 1, 3, 0, 4, 'u64', 'ABCD', '演示点位')"
        )
        conn.execute(
            "INSERT INTO point (id, name, device_id, slave_id, func_code, reg_offset, "
            "reg_count, data_type, byte_order, remark) "
            "VALUES (2, '1号总表-累计电量', 2, 1, 3, 0, 4, 'u64', 'ABCD', '现场点位')"
        )
        conn.execute(
            "INSERT INTO meter (id, name, node_id, category_id, purpose, is_counted, "
            "daily_8am, is_enabled, remark) "
            "VALUES (1, '模拟-A栋-01号电表', 1, 1, 'total', 1, 1, 1, '演示表')"
        )
        conn.execute(
            "INSERT INTO meter (id, name, node_id, category_id, purpose, is_counted, "
            "daily_8am, is_enabled, remark) "
            "VALUES (2, '1号总表', 2, 1, 'production', 1, 1, 1, '现场表')"
        )
        conn.execute("INSERT INTO meter_point (meter_id, point_id, role) VALUES (2, 2, 'reading')")
        conn.execute(
            "INSERT INTO reading (point_id, ts, value, quality) VALUES (2, 1767225600, 100.0, 0)"
        )
        conn.execute(
            "INSERT INTO reading_latest (point_id, ts, value, quality) VALUES (2, 1767225600, 100.0, 0)"
        )
        conn.execute(
            "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) "
            "VALUES (2, 1767225600, 1000000, 12)"
        )
        conn.execute(
            "INSERT INTO daily (meter_id, day_start, energy_wh, is_final, price_cent) "
            "VALUES (2, 1767225600, 1000000, 1, 80.0)"
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_database_upgrades_without_data_loss(tmp_path, monkeypatch):
    """升级后：补列到位、演示标记回填、remark 不被列序错位覆盖、主键改为自增。"""
    legacy_path = tmp_path / "legacy.db"
    _build_legacy_db(legacy_path)

    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "DB_PATH", legacy_path)
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()
    try:
        assert migrations.current_version(db.get_conn()) == migrations.SCHEMA_VERSION

        # 1) 补列成功
        for table in ("node", "device", "point", "meter"):
            columns = {
                str(row[1]) for row in db.get_conn().execute(f"PRAGMA table_info({table})")
            }
            assert "is_demo" in columns, f"{table} 缺少 is_demo"
        daily_columns = {
            str(row[1]) for row in db.get_conn().execute("PRAGMA table_info(daily)")
        }
        assert "covered_hours" in daily_columns

        # 2) 演示数据按命名回填，现场数据保持 0
        demo = db.query_one("SELECT is_demo, remark FROM meter WHERE id=1")
        real = db.query_one("SELECT is_demo, remark FROM meter WHERE id=2")
        assert int(demo["is_demo"]) == 1 and str(demo["remark"]) == "演示表"
        assert int(real["is_demo"]) == 0 and str(real["remark"]) == "现场表"

        # 3) 重建表按列名搬运：remark 与其它字段都不能错位
        point_row = db.query_one("SELECT name, remark, data_type FROM point WHERE id=2")
        assert str(point_row["remark"]) == "现场点位"
        assert str(point_row["data_type"]) == "u64"
        device_row = db.query_one("SELECT host, name FROM device WHERE id=2")
        assert str(device_row["host"]) == "192.0.2.10"

        # 4) 历史汇总与读数一条不少
        assert int(db.query_one("SELECT COUNT(*) AS c FROM reading")["c"]) == 1
        assert int(db.query_one("SELECT energy_wh FROM daily WHERE meter_id=2")["energy_wh"]) == 1_000_000

        # 5) covered_hours 按 hourly 实际行数回填：新列留 0 会让报表把历史日显示成
        #    「覆盖 0 小时」，比不显示更误导。此处当日只有 1 条 hourly，应回填 1。
        covered = db.query_one("SELECT covered_hours FROM daily WHERE meter_id=2")
        assert int(covered["covered_hours"]) == 1, "老日报的覆盖小时必须按 hourly 回填"

        # 6) 主键改为自增：删掉最大 ID 后新建不会复用
        db.execute("DELETE FROM meter_point WHERE meter_id=2")
        db.execute("DELETE FROM meter WHERE id=2")
        new_meter = db.execute(
            "INSERT INTO meter (name, node_id, category_id) VALUES ('换表后总表', 2, 1)"
        )
        assert new_meter > 2
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = None


def test_new_database_is_already_autoincrement(tmp_path, monkeypatch):
    """新库建表即自增，迁移 7 应跳过重建而直接登记版本。"""
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    monkeypatch.setattr(db, "_conn", None)
    db.init_db()
    try:
        sql = db.query_one(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='meter'"
        )["sql"]
        assert re.search(r"PRIMARY KEY AUTOINCREMENT", str(sql).upper())
        assert migrations.current_version(db.get_conn()) == migrations.SCHEMA_VERSION
    finally:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
