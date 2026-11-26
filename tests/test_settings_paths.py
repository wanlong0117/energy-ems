"""v2 双路径与配置兼容验证：app_dir/resource_dir（源码+frozen）、旧 config 补缺省字段。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import auth
import db
import settings

SRC_ROOT = Path(settings.__file__).resolve().parent


def test_app_dir_and_resource_dir_in_source_mode(monkeypatch):
    """Arrange: 非 frozen 源码模式。Act/Assert: 两个目录都指向项目根。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert settings.is_frozen() is False
    assert settings.app_dir() == SRC_ROOT
    assert settings.resource_dir() == SRC_ROOT
    assert settings.DASHBOARD_DIR == SRC_ROOT / "dashboard"
    assert (settings.DASHBOARD_DIR / "index.html").is_file()


def test_frozen_paths_derive_from_executable_and_meipass(monkeypatch, tmp_path):
    """Arrange: monkeypatch frozen 环境。Act: 解析路径。Assert: 可写=exe 同级、只读=_MEIPASS。"""
    dist = tmp_path / "energy_ems_dist"
    (dist / "_internal").mkdir(parents=True)
    exe = dist / "energy_ems.exe"
    exe.write_bytes(b"")
    meipass = dist / "_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    assert settings.is_frozen() is True
    assert settings.app_dir() == dist.resolve()
    assert settings.resource_dir() == meipass
    # 可写目录绝不来自 _MEIPASS，只读资源绝不来自 exe 同级
    assert settings.app_dir() != settings.resource_dir()


def test_old_config_only_password_is_backfilled_without_overwrite(isolated_db):
    """Arrange: 旧库 config.json 仅 password。Act: load_password。Assert: 补三个缺省键且不覆盖已有键。"""
    config_path = auth.CONFIG_PATH
    config_path.write_text(json.dumps({"password": "custom_pwd"}), encoding="utf-8")

    assert auth.load_password() == "custom_pwd"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["password"] == "custom_pwd"
    assert saved["data_mode"] == "simulate"
    assert saved["simulate_interval_sec"] == 300
    assert saved["simulate_rollup_sec"] == 600


def test_existing_user_keys_are_not_overwritten_by_defaults(isolated_db):
    """Arrange: 用户已有 data_mode/simulate_*。Act: load_config。Assert: 全部保留原值。"""
    config_path = auth.CONFIG_PATH
    original = {
        "password": "p",
        "password_changed": True,
        "data_mode": "live",
        "simulate_interval_sec": 77,
        "simulate_rollup_sec": 99,
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    loaded = auth.load_config()
    assert loaded["data_mode"] == "live"
    assert loaded["simulate_interval_sec"] == 77
    assert loaded["simulate_rollup_sec"] == 99
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == original  # 字段齐全，不应回写覆盖


def test_set_data_mode_persists_and_rejects_invalid(isolated_db):
    """Arrange/Act: 切换合法模式并写审计。Assert: 配置持久化、非法模式抛 ValueError。"""
    assert auth.set_data_mode("LIVE") == "live"
    assert json.loads(auth.CONFIG_PATH.read_text(encoding="utf-8"))["data_mode"] == "live"
    assert auth.load_config()["data_mode"] == "live"
    assert db.query_one("SELECT 1 FROM event_log WHERE code='MODE_SWITCH'") is not None
    with pytest.raises(ValueError):
        auth.set_data_mode("invalid")
