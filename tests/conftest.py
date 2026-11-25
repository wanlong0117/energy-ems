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
