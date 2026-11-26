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
    # 资产名必须含 tag：同一天发布多个补丁版本时不能再撞名（P2-5）
    assert 'energy-ems-$tag-win11-x64.zip' in yml
    assert "upload-artifact" in yml
    assert "artifact" in yml.lower()


def test_release_package_carries_licenses_and_checksum(isolated_db):
    """发布合规：zip 内必须有 MIT 与第三方声明，且产出 SHA256 校验文件。"""
    yml = (SRC_ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    assert "licenses" in yml and "LICENSE" in yml
    assert "THIRD_PARTY_NOTICES.md" in yml
    assert "Get-FileHash" in yml and ".sha256" in yml
    assert "release_notes.py" in yml, "Release 正文必须按 tag 生成，不再用通用说明"


def test_checksum_file_uses_lf_endings(isolated_db):
    """`.sha256` 必须 LF 结尾。

    Windows runner 上 `Set-Content` 会写成 CRLF，`sha256sum -c` / `shasum -c` 把行尾的
    `\\r` 当成文件名的一部分，去找一个叫 "xxx.zip\\r" 的文件并报 `FAILED open or read`。
    校验值本身是对的，但用户会以为包损坏（v0.1.6 的 release 资产就踩了这个坑）。
    """
    yml = (SRC_ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    # 行尾必须显式写 `n（LF），而不是交给 Set-Content 按平台补行尾
    assert "$hash  $zip`n" in yml, "校验值必须显式写 LF 行尾"
    assert "WriteAllText" in yml, "需用 WriteAllText 控制行尾，避免平台默认 CRLF"
    assert 'Set-Content "$zip.sha256"' not in yml, "Set-Content 在 Windows 上会写成 CRLF"
    # 还要有运行时自检：写成 CRLF 时 WriteAllText 不会报错，只有回读字节才能发现
    assert "ReadAllBytes" in yml and "-contains 13" in yml, "必须有 CR 自检，否则 CRLF 会静默通过"


def test_release_step_is_tag_gated_and_uses_ref_name(isolated_db):
    """Arrange/Act: 读取 workflow 原文。Assert: release 仅由 v* tag 门控、版本号取自 ref_name、无硬编码版本。"""
    # Arrange：以 utf-8 读入 workflow 全文
    yml = (SRC_ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")

    # Act & Assert：锁死四点契约，防止修复被回退
    # 1) step 级门控：只有打 v* tag 才发布，push 主干只产出 artifact
    assert "if: startsWith(github.ref, 'refs/tags/v')" in yml, "release 必须由 v* tag 门控"
    # 2) 版本号取自触发本次运行的 tag，不再硬编码
    assert "tag_name: ${{ github.ref_name }}" in yml, "tag_name 必须取 github.ref_name"
    # 3) 不得存在硬编码版本串（任一旧版本回退都要被拦下）
    assert "v0.1.0" not in yml, "workflow 不得硬编码 v0.1.0"
    assert "v1.0.0" not in yml, "workflow 不得硬编码 v1.0.0"
    # 4) 同一 tag 重跑时可覆盖已有 asset，避免发布失败
    assert "overwrite_files: true" in yml, "需允许覆盖同名 release asset"


def test_workflow_actions_are_pinned_to_commit_sha(isolated_db):
    """供应链：workflow 里每个 uses 都必须按 40 位 commit SHA 固定，并保留 `# vX` 可读注释。

    发布 job 持有 contents: write，若引用可移动的 tag（如 actions/checkout@v4），
    上游一旦重打 tag，未经审阅的代码即可拿到本仓库写权限。Dependabot 能识别这种写法
    并自动提升级 PR，因此固定 SHA 不牺牲可维护性。
    """
    # Arrange：收齐三个 workflow 原文
    workflow_dir = SRC_ROOT / ".github" / "workflows"
    files = sorted(workflow_dir.glob("*.yml"))
    assert files, "必须存在 workflow 文件"

    pinned = re.compile(r"^uses:\s*([\w.-]+/[\w./-]+)@([0-9a-f]{40})\s*(#\s*v\S+)?\s*$")

    # Act & Assert：逐行核对，任何一个 tag 引用都要被拦下
    for path in files:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("uses:"):
                continue
            match = pinned.match(stripped)
            assert match, f"{path.name}:{lineno} 未按 commit SHA 固定: {stripped}"
            assert match.group(3), f"{path.name}:{lineno} 缺少 `# vX` 注释，后续无法判断版本: {stripped}"


def test_package_verifier_handles_pyinstaller_onedir_layout(isolated_db):
    """发布包清单校验必须认 PyInstaller 6 的 onedir 布局（数据文件在 `_internal/`）。

    实测 v0.1.3 的 zip：`energy_ems.exe` 在根，而 `schema.sql`、`dashboard/index.html`
    在 `_internal/` 下。此前校验写死根路径，第一次跑就必然失败。
    同时确认依赖自带的 `*.dist-info/licenses/LICENSE` 不能冒充我们的 MIT 许可。
    """
    import re as _re

    yml = (SRC_ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding="utf-8")
    block = yml.split("$required = @(", 1)[1].split(")", 1)[0]
    patterns = _re.findall(r"'([^']+)'", block)
    assert patterns, "校验步骤必须列出必需条目"

    def missing(entries: list[str]) -> list[str]:
        return [
            pattern for pattern in patterns
            if not any(_re.search(pattern, entry) for entry in entries)
        ]

    # PyInstaller 6 onedir：exe 在根，资源在 _internal/
    onedir_new = [
        "energy_ems.exe",
        "_internal/schema.sql",
        "_internal/dashboard/index.html",
        "licenses/LICENSE",
        "licenses/THIRD_PARTY_NOTICES.md",
        "build_win11.bat",
    ]
    assert missing(onedir_new) == [], f"onedir 布局应全部通过：{missing(onedir_new)}"

    # 老布局（资源平铺在根）也要继续认
    onedir_old = [
        "energy_ems.exe",
        "schema.sql",
        "dashboard/index.html",
        "licenses/LICENSE",
        "licenses/THIRD_PARTY_NOTICES.md",
    ]
    assert missing(onedir_old) == [], f"平铺布局应全部通过：{missing(onedir_old)}"

    # 依赖自带的许可不能替代我们的顶层 licenses/
    fake = [
        "energy_ems.exe",
        "_internal/schema.sql",
        "_internal/dashboard/index.html",
        "_internal/pydantic-2.14.4.dist-info/licenses/LICENSE",
    ]
    lacking = missing(fake)
    assert any(pattern.startswith("^licenses/LICENSE") for pattern in lacking), \
        "缺少顶层 MIT 许可必须被拦下，不能靠 dist-info 里的 LICENSE 蒙过去"
    assert any("THIRD_PARTY" in pattern for pattern in lacking), "第三方声明缺失也要被拦下"


def test_readme_deploy_covers_mode_switch_backup_vcredist(isolated_db):
    """Arrange/Act: 读取部署说明。Assert: 覆盖启动/模式切换/备份/VC++ 运行库。"""
    readme = (SRC_ROOT / "README_DEPLOY.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "simulate" in lowered and "live" in lowered
    assert any(k in lowered for k in ("backup", "备份", ".db"))
    assert any(k in lowered for k in ("vc_redist", "vc++", "visual c++", "运行库"))
