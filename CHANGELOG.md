# 更新日志 / Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-26

首个公开版本。

### 新增

- Modbus TCP / RTU-over-TCP / RTU 串口三协议采集（自研，无 `pymodbus`）
- 100 电表 + 5 组冷却塔电极 + 5 个机械排水手动点的预置模型
- 小时 / 日 / 周 / 月汇总，日结算按本地 08:00（UTC 00:00）口径
- 报表导出：Excel（openpyxl）、PDF（ReportLab，内置中文字体）
- 单文件看板：KPI、趋势、分项、最新读数、配置、报表、手动录入、事件日志
- 模拟器：`simulate` 模式无硬件演示
- Windows onedir 免安装包（PyInstaller + GitHub Actions）
- 暗色模式与移动端响应式
- 首次登录强制修改默认密码
- `EMS_PASSWORD` 环境变量指定初始密码

### 安全

- 会话 cookie 使用 `httponly` + `SameSite=Lax`
- 改密后清空全部会话
- 新增 [SECURITY.md](SECURITY.md)

### 文档

- 中英双语安装配置说明
- PRD、系统设计、开源可行性评估
- MIT 许可证与第三方依赖声明

[0.1.0]: https://github.com/wanlong0117/energy-ems/releases/tag/v0.1.0
