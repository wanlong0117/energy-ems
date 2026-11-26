# 变更说明 / Pull Request

## 做了什么

<!-- 一句话说明这个 PR 解决什么问题，关联 issue 请写 Fixes #123 -->

## 影响面自查

- [ ] 改了计量/结算口径（涉及 `aggregation.py` / `manual.py` / `tariff.py`）→ 已在 PR 里说明是否需要重算历史
- [ ] 改了数据库结构 → 新增了 `migrations.py` 迁移，且用 `PRAGMA table_info` 做过存在性判断
- [ ] 改了接口行为 → 同步更新了 `README.md` / `README_DEPLOY.md` / `CHANGELOG.md`
- [ ] 新增了依赖 → 更新了 `requirements*.txt`（`==` 锁定）与 `THIRD_PARTY_NOTICES.md`

## 验证方式

<!-- 贴出实际执行过的命令与结果，而不是「已测试」 -->
```
python -m pytest -q
python -m ruff check .
```

## 数据安全

- [ ] 没有把真实现场 IP、密码、公司名写进代码/文档/测试
- [ ] 涉及删除或覆盖数据的逻辑有审计事件（`event_log`）
