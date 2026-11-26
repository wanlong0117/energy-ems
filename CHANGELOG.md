# 更新日志 / Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.2] - 2026-11-26

审计报告「中低优先级与一致性」12 项全部处理，看板单位统一为 kWh / 元，测试 153 → 175 项。

### 新增

- **分时电价（峰平谷）**：`tariff.start_hour/end_hour` 真正参与日结算，按当天各小时电量
  逐段计价；未覆盖时段回退 `is_default` 单一电价，两者都没有则电费记 0 并标记 `none`。
- **电价快照**：`daily` 新增 `price_cent`（当日加权单价）与 `tariff_snapshot`（时段明细 JSON）；
  已定稿日按新电价重算会记 `TARIFF_REPRICE` 事件，历史电费变动可追溯。
- **数据库版本化迁移**：`schema_version` 表 + `migrations.py` 顺序迁移，旧库启动自动补列补索引，
  重复启动不重复执行，失败即中止。
- **健康检查**：`GET /healthz`（匿名、最小信息、数据库异常 503）与 `GET /api/health`
  （模式、采集任务数、迁移版本、配置错误）。
- **结构化日志**：应用日志改为单行 JSON（`logging_config.py`），便于现场收集与检索。
- **在线备份**：`scripts/backup.py` 用 SQLite backup API 复制数据库并按份数清理旧备份。
- **CI 门槛**：`ruff check` 静态检查 + `pytest-cov` 覆盖率门槛（`.coveragerc`），依赖全部锁定版本。

### 修复

- 看板 KPI 与报表预览统一显示 kWh 与 元（此前 KPI 仍显示毫度整数、分）。
- 串口波特率原样使用配置值，4800/2400 等低速率不再被静默改写为 9600；非法值配置接口返回 400。
- Modbus 响应校验 MBAP 事务号与单元号（RTU 校验站号），防串包；设备异常码不再重试。
- 采集任务异常退出后每秒巡检自动复活，并记 `COLLECTOR_REVIVE`（此前只在配置变更时重建）。
- `config.json` 解析失败改为**拒绝登录**（503）并停用后台任务，不再回退公开默认密码。
- 日切点可配置：`EMS_DAY_START_HOUR` / `EMS_TZ_OFFSET_HOURS`，默认 08:00 / UTC+8 与旧行为一致；
  报表与分时电价均按配置时区换算本地小时。
- 会话过期时看板统一处理 401 并跳回登录页（此前只弹提示，页面仍停在业务页）。

### 文档

- README 增补分时电价、环境变量、运维（探活/日志/备份/升级/恢复）与安全项；
- CHANGELOG、版本号与 FastAPI 元信息统一由 `settings.APP_VERSION` 提供。

## [0.1.1] - 2026-11-26

针对第三方审计报告（真实投运可用性、计量口径与安全）的修复版本。
审计的 5 项阻断级与 8 项高优先级问题全部处理，测试从 137 项增至 153 项。

### 修复（阻断级）

- **live 模式补小时汇总任务**：此前真实采集模式下 `hourly` 无写入者，日/周/月与报表全空。
  新增 `Aggregator.run_hourly_loop()`，`main.lifespan` 在 live 分支注册 `ems-hourly-rollup`。
- **服务端强制改密**：`require_auth` 增加 403 拦截，默认密码状态下仅放行
  `/api/password`、`/api/logout`、`/api/session`，杜绝脚本绕过前端引导。
- **数据库并发安全**：`db` 增加进程内可重入锁，所有读写成套持锁；
  多语句事务改用 `db.transaction()` 在独立连接上 `BEGIN IMMEDIATE`，
  修复「其他线程提交时把本线程未提交写入一并提交、回滚失效」的静默数据损坏。
- **小时电量改间隔法**：以整点前最后一条读数为基准、小时结束前最后一条为收口，
  不再每小时丢弃一个采样间隔（60s 轮询约少计 1.7%，300s 约 8.3%）；
  基准缺失时回退窗口首条并记 `ROLLUP_NO_BASE`。
- **接口文档需鉴权**：关闭 FastAPI 默认 `docs_url`/`redoc_url`/`openapi_url`，
  `/docs` 与 `/openapi.json` 改为带 `require_auth` 的入口。

### 修复（高优先级）

- 读超时/读失败按「保持上一值 + 质量码 1」落库，`quality=1` 不再是死代码；
  分组级失败隔离，一个分组掉线不再导致整轮无读数。
- 日结算按覆盖率定稿：小时覆盖与表覆盖均需 ≥90%，否则保留 `is_final=0` 并记 `SETTLE_INCOMPLETE`。
- 配置/数据接口内部异常不再回显 SQLite 原文；删除被引用记录返回 409，创建时外键不存在返回 400。
- 原始读数按 90 天滚动清理（分批 5000 行），事件日志裁剪至 2 万条。
- 认证加固：PBKDF2-SHA256 加盐哈希（旧明文自动升级）、登录失败 5 次锁定 60 秒、
  配置原子写入 + 0600、会话定期清理、`EMS_COOKIE_SECURE` 控制 Cookie Secure 标志。
- 手动录入时间戳限制在最近 90 天至当前 5 分钟，禁止结算未来日期。
- 报表导出默认最近 31 天，区间上限 366 天、行数上限 2 万；电量改 kWh、电费改 元（两位小数），
  并在表尾固定输出口径说明。
- 聚合并发：日结算、模拟汇总、小时汇总均放入线程执行，避免阻塞事件循环。

### 新增测试

- live 链路端到端（读数 → 小时 → 日 → 报表）与 lifespan 任务注册
- 默认密码业务接口 403、`/docs` 与 `/openapi.json` 匿名 401
- 双线程事务隔离（一方回滚不留痕）、超时落库质量码
- 间隔法与基准缺失回退、覆盖率定稿、90 天清理
- 登录失败锁定、口令哈希存储、手动录入时间边界、报表默认区间与跨度上限

### 文档

- README / SECURITY 同步为修复后的真实行为，删除已不成立的描述

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

[0.1.2]: https://github.com/wanlong0117/energy-ems/releases/tag/v0.1.2
[0.1.1]: https://github.com/wanlong0117/energy-ems/releases/tag/v0.1.1
[0.1.0]: https://github.com/wanlong0117/energy-ems/releases/tag/v0.1.0
