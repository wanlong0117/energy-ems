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


def parse_version(text: str) -> tuple[int, int, int, int]:
    """把 v0.1.4 / 0.1.4-rc1 这类字符串解析成四段整数（缺位补 0）。"""
    numbers: list[int] = [int(part) for part in re.findall(r"\d+", text)[:3]]
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2], 0


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
