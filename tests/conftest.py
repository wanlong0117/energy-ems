"""全局测试夹具：每个测试使用独立的临时 SQLite 文件，不污染工程目录。"""
from __future__ import annotations

import pytest

import auth
import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """把 db/auth 的运行时路径重定向到 pytest 临时目录，并重置内存会话。"""
    if db._conn is not None:
        db._conn.close()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "energy.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(auth, "CONFIG_PATH", tmp_path / "config.json")
    auth._sessions.clear()
    db.init_db()
    yield tmp_path
    if db._conn is not None:
        db._conn.close()
    db._conn = None
    auth._sessions.clear()


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
