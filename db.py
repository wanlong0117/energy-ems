"""SQLite 连接与初始化。单进程架构下全局一个连接，check_same_thread=False 供 asyncio 使用。

v2：DB_PATH / SCHEMA_PATH 走 settings.app_dir()/resource_dir()，兼容 PyInstaller frozen 路径；
    测试夹具仍可在运行时 monkeypatch db.DB_PATH / db._conn（本模块每次调用均读模块级变量）。
v3：新增进程内可重入锁 _lock，所有访问串行化；多语句事务改用 transaction()，
    在独立连接上 BEGIN IMMEDIATE，避免与全局连接的隐式提交互相污染导致半写半不写。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from settings import app_dir, resource_dir

DB_PATH = app_dir() / "energy.db"
SCHEMA_PATH = resource_dir() / "schema.sql"

_conn: sqlite3.Connection | None = None
# 进程内可重入锁：单进程多线程（事件循环线程 + 线程池里的同步路由）共用一条连接，
# 读写的「执行 + 提交」必须成对持有，否则一个线程的提交会把另一个线程未提交的写入带走。
_lock: threading.RLock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    """返回全局连接，首次调用时创建并确保可写目录存在。"""
    global _conn
    with _lock:
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
    """建库 + 版本化迁移：旧库自动补列补索引，原数据不丢。"""
    from migrations import apply_migrations  # 延迟导入，避免 db <-> migrations 循环

    with _lock:
        conn = get_conn()
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        apply_migrations(conn)


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    """查询多行（短事务，不持有游标跨 await）。"""
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    """查询单行。"""
    with _lock:
        return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | list = ()) -> int:
    """执行写语句并立即提交，返回 lastrowid 或受影响行数。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or cur.rowcount


@contextmanager
def locked_conn() -> Iterator[sqlite3.Connection]:
    """全局连接 + 持锁执行多条写语句；调用方自行 commit/rollback。

    供采集批量写这类「多语句必须成组」的场景使用，避免在两次 executemany
    之间被其它线程的提交打断。
    """
    with _lock:
        yield get_conn()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """独立连接上的显式事务：持锁期间全局连接不会插入隐式提交。

    用法：
        with db.transaction() as conn:
            conn.execute(...)   # 多条语句同生共死
    退出时统一 commit；抛异常则 rollback。锁为可重入锁，事务内可继续调用
    db.query / log_event（同线程），不会自锁。
    """
    with _lock:
        conn: sqlite3.Connection = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


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


# ---------- 演示数据隔离（P0） ----------
def has_real_meter() -> bool:
    """是否存在「启用且计入」的真实表（is_demo=0）。

    判据是「有没有真表」而不是「当前是不是 live 模式」：一个纯演示实例
    （simulate）必须继续展示模拟数据，否则看板一进来就空；一旦接入了真表，
    演示数据就必须整体让位，否则 100 块模拟表会把日结覆盖率分母撑到 101。
    """
    return query_one(
        "SELECT 1 FROM meter WHERE is_enabled=1 AND is_counted=1 AND is_demo=0 LIMIT 1"
    ) is not None


def demo_guard(alias: str = "m") -> str:
    """演示数据过滤片段：有真表时排除演示表，纯演示实例不过滤。"""
    return "" if not has_real_meter() else f" AND {alias}.is_demo=0"


def disable_demo_data() -> dict[str, int]:
    """停用全部演示数据（切 live 时调用），返回各表停用条数。

    只停用不删除：现场还需要「切回 simulate 继续演示」的能力，
    删数据属于显式操作（scripts/clear_demo.py）。
    """
    counts: dict[str, int] = {}
    # node 的启用位叫 is_active，其余实体叫 is_enabled
    targets: tuple[tuple[str, str], ...] = (
        ("meter", "is_enabled"), ("point", "is_enabled"),
        ("device", "is_enabled"), ("node", "is_active"),
    )
    with transaction() as conn:
        for table, flag in targets:
            cursor = conn.execute(
                f"UPDATE {table} SET {flag}=0 WHERE is_demo=1 AND {flag}=1"
            )
            counts[table] = int(cursor.rowcount or 0)
    total: int = sum(counts.values())
    if total:
        log_event(
            "warn", "api", "DEMO_DISABLED",
            "切换到 live 模式，已自动停用演示数据："
            + "、".join(f"{table} {count} 条" for table, count in counts.items() if count)
            + "。如需彻底清理请执行 scripts/clear_demo.py",
        )
    return counts


# 节流状态：key = code|ref_id -> 上次落库时刻（单调时钟）与期间被折叠的条数
_log_throttle_at: dict[str, float] = {}
_log_suppressed: dict[str, int] = {}


def log_event_throttled(
    level: str,
    source: str,
    code: str,
    message: str,
    ref_id: int | None = None,
    window_sec: int | None = None,
) -> None:
    """同 (code, ref_id) 在窗口内只落一条，其余折叠计数并在下次落库时说明。

    采集失败、任务复活、查询截断这类事件在故障状态下是「每秒一条」的量级，
    event_log 上限 2 万条，几小时就能把真正的审计证据冲掉。折叠不改语义，
    只是把「连续 N 次同类」压成一条可读记录。
    """
    from settings import EVENT_LOG_THROTTLE_SEC  # 局部导入，避免 settings <-> db 循环

    span: int = int(window_sec if window_sec is not None else EVENT_LOG_THROTTLE_SEC)
    key: str = f"{code}|{ref_id if ref_id is not None else '-'}"
    now: float = time.monotonic()
    last: float | None = _log_throttle_at.get(key)
    if last is not None and now - last < span:
        _log_suppressed[key] = _log_suppressed.get(key, 0) + 1
        return
    folded: int = _log_suppressed.pop(key, 0)
    _log_throttle_at[key] = now
    if folded:
        message = f"{message}（此前 {span}s 内另有 {folded} 次同类事件已折叠）"
    log_event(level, source, code, message, ref_id)
