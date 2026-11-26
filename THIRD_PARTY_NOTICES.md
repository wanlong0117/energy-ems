# 第三方依赖与许可 / Third-Party Notices

本项目自身以 [MIT 许可证](LICENSE) 分发。运行与构建依赖以下开源组件，各组件版权归其作者所有，遵循各自许可证。

| 组件 | 版本约束 | 许可证 | 用途 |
|---|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | >=0.110 | MIT | HTTP 框架 |
| [Uvicorn](https://github.com/encode/uvicorn) | >=0.29 | BSD-3-Clause | ASGI 服务器 |
| [Pydantic](https://github.com/pydantic/pydantic) | >=2.5 | MIT | 请求/响应校验 |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) | >=3.1 | MIT | Excel 导出 |
| [ReportLab](https://www.reportlab.com/opensource/) | >=4.0 | BSD-3-Clause | PDF 导出 |
| [pyserial](https://github.com/pyserial/pyserial) | >=3.5 | BSD-3-Clause | Modbus RTU 串口 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) | >=6.0 | GPL-2.0-or-later **with bootloader exception** | Windows onedir 打包（仅构建期） |

## 说明

- 本项目**未使用** `pymodbus`，Modbus TCP / RTU-over-TCP / RTU 串口均为自研实现（`modbus.py`），
  不引入 GPL 代码。
- 本项目**未复制** MyEMS、OpenEMS、Emoncms 等 AGPL 项目的源代码；功能设计为独立实现。
- PDF 导出使用 ReportLab 内置 `STSong-Light` CID 字体，未内嵌任何商业字体文件。
- PyInstaller 的 bootloader exception 明确允许将打包产物以任意许可证分发；
  本项目产物按 MIT 分发。

如发现遗漏或错误，请提交 issue 或邮件联系维护者。
