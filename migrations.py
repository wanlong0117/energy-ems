"""数据库版本化迁移：只前向、按序、可重复执行。

背景：v1/v2 只有 schema.sql 的 `CREATE TABLE IF NOT EXISTS`，旧库新增列不会自动补，
升级只能靠人工 ALTER。这里引入 `schema_version` 表 + 顺序迁移：
  - init_db() 先跑 schema.sql（新库一次建全），再 apply_migrations() 补齐旧库差异；
  - 每条迁移在独立事务内执行并记录版本，重复启动不会重复执行；
  - 迁移失败即抛错（不回退），避免半升级状态下继续对外服务。
新增列的迁移必须先查 PRAGMA table_info，因为老库与新建库的起点不同。
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

import db

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回表的现有列名集合。"""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """按需补列：已存在则跳过，保证迁移可重复执行。"""
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _v1_baseline(conn: sqlite3.Connection) -> None:
    """基线：v0.1.x 的 13 张表结构，由 schema.sql 负责，这里只登记版本。"""
    return None


def _v2_daily_price_snapshot(conn: sqlite3.Connection) -> None:
    """daily 增加电价快照：price_cent（加权平均单价）与 tariff_snapshot（JSON 明细）。"""
    _add_column(conn, "daily", "price_cent", "REAL")
    _add_column(conn, "daily", "tariff_snapshot", "TEXT")


def _v3_daily_index(conn: sqlite3.Connection) -> None:
    """日汇总按日查询的索引：报表按 day_start 区间扫描，原先只能走主键前缀。"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_day ON daily(day_start)")


def _v4_reading_ts_index(conn: sqlite3.Connection) -> None:
    """原始读数按时间索引：reading 主键是 (point_id, ts)，90 天清理与按时段扫描
    用不上主键前缀，只能全表扫——100 表 × 90 天可达数千万行。"""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reading_ts ON reading(ts)")


def _v5_is_demo(conn: sqlite3.Connection) -> None:
    """演示数据隔离：node/device/point/meter 增加 is_demo，并回填既有的「模拟-*」数据。

    背景（v0.1.3 复审 P0）：simulate 预置的 100 块表演示表切到 live 后仍在
    「启用 + 计入」，日结算覆盖率分母变成 101，真实表永远达不成 90% 门槛，
    日报全部停在「未结算」。靠名字前缀判断不可靠（用户可改名），因此落成显式列，
    并按历史命名回填一次，保证老库升级后也能被识别。
    """
    for table in ("node", "device", "point", "meter"):
        _add_column(conn, table, "is_demo", "INTEGER NOT NULL DEFAULT 0")
    for table in ("node", "device", "point", "meter"):
        conn.execute(f"UPDATE {table} SET is_demo=1 WHERE name LIKE '模拟-%' AND is_demo=0")


def _v6_daily_covered_hours(conn: sqlite3.Connection) -> None:
    """daily 增加 covered_hours：当日有多少小时有数据，报表据此标出可信度。

    背景（P2-4）：90% 门槛下有数据的表会被定稿，但缺失表不产行，报表直接求和
    就静默少计。补上每行覆盖小时数后，读表的人能看出「这块表只覆盖了 3 小时」。
    """
    _add_column(conn, "daily", "covered_hours", "INTEGER NOT NULL DEFAULT 0")
    # 老库的历史日报按 hourly 实际行数回填：新列默认 0 会让报表把历史日显示成
    # 「覆盖 0 小时」，比不显示更误导。当日口径按小时对齐（UTC 整点、左闭右开），
    # 因此直接数落在 [day_start, day_start+86400) 内的 hourly 行即可。
    conn.execute(
        """
        UPDATE daily SET covered_hours = (
            SELECT COUNT(*) FROM hourly h
             WHERE h.meter_id = daily.meter_id
               AND h.hour_start >= daily.day_start
               AND h.hour_start <  daily.day_start + 86400
        )
        """
    )


# 实体表定义（重建用）：与 schema.sql 保持一致，仅主键改为 AUTOINCREMENT
_REBUILD_TABLES: dict[str, str] = {
    "energy_category": """
