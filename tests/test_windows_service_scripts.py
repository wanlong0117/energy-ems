"""Windows 服务化脚本契约：装得上、起得来、出错能查到、批处理保持纯 ASCII。

现场最容易出问题的不是脚本逻辑，而是三件事：
1. 脚本被放到别的目录 → 找不到 exe；所以每个 bat 第一步必须 `cd /d "%~dp0"` 并校验 exe；
2. 没有管理员权限 → 注册静默失败；必须先 `net session` 探测并以错误码退出；
3. 批处理里写中文 → cmd.exe 按当前代码页解析 UTF-8 字节，轻则乱码重则截断命令。
   因此所有 .bat 必须是纯 ASCII，中文说明只写在 README_DEPLOY.md。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import main

SRC_ROOT = Path(main.__file__).resolve().parent
WIN_DIR = SRC_ROOT / "scripts" / "windows"


def _bat(name: str) -> str:
    return (WIN_DIR / name).read_text(encoding="utf-8")


def test_service_scripts_exist():
    """四个脚本 + 一个 WinSW 配置模板必须随仓库分发。"""
    for name in (
        "energy-ems-service.xml",
        "install-service.bat",
        "uninstall-service.bat",
        "install-autostart-task.bat",
        "uninstall-autostart-task.bat",
    ):
        assert (WIN_DIR / name).is_file(), f"缺少 {name}"


def test_bat_files_are_pure_ascii():
    """批处理必须纯 ASCII：cmd.exe 不做 UTF-8 解码，中文会截断命令。"""
    for path in sorted(WIN_DIR.glob("*.bat")):
        raw: bytes = path.read_bytes()
        assert b"\xef\xbb\xbf" != raw[:3], f"{path.name} 不得带 BOM"
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:  # pragma: no cover - 断言失败路径
            raise AssertionError(f"{path.name} 含非 ASCII 字节（{exc.start} 处），中文请写进 README") from exc


def test_bat_files_cd_to_script_dir_and_check_exe():
    """每个 bat 都要先切到自身目录，并校验 energy_ems.exe 是否在身边。"""
    for name in ("install-service.bat", "install-autostart-task.bat"):
        text = _bat(name)
        assert 'cd /d "%~dp0"' in text, f"{name} 必须先切到脚本目录"
        assert "energy_ems.exe" in text, f"{name} 必须校验 exe 存在"
        assert "net session" in text, f"{name} 必须先探测管理员权限"
        assert "exit /b 1" in text, f"{name} 失败必须有非 0 退出码"


def test_winsw_config_contract():
    """WinSW 配置锁死：exe 路径、工作目录、自动启动、崩溃重启、日志轮转。"""
    root = ET.parse(WIN_DIR / "energy-ems-service.xml").getroot()
    assert root.tag == "service"
    assert root.findtext("id") == "energy-ems"
    assert root.findtext("executable") == r"%BASE%\energy_ems.exe"
    assert root.findtext("workingdirectory") == "%BASE%"
    assert root.findtext("startmode") == "Automatic"
    assert root.findtext("delayedAutoStart") == "true"
    # 崩溃拉起：至少三级 restart，且 resetfailure 存在
    failures = root.findall("onfailure")
    assert len(failures) >= 2
    assert all(f.get("action") == "restart" for f in failures)
    assert root.findtext("resetfailure")
    # stdout 日志必须落盘并轮转，否则服务模式下日志无处可查
    assert root.findtext("logpath") == r"%BASE%\logs"
    log = root.find("log")
    assert log is not None and log.get("mode") == "roll-by-size-time"
    assert log.findtext("pattern")
    # 停止要留足时间给应用取消后台任务（stoptimeout 必须存在且 > 10s）
    assert int(root.findtext("stoptimeout").split()[0]) > 10


def test_install_service_bat_uses_matching_winsw_names():
    """WinSW 要求 exe 名与 xml 名一致，脚本必须自行改名而不是要求用户手工处理。"""
    text = _bat("install-service.bat")
    assert 'copy /y "WinSW.exe" "energy-ems-service.exe"' in text
    assert "energy-ems-service.exe\" install" in text
    assert "energy-ems-service.exe\" start" in text
    # 装完必须自证：打 /healthz 而不是只看 start 的返回码
    assert "/healthz" in text
    assert "Invoke-WebRequest" in text


def test_autostart_task_uses_system_account_at_boot():
    """零依赖方案：开机 30s 后以 SYSTEM 启动，路径带空格也能被 schtasks 正确接收。"""
    text = _bat("install-autostart-task.bat")
    assert "/sc onstart" in text
    assert "/ru SYSTEM" in text
    assert "/delay 0000:30" in text
    # 带空格的路径必须转义成 schtasks 能识别的 \"...\" 形式
    assert r'/tr "\"%EXE%\""' in text


def test_deploy_doc_has_service_section_and_error_codes():
    """README_DEPLOY 必须有服务化章节与错误码对照，否则现场只能靠猜。"""
    doc = (SRC_ROOT / "README_DEPLOY.md").read_text(encoding="utf-8")
    assert "## 9. 设为 Windows 服务" in doc
    section = doc.split("## 9. 设为 Windows 服务", 1)[1]
    assert "WinSW" in section and "schtasks" in section
    for code in ("E01", "E02", "E03", "E04", "E05", "E06", "E21", "E23"):
        assert code in section, f"文档缺少错误码 {code}"
    # 第 2 节必须提醒「双击只是试用」，否则用户不会翻到第 9 节
    head = doc.split("## 2. 启动", 1)[1].split("## 3.", 1)[0]
    assert "第 9 节" in head
