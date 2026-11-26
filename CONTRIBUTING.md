# 贡献指南 / Contributing

感谢你愿意改进 Energy EMS。为了让评审更高效，请先阅读以下约定。

## 开发环境

```bash
git clone https://github.com/wanlong0117/energy-ems.git
cd energy-ems
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 应全部通过
.venv/bin/python main.py               # http://127.0.0.1:8000
```

- Python 3.11+
- 不引入前端构建链：看板保持单文件 `dashboard/index.html`
- 不引入 ORM / Redis / 消息队列：保持单进程 + SQLite

## 提交前必须通过

```bash
.venv/bin/python -m pytest -q
```

- 新增功能必须带测试
- 修复 bug 请先写一个能复现的失败测试
- 不接受的 PR：仅格式化全文件、引入重型依赖、破坏 SQLite 兼容性

## 代码约定

- 遵循现有风格：`from __future__ import annotations`、类型标注、短函数、中文注释
- 数据库变更必须**幂等**：新表写进 `schema.sql`（`CREATE TABLE IF NOT EXISTS`），加列/加索引/改主键
  必须新增一条 `migrations.py` 迁移（先 `PRAGMA table_info` 判存在），旧库启动自动按序升级
- 时间口径统一 UTC 秒；日切点可配（`EMS_DAY_START_HOUR`，默认本地 08:00 = UTC 00:00），
  换算只允许走 `settings.day_floor()` / `local_hour()`
- 电量单位：毫度（1 kWh = 1,000,000）
- 前端不引入框架；新增交互优先用原生 API
- 不提交 `config.json`、`energy.db*`、`.venv/`、`dist/`、`build/`

## 分支与提交

- 分支：`feat/<short-name>`、`fix/<short-name>`、`docs/<short-name>`
- 提交信息用 Conventional Commits：
  - `feat: 新增 MQTT 采集`
  - `fix: 修正周报跨月边界`
  - `docs: 补充 Docker 部署`
- 一个 PR 只做一件事；大改动请先开 issue 讨论

## PR 检查清单

- [ ] `pytest` 全绿
- [ ] 新增/修改行为有测试覆盖
- [ ] 未提交敏感文件（`config.json`、`energy.db`、密钥）
- [ ] 文档已同步（README / CHANGELOG，必要时 SECURITY）
- [ ] 没有复制 AGPL 项目的代码（本项目为 MIT，需保持许可干净）

## 报告问题

- Bug：使用 Bug Report 模板，附版本、部署方式、复现步骤、日志
- 功能建议：说明使用场景与当前替代方案
- 安全漏洞：**不要开公开 issue**，见 [SECURITY.md](SECURITY.md)

## 许可

提交即表示你同意以 [MIT](LICENSE) 授权你的贡献。