CREATE TABLE {name} (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,
  unit  TEXT NOT NULL
)""",
    "node": """
CREATE TABLE {name} (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  parent_id   INTEGER REFERENCES node(id),
  kind        TEXT    NOT NULL DEFAULT 'area',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  is_active   INTEGER NOT NULL DEFAULT 1,
  is_demo     INTEGER NOT NULL DEFAULT 0,
  remark      TEXT
)""",
    "device": """
CREATE TABLE {name} (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  node_id       INTEGER REFERENCES node(id),
  protocol      TEXT    NOT NULL,
  host          TEXT    NOT NULL,
  port          INTEGER NOT NULL DEFAULT 502,
  timeout_ms    INTEGER NOT NULL DEFAULT 3000,
  retry         INTEGER NOT NULL DEFAULT 2,
  poll_interval INTEGER NOT NULL DEFAULT 60,
  is_enabled    INTEGER NOT NULL DEFAULT 1,
  is_demo       INTEGER NOT NULL DEFAULT 0,
  last_ok_at    INTEGER,
  last_error    TEXT,
  remark        TEXT
)""",
    "point": """
CREATE TABLE {name} (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT    NOT NULL,
  device_id    INTEGER NOT NULL REFERENCES device(id),
  slave_id     INTEGER NOT NULL,
  func_code    INTEGER NOT NULL DEFAULT 3,
  reg_offset   INTEGER NOT NULL,
  reg_count    INTEGER NOT NULL DEFAULT 2,
  data_type    TEXT    NOT NULL,
  byte_order   TEXT    NOT NULL DEFAULT 'ABCD',
  scale        REAL    NOT NULL DEFAULT 1,
  offset_const REAL    NOT NULL DEFAULT 0,
  min_val      REAL,
  max_val      REAL,
  unit         TEXT,
  point_role   TEXT    NOT NULL DEFAULT 'energy',
  is_enabled   INTEGER NOT NULL DEFAULT 1,
  is_demo      INTEGER NOT NULL DEFAULT 0,
  remark       TEXT,
  UNIQUE(device_id, slave_id, func_code, reg_offset)
)""",
    "meter": """
CREATE TABLE {name} (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  node_id       INTEGER NOT NULL REFERENCES node(id),
  category_id   INTEGER NOT NULL REFERENCES energy_category(id),
  purpose       TEXT    NOT NULL DEFAULT 'total',
  is_counted    INTEGER NOT NULL DEFAULT 1,
  daily_8am     INTEGER NOT NULL DEFAULT 1,
  is_enabled    INTEGER NOT NULL DEFAULT 1,
  is_demo       INTEGER NOT NULL DEFAULT 0,
  remark        TEXT
)""",
    "tariff": """
