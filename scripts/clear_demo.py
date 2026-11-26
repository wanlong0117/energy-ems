"""清理模拟器预置的演示数据（表 / 点位 / 设备 / 节点）及其历史汇总。

为什么需要：simulate 模式预置 4 栋×25 块「模拟-*」电表，切到 live 后即使已自动
停用，历史日报与原始读数仍留在库里；报表一旦混入演示数据，现场对账会直接失真。
本脚本按 is_demo 标记整条链路删除，不依赖名称前缀。

用法：
    python scripts/clear_demo.py --db energy.db --dry-run   # 先看会删掉什么
    python scripts/clear_demo.py --db energy.db             # 真正清理

安全约定：
  · 只删 is_demo=1 的实体与挂在它们名下的数据，真实表一条不动；
  · 单事务执行，失败整体回滚；执行前请先备份（scripts/backup.py）；
  · 清理后用 scripts/recompute.py 重算受影响的统计日。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Windows 控制台默认按 cp1252 编码，打印中文会抛 UnicodeEncodeError，把整个 CI 步骤
# 直接跑挂（本地 macOS/Linux 是 UTF-8，永远复现不出来）。stdout / stderr 都定成 UTF-8 并
# 用 replace 兜底：日志里最多出现问号，不会因为一句话把构建/清理停掉。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402

# 删除顺序：子表在前、实体在后，避免外键挡住（point/meter 仍有 FK 引用）。
# 模板只写 WHERE 子句，DELETE 与 SELECT COUNT 由同一份条件生成——统计绝不能再执行
# DELETE（sqlite3 的 DELETE 不返回行，fetchone() 拿到 None 直接崩；若连接是自动提交的，
# --dry-run 还会顺手把数据删掉）。
_STEP_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("hourly", "meter_id IN ({meters})"),
    ("daily", "meter_id IN ({meters})"),
    ("meter_point", "meter_id IN ({meters}) OR point_id IN ({points})"),
    ("meter", "id IN ({meters})"),
    ("manual_reading", "point_id IN ({points})"),
    ("reading", "point_id IN ({points})"),
    ("reading_latest", "point_id IN ({points})"),
    ("point", "id IN ({points})"),
    ("device", "is_demo=1"),
    ("node", "is_demo=1"),
)


def _ids(conn: sqlite3.Connection) -> tuple[list[int], list[int]]:
    meters = [int(row[0]) for row in conn.execute("SELECT id FROM meter WHERE is_demo=1")]
    points = [int(row[0]) for row in conn.execute("SELECT id FROM point WHERE is_demo=1")]
    return meters, points


def _steps(
    meters: list[int], points: list[int]
) -> list[tuple[str, str, str, tuple[int, ...]]]:
    """把模板渲染成 (表名, DELETE 语句, COUNT 语句, 参数)；空集合用 NULL 占位，保证语句合法且不误删。"""
    meter_sql: str = ",".join("?" for _ in meters) or "NULL"
    point_sql: str = ",".join("?" for _ in points) or "NULL"
    steps: list[tuple[str, str, str, tuple[int, ...]]] = []
    for name, where in _STEP_TEMPLATES:
        clause: str = where.format(meters=meter_sql, points=point_sql)
        params: list[int] = []
        if "{meters}" in where:
            params.extend(meters)
        if "{points}" in where:
            params.extend(points)
        steps.append((
            name,
            f"DELETE FROM {name} WHERE {clause}",
            f"SELECT COUNT(*) FROM {name} WHERE {clause}",
            tuple(params),
        ))
    return steps


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """统计将被清理的行数（只读，绝不执行 DELETE）。"""
    meters, points = _ids(conn)
    counted: dict[str, int] = {}
    for name, _delete_sql, count_sql, params in _steps(meters, points):
        row = conn.execute(count_sql, params).fetchone()
        counted[name] = int(row[0]) if row is not None else 0
    counted["meter"] = len(meters)
    counted["point"] = len(points)
    return counted


def clear(conn: sqlite3.Connection) -> dict[str, int]:
    """按依赖顺序删除演示数据，返回各表删除行数。"""
    meters, points = _ids(conn)
    stats: dict[str, int] = {}
    for name, delete_sql, _count_sql, params in _steps(meters, points):
        cursor = conn.execute(delete_sql, params)
        stats[name] = int(cursor.rowcount or 0)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理模拟器预置的演示数据")
    parser.add_argument("--db", default=str(ROOT / "energy.db"), help="数据库文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return 2
    db.DB_PATH = db_path
    db.init_db()

    real_row = db.query_one(
        "SELECT COUNT(*) AS cnt FROM meter WHERE is_demo=0 AND is_enabled=1 AND is_counted=1"
    )
    real_meters: int = int(real_row["cnt"] or 0) if real_row is not None else 0

    conn = db.get_conn()
    planned = _counts(conn)
    print(f"数据库      : {db_path}")
    print(f"真实表数量  : {real_meters}（不会被清理）")
    print("将清理      : " + "，".join(f"{table} {count} 行" for table, count in planned.items()))
    if args.dry_run:
        print("\n--dry-run：未删除任何数据。去掉该参数即可执行清理。")
        return 0
    if not any(planned.values()):
        print("没有需要清理的演示数据。")
        return 0

    with db.transaction() as txn:
        stats = clear(txn)
    db.log_event(
        "warn", "api", "DEMO_CLEARED",
        "已清理演示数据：" + "，".join(f"{table} {count} 行" for table, count in stats.items() if count),
    )
    print("清理完成    : " + "，".join(f"{table} {count} 行" for table, count in stats.items() if count))
    if real_meters:
        print("提示：受影响的统计日请执行 scripts/recompute.py 按新口径重算。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
