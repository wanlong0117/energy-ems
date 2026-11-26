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


MIGRATIONS: list[Migration] = [
    (1, "baseline: 13 张表", _v1_baseline),
    (2, "daily 增加电价与价格快照字段", _v2_daily_price_snapshot),
    (3, "daily 增加 day_start 索引", _v3_daily_index),
    (4, "reading 增加 ts 索引", _v4_reading_ts_index),
]

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
        applied += 1
        db.log_event("info", "api", "DB_MIGRATED", f"数据库迁移到 v{version}：{description}")
    return applied


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "apply_migrations", "current_version"]
