# 第三方依赖与许可 / Third-Party Notices

本项目自身以 [MIT 许可证](LICENSE) 分发。运行与构建依赖以下开源组件，各组件版权归其作者所有，遵循各自许可证。

> Windows 二进制包（`energy-ems-*.zip`）会把这文件与 `LICENSE` 一起放在 `licenses/` 目录内，
> 与协议要求一致：**任何再分发都必须保留本文件与 MIT 许可文本**。

## 直接依赖

| 组件 | 版本约束 | 许可证 | 用途 |
|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | ==0.165.0 | MIT | HTTP 框架 |
| [Starlette](https://github.com/encode/starlette) | 随 FastAPI | BSD-3-Clause | ASGI 工具集 |
| [Uvicorn](https://github.com/encode/uvicorn) | ==0.54.0 | BSD-3-Clause | ASGI 服务器 |
| [Pydantic](https://github.com/pydantic/pydantic) | ==2.14.4 | MIT | 请求/响应校验 |
| [Click](https://github.com/pallets/click) | 随 Uvicorn | BSD-3-Clause | Uvicorn 命令行 |
| [idna](https://github.com/kjd/idna) | 随 httpx | BSD-3-Clause | 域名编码 |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | ==3.1.5 | MIT | Excel 导出 |
| [ReportLab](https://www.reportlab.com/opensource/) | ==5.1.1 | BSD-3-Clause | PDF 导出 |
| [pyserial](https://github.com/pyserial/pyserial) | ==3.5 | BSD-3-Clause | Modbus RTU 串口 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | ==6.22.3 | GPL-2.0-or-later **with bootloader exception** | Windows onedir 打包（仅构建期） |

## 随二进制分发的运行时

PyInstaller 产物内置以下运行时组件，同样需要在使用者文档中声明：

| 组件 | 许可证 | 说明 |
|---|---|---|
| Python 解释器 | PSF License 2.0 | 内置解释器与标准库，版权归 Python Software Foundation |
| OpenSSL / libcrypto | Apache-2.0 | Python `ssl` / `hashlib` 依赖的加密库（Windows 包内 `libcrypto-*.dll`） |
| SQLite | Public Domain | Python `sqlite3` 内置数据库引擎 |
| zlib / libffi / XZ | zlib / MIT / Public Domain | 标准库压缩与 FFI 依赖 |
| tzdata（若随包） | Apache-2.0 | 时区数据 |

## 说明

- 本项目**未使用** `pymodbus`，Modbus TCP / RTU-over-TCP / RTU 串口均为自研实现（`modbus.py`），
  不引入 GPL 代码。
- 本项目**未复制** MyEMS、OpenEMS、Emoncms 等 AGPL 项目的源代码；功能设计为独立实现。
- PDF 导出使用 ReportLab 内置 `STSong-Light` CID 字体，未内嵌任何商业字体文件。
- PyInstaller 的 bootloader exception 明确允许将打包产物以任意许可证分发；
  本项目产物按 MIT 分发。
- MIT / BSD-3 均要求「保留版权声明与免责声明」，因此二进制包内的 `licenses/` 目录不可删除。

如发现遗漏或错误，请提交 issue 或邮件联系维护者。
