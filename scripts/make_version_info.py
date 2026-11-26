"""生成 PyInstaller 的 Windows 版本资源（exe 属性里可见版本号）。

为什么需要：此前 exe 没有版本资源，用户下载多个包后只能靠文件名猜；
文件属性里版本号为空，杀软/资产清单也无法识别版本。

用法（CI 与本地均可）：
    python scripts/make_version_info.py --version v0.1.4 --out build/version_info.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Windows 控制台默认按 cp1252 编码，打印中文会抛 UnicodeEncodeError，把整个 CI 步骤
# 直接跑挂（本地 macOS/Linux 是 UTF-8，永远复现不出来）。stdout / stderr 都定成 UTF-8 并
# 用 replace 兜底：日志里最多出现问号，不会因为一句话把构建停掉。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple4},
    prodvers={tuple4},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'energy-ems'),
         StringStruct('FileDescription', 'Energy EMS 轻量能耗管理系统'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'energy_ems'),
         StringStruct('LegalCopyright', 'MIT License'),
         StringStruct('OriginalFilename', 'energy_ems.exe'),
         StringStruct('ProductName', 'energy-ems'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")
_MAX_FIELD = 65535  # Windows 版本资源的每段是 WORD，超了 PyInstaller 直接拒编


def parse_version(text: str) -> tuple[int, int, int, int]:
    """把 v0.1.4 / 0.1.4-rc1 解析成四段整数；非语义化版本号一律退回 0.0.0.0。

    为什么必须严格：主干构建的 tag 形如 `dev-2120610`（提交号前 7 位），旧实现把所有
    数字抓出来当主版本号，得到 `filevers=(2120610,0,0,0)` —— 超过 WORD 上限，PyInstaller
    在「打标签的运行成功、推主干的运行失败」这种最迷惑的组合上挂掉。
    """
    match = _SEMVER.match(text.strip())
    if match is None:
        return 0, 0, 0, 0
    major, minor, patch = (min(int(part), _MAX_FIELD) for part in match.groups())
    return major, minor, patch, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 PyInstaller 版本资源文件")
    parser.add_argument("--version", default="0.0.0", help="版本号，可带 v 前缀或 -rc 后缀")
    parser.add_argument("--out", default="build/version_info.txt", help="输出文件路径")
    args = parser.parse_args(argv)

    version: str = args.version.strip() or "0.0.0"
    numbers = parse_version(version)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        TEMPLATE.format(tuple4=numbers, version=version.lstrip("v")),
        encoding="utf-8",
    )
    print(f"版本资源已写入 {target}：{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
