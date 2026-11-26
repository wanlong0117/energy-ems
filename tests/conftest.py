"""全局测试夹具：每个测试使用独立的临时 SQLite 文件，不污染工程目录。"""
from __future__ import annotations

import pytest

import auth
import db
from collector import collector


def _reset_module_state() -> None:
    """清掉跨用例残留的进程内状态。

    · 事件日志节流：同 (code, ref_id) 60 秒内只落一条，若不清，相邻用例
      断言「本次写入了 READ_FAIL / COLLECTOR_REVIVE」会莫名其妙查不到。
    · 采集器复活退避：device_id 在不同用例间重复，残留的 hold 会让新任务不重建。
    """
    db._log_throttle_at.clear()
    db._log_suppressed.clear()
    collector.tasks.clear()
    collector._revive_hold.clear()
    collector._revive_delay.clear()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 db/auth 的运行时路径重定向到 pytest 临时目录，并重置内存会话。"""
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "energy.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(auth, "CONFIG_PATH", tmp_path / "config.json")
    auth._sessions.clear()
    _reset_module_state()
    db.init_db()
    yield tmp_path
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    auth._sessions.clear()
    _reset_module_state()


@pytest.fixture
def ready_db(isolated_db):
    """已投运实例：默认密码已改过，业务接口不再被强制改密拦截。

    「默认密码状态下业务接口一律 403」由 test_password.py 单独覆盖，
    其余用例测的是业务逻辑，应运行在已完成首次改密的状态下。
    """
    config = auth.load_config()
    config["password_changed"] = True
    auth.save_config(config)
    return isolated_db
