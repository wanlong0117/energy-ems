"""发布/运维脚本回归：Windows 控制台（cp1252）下也得跑完，产物内容还得对。

为什么单独测这一层：v0.1.4 的 Windows 构建挂在「生成 exe 版本资源」这一步——脚本
`print` 中文，而 runner 控制台编码是 cp1252，直接 `UnicodeEncodeError`。本地开发机
是 UTF-8，这个失败**永远复现不出来**，只能靠显式构造 cp1252 环境兜住。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENCODINGS = ["cp1252", "utf-8"]


def _run(script: str, *args: str, console_encoding: str) -> subprocess.CompletedProcess[str]:
    """用指定 I/O 编码跑脚本，模拟 Windows 控制台。"""
    env = {
        **os.environ,
        "PYTHONIOENCODING": console_encoding,
        "PYTHONUTF8": "0",  # 关键：关掉 UTF-8 模式，才能重现 cp1252
    }
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=ROOT,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("console_encoding", ENCODINGS)
def test_make_version_info_survives_windows_console(tmp_path, console_encoding):
    """两种控制台下都要成功，且版本资源内容一致。"""
    # Arrange
    target = tmp_path / "build" / "version_info.txt"

    # Act
    result = _run(
        "make_version_info.py", "--version", "v0.1.4", "--out", str(target),
        console_encoding=console_encoding,
    )

    # Assert
    assert result.returncode == 0, f"{console_encoding} 下失败：{result.stderr}"
    assert "UnicodeEncodeError" not in result.stderr
    content = target.read_text(encoding="utf-8")
    assert "VSVersionInfo(" in content
    assert "filevers=(0, 1, 4, 0)" in content
    assert "StringStruct('FileVersion', '0.1.4')" in content
    assert "OriginalFilename', 'energy_ems.exe'" in content
    # PyInstaller 要求文件带编码声明，否则中文描述会被按 cp1252 读
    assert content.startswith("# UTF-8")


def test_make_version_info_accepts_main_branch_dev_tag(tmp_path):
    """主干构建的 tag 是 `dev-<sha>`，绝不能把 sha 前 7 位当成主版本号。

    旧实现抓所有数字 → `filevers=(2120610,0,0,0)`，超过 WORD 上限，PyInstaller 拒编。
    诡异之处在于「推 tag 的运行成功、推主干的运行失败」，只看 tag 结果是看不出来的。
    """
    # Arrange
    target = tmp_path / "version_info.txt"

    # Act
    result = _run(
        "make_version_info.py", "--version", "dev-2120610", "--out", str(target),
        console_encoding="utf-8",
    )

    # Assert
    assert result.returncode == 0, result.stderr
    content = target.read_text(encoding="utf-8")
    assert "filevers=(0, 0, 0, 0)" in content, "非语义化版本必须退回 0.0.0.0"
    assert "dev-2120610" in content, "人读的版本串仍要保留，便于分辨主干构建"


def test_parse_version_strictness():
    """版本解析的边界：只认 x.y.z，每段夹到 WORD 上限。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    from make_version_info import parse_version

    assert parse_version("v0.1.4") == (0, 1, 4, 0)
    assert parse_version("0.1.4-rc1") == (0, 1, 4, 0)
    assert parse_version("v10.20.30") == (10, 20, 30, 0)
    # 关键：提交号不能被当成版本号
    assert parse_version("dev-2120610") == (0, 0, 0, 0)
    assert parse_version("dev-9f3a1b2") == (0, 0, 0, 0)
    assert parse_version("") == (0, 0, 0, 0)
    assert parse_version("v999999.1.1") == (65535, 1, 1, 0)


@pytest.mark.parametrize("console_encoding", ENCODINGS)
def test_release_notes_survives_windows_console(tmp_path, console_encoding):
    """Release 正文按 tag 生成，同样不能被控制台编码打断。"""
    # Arrange
    target = tmp_path / "release_notes.md"

    # Act
    result = _run(
        "release_notes.py", "--tag", "v0.1.4", "--out", str(target),
        console_encoding=console_encoding,
    )

    # Assert
    assert result.returncode == 0, f"{console_encoding} 下失败：{result.stderr}"
    assert "UnicodeEncodeError" not in result.stderr
    body = target.read_text(encoding="utf-8")
    assert body.startswith("能耗管理系统 energy-ems v0.1.4")
    assert "EMS_ALLOW_LAN=1" in body, "默认只监听本机的提示必须出现在下载页"
    assert "licenses/" in body, "发布包含许可目录这件事要写给下载的人看"


def test_backup_reports_error_instead_of_crashing_on_windows_console(tmp_path):
    """错误分支也走中文提示：stderr 同样要兜底，不能让 traceback 自己崩在编码上。"""
    # Act
    result = _run(
        "backup.py", "--db", str(tmp_path / "missing.db"), "--out", str(tmp_path / "out"),
        console_encoding="cp1252",
    )

    # Assert
    assert "UnicodeEncodeError" not in result.stderr
    assert "Traceback" not in result.stderr, "缺库属于预期分支，不该抛栈"
    assert result.returncode == 1
    assert "备份失败" in result.stderr


def test_clear_demo_dry_run_prints_on_windows_console(tmp_path):
    """现场最常用的 --dry-run 输出也必须在 cp1252 下完整打印。"""
    # Arrange
    db_path = tmp_path / "energy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO node (id, name, kind, sort_order, is_active, is_demo) "
            "VALUES (1, '模拟-A栋', 'area', 0, 1, 1)"
        )
        conn.execute(
            "INSERT INTO meter (name, node_id, category_id, purpose, is_counted, is_enabled, is_demo) "
            "VALUES ('模拟-001号电表', 1, 1, 'total', 1, 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    # Act
    result = _run("clear_demo.py", "--db", str(db_path), "--dry-run", console_encoding="cp1252")

    # Assert
    assert result.returncode == 0, result.stderr
    assert "将清理" in result.stdout and "meter 1 行" in result.stdout
    assert "未删除任何数据" in result.stdout
