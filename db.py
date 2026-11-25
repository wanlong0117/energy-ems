"""SQLite 连接与初始化。单进程架构下全局一个连接，check_same_thread=False 供 asyncio 使用。

v2：DB_PATH / SCHEMA_PATH 走 settings.app_dir()/resource_dir()，兼容 PyInstaller frozen 路径；
    测试夹具仍可在运行时 monkeypatch db.DB_PATH / db._conn（本模块每次调用均读模块级变量）。
"""
from __future__ import annotations

import sqlite3
import time

from settings import app_dir, resource_dir

DB_PATH = app_dir() / "energy.db"
SCHEMA_PATH = resource_dir() / "schema.sql"

_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """返回全局连接，首次调用时创建并确保可写目录存在。"""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute("PRAGMA synchronous = NORMAL")
        _conn.execute("PRAGMA busy_timeout = 5000")
    return _conn


def init_db() -> None:
    """幂等执行 schema.sql：旧库自动补 manual_reading，原 12 表数据不丢。"""
    conn = get_conn()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    """查询多行（短事务，不持有游标跨 await）。"""
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    """查询单行。"""
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | list = ()) -> int:
    """执行写语句并立即提交，返回 lastrowid 或受影响行数。"""
    conn = get_conn()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid or cur.rowcount


def log_event(
    level: str,
    source: str,
    code: str | None,
    message: str,
    ref_id: int | None = None,
) -> None:
    """写审计/事件日志（source ∈ collector/api/user）。"""
    execute(
        "INSERT INTO event_log (ts, level, source, code, message, ref_id) VALUES (?,?,?,?,?,?)",
        (int(time.time()), level, source, code, message, ref_id),
    )
