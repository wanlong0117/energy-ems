"""v2 单文件看板与 Win11 打包静态校验：无外链/6 页签/CN-EN 双语/SVG 自绘、spec/bat/CI 产物。"""
from __future__ import annotations

import re
from pathlib import Path

import main

SRC_ROOT = Path(main.__file__).resolve().parent
SIX_TABS = ("login", "dashboard", "config", "report", "manual", "events")
TAB_KEYS = ("tab_login", "tab_dashboard", "tab_config", "tab_report", "tab_manual", "tab_events")


def _html() -> str:
    path = SRC_ROOT / "dashboard" / "index.html"
    assert path.is_file(), "单文件看板必须存在"
    return path.read_text(encoding="utf-8")


def test_dashboard_has_no_external_links_or_chart_js(isolated_db):
    """Arrange/Act: 读取看板源码。Assert: 无 script src / link href / CDN / Chart.js 外链。"""
    text = _html()
    assert not re.search(r"<script[^>]*\bsrc\s*=", text, re.I), "不得有外链脚本"
    assert not re.search(r"<link[^>]*\bhref\s*=", text, re.I), "不得有外链样式"
    assert not re.search(r"https?://", text, re.I), "不得引用任何外部 URL"
    assert "chart.js" not in text.lower()
    assert "@import" not in text
    # 图表为内联 SVG 自绘
    assert "<svg" in text and "lineChart" in text and "barChart" in text


def test_dashboard_has_six_tabs_and_bilingual_i18n(isolated_db):
    """Arrange/Act: 解析看板。Assert: 6 页签齐全、CN/EN 字典存在且 key 集合一致、页签文案双语覆盖。"""
    text = _html()
    for tab in SIX_TABS:
        assert f'data-tab="{tab}"' in text, f"缺页签 {tab}"
        assert f'id="tab-{tab}"' in text, f"缺页签区块 {tab}"

    zh_part = text.split("zh: {", 1)[1].split("en: {", 1)[0]
    en_part = text.split("en: {", 1)[1].split("};", 1)[0]
    zh_keys = set(re.findall(r"(\w+)\s*:\s*\"", zh_part))
    en_keys = set(re.findall(r"(\w+)\s*:\s*\"", en_part))
    assert zh_keys == en_keys, "CN/EN 字典 key 必须完全一致"
    for key in TAB_KEYS:
        assert key in zh_keys and key in en_keys

    # 页面实际引用的 data-i18n key 必须在两语种都存在
    used = set(re.findall(r'data-i18n(?:-ph)?="(\w+)"', text))
    assert used
    assert used.issubset(zh_keys) and used.issubset(en_keys)


def test_dashboard_has_no_hardcoded_mock_data(isolated_db):
    """Arrange/Act: 读取看板 JS。Assert: 数据全部来自真实 /api，无 mock/fake 硬编码数据。"""
    text = _html().lower()
    for marker in ("mock", "fake_data", "dummy", "math.random"):
        assert marker not in text, f"看板不得含 mock 数据标记: {marker}"
    for endpoint in ("/api/latest", "/api/hourly", "/api/daily",
                     "/api/manual", "/api/report/export", "/api/mode"):
        assert endpoint in text, f"看板应读取真实 API: {endpoint}"
    # 周/月报表按 kind 动态拼接成 /api/weekly、/api/monthly，仍是真实 API
    assert '"/api/" + kind' in text, "周/月报表应经 /api/{kind} 读取真实数据"


def test_pyinstaller_spec_has_datas_and_hiddenimports(isolated_db):
    """Arrange/Act: 读取 spec。Assert: datas 含 schema.sql+dashboard、hiddenimports、onedir COLLECT。"""
    spec = (SRC_ROOT / "energy-ems.spec").read_text(encoding="utf-8")
    assert "schema.sql" in spec
    assert "dashboard/index.html" in spec
    assert "hiddenimports" in spec
    for dep in ("openpyxl", "reportlab", "serial"):
        assert dep in spec, f"hiddenimports 应含 {dep}"
    assert "COLLECT(" in spec and "exclude_binaries=True" in spec, "必须为 onedir 打包"


def test_build_bat_launches_onedir_exe(isolated_db):
    """Arrange/Act: 读取 bat。Assert: 双击启动 onedir exe 并设工作目录为 exe 所在。"""
    bat = (SRC_ROOT / "build_win11.bat").read_text(encoding="utf-8")
    assert "energy_ems.exe" in bat
    assert "cd /d" in bat
    assert re.search(r"start\s+\"Energy EMS\"", bat)
    assert "ERROR" in bat  # 有找不到 exe 的兜底提示


def test_github_actions_produces_win11_artifact(isolated_db):
    """Arrange/Act: 读取 workflow。Assert: windows-latest runner + PyInstaller + artifact 名含版本标识。"""
    yml = (SRC_ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    assert "runs-on: windows-latest" in yml
    assert "pyinstaller energy-ems.spec" in yml
    assert "energy-ems-win11-x64" in yml
    assert "upload-artifact" in yml
    assert "artifact" in yml.lower()


def test_readme_deploy_covers_mode_switch_backup_vcredist(isolated_db):
    """Arrange/Act: 读取部署说明。Assert: 覆盖启动/模式切换/备份/VC++ 运行库。"""
    readme = (SRC_ROOT / "README_DEPLOY.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "simulate" in lowered and "live" in lowered
    assert any(k in lowered for k in ("backup", "备份", ".db"))
    assert any(k in lowered for k in ("vc_redist", "vc++", "visual c++", "运行库"))
