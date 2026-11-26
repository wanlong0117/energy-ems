"""结构一致性守卫：迁移里的「重建表定义」必须与白名单、schema.sql 三方对齐。

背景：v7 迁移要把实体表重建成 AUTOINCREMENT，做法是「建临时表 → 按列名搬运 →
删旧表 → 改名」。搬运用的列名清单 `_REBUILD_COLUMNS` 是手写的，一旦漏了新加的列
（v0.1.5 加 `meter.shutdown_power_w` 时差点漏掉），未来某次重建就会**静默丢列**：
重建成功、服务照常启动，只是那一列的数据没了。

这类错误没法靠运行时断言发现，只能靠结构测试钉住。三方对齐的判据：
  1. `_REBUILD_TABLES[table]` 的 DDL 列集合
  2. `_REBUILD_COLUMNS[table]` 的白名单列集合
  3. `schema.sql` 里同一张表的列集合（新建库的真实形态）
"""
from __future__ import annotations

import sqlite3

import db
import migrations


def _ddl_columns(conn: sqlite3.Connection, table: str, ddl: str) -> set[str]:
    """在空库里建出这条 DDL，返回它的列名集合。"""
    name: str = f"probe_{table}"
    conn.execute(ddl.format(name=name))
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def _schema_sql_columns() -> dict[str, set[str]]:
    """用 schema.sql 建一个内存库，返回实体表的列名集合。"""
    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    try:
        conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
        result: dict[str, set[str]] = {}
        for table in migrations._REBUILD_TABLES:
            result[table] = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
        return result
    finally:
        conn.close()


def test_rebuild_whitelist_covers_every_ddl_column() -> None:
    """白名单必须与重建 DDL 的列完全一致（漏列 = 重建时丢数据）。"""
    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    try:
        for table, ddl in migrations._REBUILD_TABLES.items():
            ddl_columns: set[str] = _ddl_columns(conn, table, ddl)
            whitelist: set[str] = set(migrations._REBUILD_COLUMNS[table])
            assert whitelist == ddl_columns, (
                f"{table}: 白名单与 DDL 不一致；"
                f"只在 DDL: {sorted(ddl_columns - whitelist)}，"
                f"只在白名单: {sorted(whitelist - ddl_columns)}"
            )
    finally:
        conn.close()


def test_schema_sql_matches_rebuild_definition() -> None:
    """schema.sql（新建库）与重建 DDL（老库升级）必须给出同一组列。

    两者不一致时，新装实例与升级实例的表结构会悄悄分叉：同一份代码在两种库上
    跑出不同结果，最难排查。
    """
    schema_columns: dict[str, set[str]] = _schema_sql_columns()
    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    try:
        for table, ddl in migrations._REBUILD_TABLES.items():
            rebuild: set[str] = _ddl_columns(conn, table, ddl)
            assert rebuild == schema_columns[table], (
                f"{table}: schema.sql 与重建 DDL 不一致；"
                f"只在 schema.sql: {sorted(schema_columns[table] - rebuild)}，"
                f"只在 DDL: {sorted(rebuild - schema_columns[table])}"
            )
    finally:
        conn.close()
