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
from pathlib import Path

from settings import app_dir, resource_dir

DB_PATH = app_dir() / "energy.db"
SCHEMA_PATH = resource_dir() / "schema.sql"

_conn: sqlite3.Connection | None = None
# 进程内可重入锁：单进程多线程（事件循环线程 + 线程池里的同步路由）共用一条连接，
# 读写的「执行 + 提交」必须成对持有，否则一个线程的提交会把另一个线程未提交的写入带走。
_lock: threading.RLock = threading.RLock()
# 当前线程是否正处在 transaction() 块内。用来把「事务里误用全局连接」从
# 「卡满 busy_timeout 再报 database is locked」变成一句能直接照做的错误。
_txn_state: threading.local = threading.local()


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
    """执行写语句并立即提交，返回 lastrowid 或受影响行数。

    ⚠️ 不得在 `with transaction() as conn:` 块内调用。transaction() 在独立连接上
    已用 BEGIN IMMEDIATE 占住写锁，这里再走全局连接写同一个库，SQLite 只认连接不认
    线程——同一线程会把自己堵死，卡满 busy_timeout 后抛 database is locked。
    事务内要写，请用块内那个 conn；要记审计日志，用 log_event_on(conn, ...)。
    """
    if getattr(_txn_state, "depth", 0) > 0:
        raise RuntimeError(
            "事务块内不能调用 db.execute / db.log_event："
            "transaction() 已持有写锁，全局连接再写会自锁到 busy_timeout。"
            "请改用 `with db.transaction() as conn:` 的 conn，"
            "或用 db.log_event_on(conn, ...) 记日志。"
        )
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
        # 需要留审计痕迹时：
        with db.transaction() as conn:
            conn.execute(...)
            db.log_event_on(conn, "info", "api", "X", "说明")   # 同一事务

    退出时统一 commit；抛异常则 rollback。

    不要在块内调用 db.execute / db.log_event。`_lock` 是可重入锁，所以那样做不会
    在锁上卡住，但会卡在 SQLite 的写锁上：本事务已 BEGIN IMMEDIATE，全局连接再写
    同一库只能等 busy_timeout（5 秒）然后报 database is locked。旧注释写成「可继续
    调用 db.query / log_event 不会自锁」是错的——query 在 WAL 下能读（读到的是事务
    外快照），log_event 一定自锁。现在这种误用会直接抛 RuntimeError。
    """
    with _lock:
        depth: int = int(getattr(_txn_state, "depth", 0))
        if depth > 0:
            # 嵌套会另开一条连接再 BEGIN IMMEDIATE，必然自锁；直接说清楚
            raise RuntimeError(
                "transaction() 不支持嵌套：外层已持有写锁，内层另开连接会自锁。请复用外层 conn。"
            )
        conn: sqlite3.Connection = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        _txn_state.depth = depth + 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            _txn_state.depth = depth
            conn.close()


def use_readonly(path: Path) -> None:
    """把全局连接切换成只读 URI（运维脚本 --dry-run 用）。

    只读模式由 SQLite 自己保证：写语句直接报 attempt to write a readonly database，
    比「代码里记得别写」可靠。也不会顺带执行 schema.sql / 迁移——那正是
    `--dry-run` 最不该做的事（升级后、备份前想看影响面，却先把库结构改了）。
    """
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        uri: str = f"{path.resolve().as_uri()}?mode=ro"
        _conn = sqlite3.connect(uri, uri=True)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout = 5000")


def log_event_on(
    conn: sqlite3.Connection,
    level: str,
    source: str,
    code: str | None,
    message: str,
    ref_id: int | None = None,
) -> None:
    """在事务块内记审计日志：复用调用方的连接，随事务一起提交/回滚。"""
    conn.execute(
        "INSERT INTO event_log (ts, level, source, code, message, ref_id) VALUES (?,?,?,?,?,?)",
        (int(time.time()), level, source, code, message, ref_id),
    )


def log_event(
    level: str,
    source: str,
    code: str | None,
    message: str,
    ref_id: int | None = None,
) -> None:
    """写审计/事件日志（source ∈ collector/api/user）。

    事务块内请改用 log_event_on(conn, ...)，否则会抛 RuntimeError（原因见 execute）。
    """
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