CREATE TABLE {name} (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  price_cent  INTEGER NOT NULL,
  start_hour  INTEGER,
  end_hour    INTEGER,
  is_default  INTEGER NOT NULL DEFAULT 0
)""",
}

_REBUILD_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_node_parent ON node(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_point_device ON point(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_meter_node ON meter(node_id)",
)

# 重建时按列名搬运数据：老库的 is_demo 是 ALTER TABLE 追加在末尾的，列顺序与
# 新建库不同，用 SELECT * 会把 remark 和 is_demo 对调。
_REBUILD_COLUMNS: dict[str, tuple[str, ...]] = {
    "energy_category": ("id", "name", "unit"),
    "node": ("id", "name", "parent_id", "kind", "sort_order", "is_active", "is_demo", "remark"),
    "device": (
        "id", "name", "node_id", "protocol", "host", "port", "timeout_ms", "retry",
        "poll_interval", "is_enabled", "is_demo", "last_ok_at", "last_error", "remark",
    ),
    "point": (
        "id", "name", "device_id", "slave_id", "func_code", "reg_offset", "reg_count",
        "data_type", "byte_order", "scale", "offset_const", "min_val", "max_val", "unit",
        "point_role", "is_enabled", "is_demo", "remark",
    ),
    "meter": (
        "id", "name", "node_id", "category_id", "purpose", "is_counted", "daily_8am",
        "is_enabled", "is_demo", "remark",
    ),
    "tariff": ("id", "name", "price_cent", "start_hour", "end_hour", "is_default"),
}


def _v7_autoincrement(conn: sqlite3.Connection) -> None:
    """实体主键改 AUTOINCREMENT，堵住「删除最高 ID 后重建继承旧读数」的路径。

    背景（P1-2）：SQLite 的 INTEGER PRIMARY KEY 在删除最大值后会复用该 ID，而
    reading/reading_latest/hourly/daily 都不带外键，删除 point/meter 也不会清理，
    于是重建的同号实体上线即带旧表底码与旧日报——比「没数据」更难发现。

    SQLite 不支持 ALTER 主键，只能重建表：拷贝 -> 删旧表 -> 改名。
    期间必须关闭外键（子表引用的是表名，重建后名字仍在，约束继续有效）；
    apply_migrations 会在本条前 PRAGMA foreign_keys=OFF、后恢复 ON。
    """
    for table, ddl in _REBUILD_TABLES.items():
        existing = _columns(conn, table)
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is not None and "AUTOINCREMENT" in str(row[0] or "").upper():
            continue  # 新库建表时已是自增，无需重建
        columns: list[str] = [col for col in _REBUILD_COLUMNS[table] if col in existing]
        column_sql: str = ", ".join(columns)
        temp: str = f"{table}_rebuild"
        conn.execute(f"DROP TABLE IF EXISTS {temp}")
        conn.execute(ddl.format(name=temp))
        conn.execute(f"INSERT INTO {temp} ({column_sql}) SELECT {column_sql} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {temp} RENAME TO {table}")
    for statement in _REBUILD_INDEXES:
        conn.execute(statement)


MIGRATIONS: list[Migration] = [
    (1, "baseline: 13 张表", _v1_baseline),
    (2, "daily 增加电价与价格快照字段", _v2_daily_price_snapshot),
    (3, "daily 增加 day_start 索引", _v3_daily_index),
    (4, "reading 增加 ts 索引", _v4_reading_ts_index),
    (5, "实体表增加 is_demo 并回填模拟数据", _v5_is_demo),
    (6, "daily 增加 covered_hours", _v6_daily_covered_hours),
    (7, "实体主键改 AUTOINCREMENT", _v7_autoincrement),
]

# 需要临时关闭外键的迁移：SQLite 不允许在事务内改 PRAGMA foreign_keys
FK_OFF_MIGRATIONS: frozenset[int] = frozenset({7})

SCHEMA_VERSION: int = MIGRATIONS[-1][0] if MIGRATIONS else 0


def current_version(conn: sqlite3.Connection) -> int:
    """读取已应用的最高版本号；无记录视为 0。"""
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0] or 0) if row else 0


def apply_migrations(conn: sqlite3.Connection) -> int:
    """按序执行未应用的迁移，返回本次应用的条数。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " description TEXT NOT NULL,"
        " applied_at INTEGER NOT NULL)"
    )
    applied: int = 0
    for version, description, action in MIGRATIONS:
        if version <= current_version(conn):
            continue
        fk_off: bool = int(version) in FK_OFF_MIGRATIONS
        conn.commit()  # PRAGMA 必须在事务外设置
        if fk_off:
            conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            action(conn)
            conn.execute(
                "INSERT INTO schema_version (version, description, applied_at) VALUES (?,?,?)",
                (int(version), description, int(time.time())),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            if fk_off:
                conn.execute("PRAGMA foreign_keys=ON")
        applied += 1
        db.log_event("info", "api", "DB_MIGRATED", f"数据库迁移到 v{version}：{description}")
    return applied


__all__ = [
    "FK_OFF_MIGRATIONS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "apply_migrations",
    "current_version",
]
