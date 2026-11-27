# Energy EMS · 轻量能源管理系统

[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://github.com/wanlong0117/energy-ems/actions/workflows/tests.yml/badge.svg)](https://github.com/wanlong0117/energy-ems/actions/workflows/tests.yml)
[![Win11 Build](https://github.com/wanlong0117/energy-ems/actions/workflows/build-windows.yml/badge.svg)](https://github.com/wanlong0117/energy-ems/actions/workflows/build-windows.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#快速开始)

> 一块工控机就能跑的能源管理系统：Modbus 直连、中文报表、零运维。
> A lightweight energy management system that runs on a single box — Modbus, Chinese-ready reports, zero ops.

---

## 为什么做这个项目

MyEMS 功能全但部署重（MySQL/Redis/MQ），OpenEMS 工业级但学习曲线陡。
中小工厂、园区、物业的真实需求往往只是：**几十到几百块表、看数、出报表、接 Modbus**，
而且现场只有 Windows，没有专职 IT。

Energy EMS 的取舍：

- **单进程 + SQLite**，不依赖任何外部服务
- **Windows 免安装包**，解压双击即用，不需要装 Python
- **自研 Modbus 三协议**，不引入 `pymodbus`
- **单文件看板**，零前端构建链，改样式不需要 npm
- **中文报表**，Excel / PDF 开箱可用

## 截图

![看板（浅色）](docs/screenshots/dashboard-light.png)

| 暗色模式 | 移动端 |
|---|---|
| ![暗色](docs/screenshots/dashboard-dark.png) | ![移动端](docs/screenshots/dashboard-mobile.png) |

## 功能

| 模块 | 说明 |
|---|---|
| 采集 | Modbus TCP、RTU-over-TCP、RTU 串口（自研实现，CRC16/MBAP 完整）；串口可配校验位（`parity` N/E/O，8E1 现场表可直接接）；同一 COM 口挂多台从站时按端口排队发帧 + 3.5 字符帧间静默 |
| 规模 | 默认预置 100 块电表 + 5 组冷却塔电极 + 5 个机械排水手动点 |
| 数据 | 13 张表，SQLite WAL；读数质量分级（正常/超时/越界/人工），超时按「保持上一值 + 质量码 1」落库；功率点负值判越界（`POWER_NEGATIVE`），原值入库但不进功率统计 |
| 汇总 | 小时 / 日 / 周 / 月；累计读数回退时区分满量程翻转（按 `max_reading_kwh` 补回）、换表（按新表基准重算）与异常（按 0 计），三者分别记 `METER_ROLLOVER` / `METER_SWAP` / `METER_RESET`；`live` 模式内置小时汇总任务；小时电量用左开右闭区间 `(整点, 次整点]`，相邻小时首尾相接、24 小时合计等于首末读数差；日结算按本地 08:00（UTC 00:00）口径，**逐表**判定覆盖，不足 90% 该表不定稿；停机小时（功率低于阈值）从分母剔除，但**整台设备静默的断电小时不算停机**（断线不等于停产，见 `/api/devices/link`） |
| 电价 | 分时电价（峰平谷，按本地小时时段计价）；日表保存当日加权单价与时段快照，改价重算会记 `TARIFF_REPRICE` 事件 |
| 报表 | 日/周/月聚合，Excel（openpyxl）与 PDF（ReportLab，内置中文字体）；电量 kWh、电费 元，默认导出最近 31 天 |
| 保留 | 原始读数 90 天滚动清理，事件日志按 2 万条上限裁剪并对同码事件做 60 秒折叠；数据库版本化迁移（`schema_version`） |
| 查询 | `/api/hourly`、`/api/daily` 缺省时间窗为最近 3 天 / 366 天，单次最多 5 万行，无参查询不会整表返回 |
| 手动录入 | 机械排水量等离线数据补录，自动同步最新读数；只接受最近 90 天至当前 5 分钟内 |
| 看板 | 单文件 HTML，无框架、无构建；KPI 为「今日」口径（按后端日切点），趋势看最近 48 小时，电量/电费统一显示 kWh 与 元 |
| 主题 | 亮/暗色自动跟随系统，860px 以下响应式 |
| 安全 | 服务端强制改密（默认密码下业务接口 403）、PBKDF2 口令哈希、登录失败锁定、接口文档需登录、httponly 会话 cookie、配置损坏时拒绝登录而非回退默认密码 |
| 运维 | `/healthz` 探活、单行 JSON 日志、`scripts/backup.py` 在线备份；采集任务异常退出自动复活并记 `COLLECTOR_REVIVE` |
| 部署 | 免安装包内附 `scripts/windows/`：WinSW 服务化（开机自启 + 崩溃自动拉起 + 日志轮转）或零依赖计划任务，见 [部署说明](README_DEPLOY.md#9-设为-windows-服务开机自启生产默认方式) |
| 演示 | `simulate` 模式内置模拟器，无硬件也能完整跑通 |

## 快速开始

### 方式 A：Windows 免安装包（推荐给现场）

1. 从 [Releases](../../releases) 下载最新的 `energy-ems-v<版本>-win11-x64.zip`（同名 `.sha256` 可校验）
2. 解压到非中文、非 `C:\Program Files` 的目录，例如 `D:\energy_ems`
3. 双击 `energy_ems.exe`（`build_win11.bat` 为备用启动脚本）
4. 浏览器打开 <http://127.0.0.1:8000/>
5. 默认密码 `ems2026`，**首次登录会强制要求修改**

> 包内已含 Python 运行时与全部依赖（含 `VCRUNTIME140.dll`），无需联网、无需 pip。
> 端口默认 8000（`EMS_PORT` 可改）；如被占用，见 [FAQ](#faq)。
>
> **默认只监听 127.0.0.1**。要用手机或其他电脑访问，先改掉默认密码，再设
> `EMS_ALLOW_LAN=1` 启动——默认口令是公开的，直接监听局域网等于让同网段任何人都能抢先改密。

### 方式 B：源码运行

```bash
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
# macOS / Linux
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

打开 <http://127.0.0.1:8000/>。默认 `simulate` 模式，模拟器会立即写入 100 块表的读数。

### 方式 C：无人值守部署（可选）

首次生成配置时可用环境变量指定初始密码，避免默认密码：

```bash
EMS_PASSWORD='your-strong-password' ./energy_ems
```

> 该变量**仅在 `config.json` 不存在时生效**。之后改密请通过看板「配置 → 修改密码」，
> 密码会写入 `config.json` 并标记 `password_changed=true`。

## 配置

`config.json` 与 `energy_ems.exe` 同级（源码运行时为项目根目录），首次启动自动生成：

```json
{
  "password": "ems2026",
  "data_mode": "simulate",
  "simulate_interval_sec": 300,
  "simulate_rollup_sec": 600
}
```

| 字段 | 说明 |
|---|---|
| `password` | 登录密码（首次登录强制修改） |
| `data_mode` | `simulate` 内置模拟器 / `live` 真实 Modbus 采集，互斥 |
| `simulate_interval_sec` | 模拟器写读数间隔（秒） |
| `simulate_rollup_sec` | 模拟模式汇总间隔（秒） |

切换 `data_mode` 后需重启服务生效。

以下环境变量控制计量口径（改动后需重新结算历史，否则新旧口径混在一张表里）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMS_DAY_START_HOUR` | `8` | 本地几点算新的一天（现场抄表口径） |
| `EMS_TZ_OFFSET_HOURS` | `8` | 时区偏移，用于报表本地时刻与分时电价时段匹配 |
| `EMS_COOKIE_SECURE` | 空 | 叠 HTTPS 反向代理后设为 `1`，会话 Cookie 加 Secure |
| `EMS_TRUST_PROXY_HEADERS` | 空 | 仅在反向代理后开启：按 `X-Forwarded-For` 首跳识别来源，避免登录失败锁定把全厂一起锁掉（直连可伪造，不要对着公网开） |

### 电价（峰平谷）

`tariff` 表一行一个时段：`start_hour` / `end_hour` 为**本地小时**的右开区间，
`is_default=1` 的行作为未覆盖时段的兜底价；两者都留空则为全天单一电价。
例如峰段 10–12 点 1.20 元、其余 0.60 元：

| name | price_cent | start_hour | end_hour | is_default |
|---|---|---|---|---|
| 峰 | 120 | 10 | 12 | 0 |
| 平 | 60 | | | 1 |

日结算按当天每小时电量逐段计价，并把加权单价与时段明细写入
`daily.price_cent` / `daily.tariff_snapshot`，改价后重算会在事件日志留下 `TARIFF_REPRICE`。

### Modbus 设备配置

看板「配置」页按资源类型维护 `node / device / point / meter / meter_point / tariff`。

| protocol | host | port | 说明 |
|---|---|---|---|
| `modbus_tcp` | 网关 IP | 502 | 标准 MBAP over TCP |
| `modbus_rtu_tcp` | 网关 IP | TCP 端口 | RTU 帧（地址 + PDU + CRC16）走 TCP |
| `modbus_rtu_serial` | `COM3` 等 | 波特率（4800/9600/115200） | pyserial 串口，异步不阻塞；波特率**原样使用**，非法值返回 400 |
| `manual` | 任意 | 任意 | 手动表/机械表，不参与采集 |

> Modbus 回包会校验 MBAP 事务号与单元号（RTU 校验站号），不符即丢弃重试；
> 设备返回的异常码（`func|0x80`）属确定性答复，不重试，直接记 `READ_FAIL`。

点位试读（`POST /api/point/{id}/read`）会返回原始寄存器十六进制、解析值与四种字节序对照，
用于现场确认 `byte_order`（`ABCD / CDAB / BADC / DCBA`）。

## 架构

```
浏览器（单文件看板）
      │ HTTP
      ▼
FastAPI / Uvicorn（单进程）
  ├── api_auth      登录、会话、改密
  ├── api_health    /healthz 探活、/api/health 详细状态
  ├── api_config    7 类资源 CRUD + 点位试读
  ├── api_data      最新读数、小时/日/周/月
  ├── api_report    Excel / PDF 导出
  ├── collector     Modbus TCP / RTU-TCP / RTU 串口
  ├── simulator     演示数据（simulate 模式）
  ├── aggregation   小时 / 日 / 周 / 月汇总
  └── tariff        分时电价与价格快照
      │
      ▼
SQLite（WAL，13 张业务表 + schema_version）

目录：

```
energy-ems/
├── main.py              # 入口、lifespan、静态资源
├── auth.py              # 会话、密码策略、配置读写
├── db.py                # SQLite 连接与事件日志
├── migrations.py        # 版本化迁移
├── schema.sql           # 13 张业务表
├── modbus.py            # 自研 Modbus 三协议
├── collector.py         # 采集循环
├── simulator.py         # 模拟器
├── aggregation.py       # 聚合与结算
├── tariff.py            # 分时电价计费
├── logging_config.py    # 单行 JSON 日志
├── reports.py           # Excel / PDF
├── api_*.py             # 路由
├── dashboard/index.html # 单文件看板
├── scripts/backup.py    # 在线备份
├── scripts/recompute.py # 历史重算（口径变更后必跑）
├── tests/               # pytest
└── docs/                # PRD、架构、开源评估
```

## 测试

```bash
.venv/bin/python -m pytest -q                 # 全量用例
.venv/bin/python -m pytest -q --cov=.         # 覆盖率门槛见 .coveragerc
.venv/bin/ruff check .                        # 静态检查（CI 强制）
```

覆盖：Modbus 帧解析（含 CRC16/字节序/事务号/单元号）、聚合口径（整点闭合、逐表覆盖率定稿、
分时电价）、live 链路端到端、并发事务隔离、批量写失败回滚、周/月报表与日切点对齐、
Excel/PDF 结构与单位、认证与密码策略（服务端改密拦截、失败锁定、哈希存储、配置损坏拒绝登录）、
配置 CRUD 与外键状态码、数据库迁移、健康检查与备份、历史重算、接口文档鉴权、静态资源完整性。

## 安全

- 默认密码 `ems2026` 状态下，**服务端**只放行改密/登出/会话查询，其余接口一律 403（不是仅靠前端引导）
- 口令以 PBKDF2-SHA256 加盐哈希存储；旧明文配置在首次成功登录时自动升级
- 同来源连续 5 次登录失败锁定 60 秒，并记事件日志
- `/docs`、`/openapi.json` 需登录后访问；Swagger UI 静态资源随包内置，纯内网（无外网）可直接打开
- 会话为 httponly + SameSite=Lax cookie，7 天过期；叠 HTTPS 后设 `EMS_COOKIE_SECURE=1`
- 登录失败锁定按来源 IP 计数（5 次 / 60 秒）。置于 Nginx、Caddy 之后时对端恒为
  `127.0.0.1`，一个人输错 5 次就会锁住全厂——此时设 `EMS_TRUST_PROXY_HEADERS=1`
  改用 `X-Forwarded-For` 首跳；该头可被伪造，所以后端只能让代理可达
- `config.json` 以 0600 权限原子写入；解析失败时**拒绝登录**（503）而不是回退默认密码
- 建议仅在内网使用，公网访问请置于反向代理后并启用 HTTPS
- 漏洞上报请见 [SECURITY.md](SECURITY.md)

## 运维

| 能力 | 用法 |
|---|---|
| 探活 | `GET /healthz`（匿名，最小信息，数据库不可用返回 503）；登录后 `GET /api/health` 看模式、任务数、迁移版本；`GET /api/devices/link` 看逐设备链路（在线/断线/从未连通） |
| 日志 | 应用日志为单行 JSON（`ts/level/logger/message`），可直接重定向到文件；业务事件另存 `event_log` 表 |
| 备份 | `python scripts/backup.py --db energy.db --out backups --keep 14`，用 SQLite backup API 在线复制，不锁采集写入 |
| 重算 | 升级本版后必须执行：`python scripts/recompute.py --db energy.db --dry-run` 看影响面，再去掉 `--dry-run` 正式重算历史小时/日汇总 |
| 升级 | 停服务、替换文件、启动即自动执行 `schema.sql` + 版本化迁移；记录见 `schema_version` 表 |
| 恢复 | 停服务，删除 `energy.db-wal` / `energy.db-shm`，用备份替换 `energy.db` 后启动 |

> 迁移只前向执行、失败即中止（不会在半个升级状态下继续服务）；
> 建议先在测试机用生产库副本跑一遍升级。

## FAQ

**Q：双击 exe 后窗口一闪而过？**
A：确认 zip 已完整解压（`energy_ems.exe` 与 `_internal/` 同级）；检查 Windows Defender
是否误删 exe（可加白名单后重新解压）。包内已含 `VCRUNTIME140.dll`，通常无需另装 VC++ 运行库。

**Q：端口 8000 被占用？**
A：在 exe 目录运行 `netstat -ano | findstr :8000`，按 PID 结束进程；
或使用端口转发：`netsh interface portproxy add v4tov4 listenport=8080 connectport=8000 connectaddress=127.0.0.1`。

**Q：看板没有数据？**
A：确认已登录；`simulate` 模式首屏会立即产数。若为空，检查 `energy.db` 是否可写、
「事件日志」是否有 `BASELINE_READY` / `SIMULATE_FAIL`。

**Q：PDF 中文乱码？**
A：使用 ReportLab 内置 `STSong-Light` CID 字体，无需外部字体；生僻字可能缺字，属正常。

## 路线图

- [x] v0.1 核心：采集、汇总、报表、看板、Win11 包
- [x] v0.1.2 审计加固：分时电价与价格快照、版本化迁移、健康检查、JSON 日志、在线备份
- [x] v0.1.3 计量口径修复：整点闭合、逐表覆盖率、掉线不记 0、事务回滚、历史重算脚本
- [x] v0.1.4 复审加固：演示数据隔离、整点补录重算、实体 ID 防复用、录入量程校验、
      发布包许可文本与 SHA256、默认仅监听本机、配置变更审计、设备配置热生效
- [x] v0.1.5 停机判定：功率低于阈值的小时不计入覆盖率分母，停产日不再卡在「未结算」
- [x] v0.1.6 修掉覆盖率门槛分子分母不同源（会让采集中断的日子被误判已结算），新增有界缺口推断
- [x] v0.1.7 发布资产 `.sha256` 改纯 LF 行尾（`sha256sum -c` 不再误报），构建内加 CR 字节自检
- [x] v0.1.8 修 Windows 包启动即退出（uvicorn 按名导入 main 在冻结环境失效），CI 增加 exe 实跑冒烟
- [x] v0.1.9 串口总线互斥（同 COM 口多从站）、Windows 服务化脚本、满量程翻转/换表补偿、离线 Swagger
- [x] v0.1.10 断电显示为断线（且不按停机豁免）、负功率记异常、日结查询合并、报表预览补覆盖/停机小时
- [ ] Docker / compose（NAS、Linux 服务器）
- [ ] 多用户与角色权限（RBAC）
- [ ] 告警通知（邮件 / 企业微信 / 钉钉）
- [ ] MQTT / OPC UA 协议插件
- [ ] 演示站点与 5 分钟视频

## 贡献

欢迎 issue 与 PR。提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，
并确保 `pytest` 全绿。安全问题请走私密渠道，见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE) © 2026 wanlong0117

可自由使用、修改、商用与闭源分发，只需在副本中保留版权声明与许可声明。
第三方依赖许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 免责声明

本软件用于辅助能源计量与统计。现场投运前请自行校验点位地址、数据类型、字节序与计量口径。
作者不对因配置错误、设备故障或数据偏差造成的生产或商业损失承担责任。
