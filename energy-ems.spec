# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包规格（Win11 x64，由 GitHub Actions windows-latest 执行）。

- datas：只读资源 schema.sql 与单文件看板 dashboard/index.html 放入 _MEIPASS；
- hiddenimports：openpyxl / reportlab / pyserial 及其子模块，保证随包可用；
- 可写数据（config.json / energy.db）由 settings.app_dir() 解析到 exe 同级目录。
"""
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

datas = [
    ("schema.sql", "."),
    ("dashboard/index.html", "dashboard"),
]

hiddenimports = [
    "openpyxl",
    "openpyxl.cell",
    "openpyxl.styles",
    "openpyxl.workbook",
    "reportlab",
    "reportlab.lib",
    "reportlab.pdfbase",
    "reportlab.pdfbase.cidfonts",
    "reportlab.platypus",
    "serial",
    "pyserial",
]
hiddenimports.extend(collect_submodules("openpyxl"))
hiddenimports.extend(collect_submodules("reportlab"))

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pymodbus", "aiohttp", "sqlalchemy", "redis", "matplotlib", "numpy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="energy_ems",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="energy_ems",
)
