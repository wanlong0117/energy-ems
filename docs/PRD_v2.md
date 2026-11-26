# 能耗管理系统 energy_ems · 增量 PRD v2

> 本 PRD 为相对 `docs/PRD.md`（v1）的**增量**，只描述本轮新增与变更；v1 的 Phase 0~3（13 源文件 + 12 表 + 74 项测试全通过）不重复。优先级 P0/P1/P2 = 重要性（Must/Should/Nice），与 Phase 里程碑为不同维度。

## 一、项目信息

| 项目 | 内容 |
|---|---|
| Language | 中文 |
| Programming Language | Python + FastAPI + SQLite + 原生 JS 单文件看板；本轮新增 openpyxl、reportlab、pyserial、PyInstaller |
| Project Name | `energy_ems` |
| 本轮范围 | 完整交付：补齐 Phase 4 看板、Phase 5 报表、Phase 6 运维/打包，并扩展模拟数据、周/月汇总、Excel/PDF、手动录入、冷却塔电极、三类 Modbus |
| 交付形态 | Win11 onedir 可执行包（双击 bat 启动）+ GitHub Actions 自动产出 zip |
| 数据一致性底线 | 看板/报表/API 一律读真实 SQLite，不允许 mock 分支 |

**原始需求复述**：强哥希望"全部做完打包、全部部署完后再慢慢接入数据，先用模拟数据"；电脑为 Win11。明确要求：①保留 Modbus TCP/RTU 采集以便后续其他接入；②需要小时/日/周/月汇总报表；③报表需 Excel/PDF 导出；④电表按 100 个预留，冷却塔电极 5 个（电导率、电流、排污阀状态），冷却塔排水量现为机械表、需支持手动输入。

## 二、本轮默认决策（已定）

| 编号 | 决策 | 落点 |
|---|---|---|
| K1 | 完整交付：补齐 Phase 4/5/6 并扩展新需求 | 全篇 P0 |
| K2 | `config.json` 增加 `data_mode`: `simulate`/`live`，默认 simulate；simulate 由内置模拟器生成 100 电表 + 5 冷却塔电极数据写入真实表；所有看板/报表/API 读真实 DB、无 mock 分支；切 live 后采集器接管 | G1/G2 |
| K3 | Win11：macOS 不交叉产 exe；交付 PyInstaller onedir 脚本（Win11 双击 bat）+ GitHub Actions（Windows runner 出 zip artifact）；配置/DB 放 exe 同级，升级不丢数据 | G12 |
| K4 | 周/月汇总不新增物化表，从 `daily` 按需聚合；新增 `/api/weekly`、`/api/monthly` 与报表导出 | G3 |
| K5 | Excel 用 openpyxl 生成 .xlsx；PDF 用 reportlab 生成 .pdf，中文用内置 STSong-Light CID 字体、不依赖外部字体文件；两库加入依赖并随包打包 | G4/G5 |
| K6 | Modbus RTU 保留：支持 `modbus_tcp`（MBAP）、`modbus_rtu_tcp`（RTU 帧走 TCP，CRC16）、`modbus_rtu_serial`（Win11 COM 口，pyserial）；仍禁止 pymodbus | G10 |
| K7 | 新增 `manual_reading` 表（point_id/ts/value/operator/remark）；录入后事务同步写 `reading`（quality=3）与 `reading_latest`；用于冷却塔机械排水量表及未来离线表补录 | G8/G9 |
| K8 | 冷却塔电极建模：5 组 × 3 点——电导率（f32，μS/cm）、电流（f32，A）、排污阀状态（u16，0/1）；用 node/device/point/meter 表达，不新增业务表 | G7 |
| K9 | 前端单文件 `dashboard/index.html`，原生 JS 内联、无外链，右上角 CN/EN 切换；含登录/看板/配置/报表/手动录入/事件日志 6 页签；图表内联 SVG 自绘，不引 Chart.js | G11 |

## 三、变更概述（相对 v1）

| 维度 | v1 状态 | v2 变更 |
|---|---|---|
| 范围 | Phase 0~3 已交付（采集/配置/聚合） | 本轮补齐 Phase 4~6 并扩展 6 项新需求 |
| 数据源 | 仅 live 采集 | 新增 simulate 模式 + 内置模拟器，默认 simulate |
| 汇总粒度 | 小时 `hourly`、日 `daily`（08:00 口径） | 新增周/月按需聚合 + 两个 API，不新增表 |
| 导出 | 仅规划 CSV | 新增 Excel（.xlsx）/ PDF（.pdf）中文导出 |
| Modbus | `modbus_tcp` / `modbus_rtu_tcp` | 新增 `modbus_rtu_serial`（COM 口，pyserial）；三协议并存，仍禁 pymodbus |
| 表结构 | 12 张表 | 新增 `manual_reading`（第 13 张），其余不变 |
| 冷却塔 | 无 | 5 组电极 × 3 点，用现有 4 张配置表表达 |
| 看板 | 规划 Phase 4，未实现 | 单文件 6 页签 + CN/EN + SVG 自绘图表 |
| 交付 | 源码运行 | Win11 onedir 包 + bat + GH Actions zip |
| 依赖 | fastapi/uvicorn/pydantic | 增 openpyxl/reportlab/pyserial + PyInstaller（构建期） |

## 四、产品目标

1. **一次交付、开箱即跑**：强哥拿到完整 Win11 包双击即用，默认模拟数据让看板与报表立刻有数；后续接真实表只需把 `data_mode` 从 simulate 切到 live，不用改接口或造 mock。
2. **可信的周期报表**：在既有 08:00 口径小时/日汇总之上按需产出小时/日/周/月汇总，支持 Excel/PDF 中文导出，满足现场对账与管理层汇报。
3. **兼容存量、预留扩展**：保留并扩展三类 Modbus 接入，补齐冷却塔电极与机械表手动录入通道，所有数据统一落同一套真实表与采集—汇总链路。

## 五、用户故事

1. As 强哥（工厂运维），I want 一次性拿到完整的 Win11 安装包并双击启动，so that 部署后先看到模拟数据在看板上跑起来，之后再慢慢接入真实电表。
2. As 强哥，I want 用 `data_mode` 在 simulate 与 live 之间切换且各处都读真实 DB，so that 演示、验收和真实接入共用同一套 API/看板/报表，不维护假数据分支。
3. As 工厂管理者，I want 查看小时/日/周/月汇总并导出 Excel 与 PDF，so that 对账和汇报不用再从 MyEMS 手工拼表。
4. As 强哥，I want 未来可继续用 Modbus TCP/RTU 三种方式接入新设备，so that 后续其他品牌电表或冷却塔仪表能直接接入而不重写采集代码。
5. As 现场运维，I want 在网页手动录入冷却塔机械排水量表读数并自动进入汇总，so that 机械表数据也能参与能耗统计与报表。
6. As 强哥，I want 看板支持 CN/EN 切换并可查事件日志，so that 现场中外人员都能看懂、异常可追溯。

## 六、需求池

### 6.1 P0（本轮必须全部交付）

| 编号 | 需求 | 验收要点 |
|---|---|---|
| G1 | 模拟/真实双模式：`config.json` 增加 `data_mode`（默认 `simulate`）；simulate 由模拟器产数写真实表，live 由采集器接管 | 默认启动为 simulate；两模式共用同一套 API/看板/报表，代码中无 mock 分支；切换后无需重启即可生效或有明确重启提示 |
| G2 | 内置模拟器：生成 100 电表 + 5 冷却塔电极数据，写 `reading`/`reading_latest` 并驱动 `aggregation` 产出 `hourly`/`daily` | 启动后 `/api/latest`、`/api/hourly`、`/api/daily` 均返回非空且时间连续；累计电量单调增；模拟频率可配置 |
| G3 | 周/月汇总：新增 `/api/weekly`、`/api/monthly`，从 `daily` 按需聚合，复用 08:00 口径与现有过滤项 | 结果与对应区间 `daily` 求和一致（误差 0）；不新增物化表；支持 meter/node/purpose 过滤 |
| G4 | Excel 导出：openpyxl 生成 .xlsx，覆盖日/周/月报表，含按表/node/purpose 分组与合计 | 文件可打开、中文表头正常、数值与 API 一致；随包打包无需另装库 |
| G5 | PDF 导出：reportlab 生成 .pdf，中文用内置 STSong-Light CID 字体 | 中文不乱码、无外部字体文件依赖；与 Excel 同口径同数值 |
| G6 | 报表与导出接口统一：统一鉴权、`{ok,...}` 响应格式、非法参数 400/422 | 未登录 401；空区间返回空报表而非报错 |
| G7 | 冷却塔电极建模：5 组 × 3 点（电导率 f32 μS/cm、电流 f32 A、排污阀状态 u16 0/1），用 node/device/point/meter 表达 | 配置页可见 15 个点；模拟数据落在对应范围；不新增业务表 |
| G8 | 新增 `manual_reading` 表（point_id/ts/value/operator/remark），与现有 12 表共存 | `init_db` 幂等建表；旧库升级自动补表、原数据不丢 |
| G9 | 手动录入 API（增/查）+ 看板录入页：录入后事务同步写 `reading`（quality=3）与 `reading_latest` | 三表一致；重复 (point_id, ts) 有明确处理；录入值进入 daily/报表 |
| G10 | Modbus 三协议：`modbus_tcp`（MBAP）、`modbus_rtu_tcp`（RTU 帧走 TCP，CRC16 校验）、`modbus_rtu_serial`（Win11 COM 口，pyserial） | 三种协议各有单测覆盖解析与 CRC；仍禁止 pymodbus；试读 3 秒内返回 |
| G11 | 单文件看板 `dashboard/index.html`：原生 JS 内联、无外链，右上角 CN/EN，6 页签（登录/看板/配置/报表/手动录入/事件日志），图表内联 SVG 自绘 | 单文件可离线打开；无 Chart.js 等外链；6 页签齐全；中英切换覆盖全部文案；图表随 API 数据刷新 |
| G12 | Win11 交付：PyInstaller onedir 打包脚本 + Win11 双击 bat + GitHub Actions（Windows runner 产出 zip artifact）；config/DB 放 exe 同级目录 | zip 解压后双击 bat 可见登录页；配置与 DB 在 exe 同级；替换 exe 后数据仍在；CI 产物可下载 |

### 6.2 P1（可后置）

| 编号 | 需求 | 验收要点 |
|---|---|---|
| H1 | Windows 重启自启（v1 F1） | 提供开机自启脚本/快捷方式说明 |
| H2 | 备份说明：复制 `.db` 文件（v1 F3） | WAL 一致性快照操作说明；v0.1.2 提供 `scripts/backup.py` 在线备份 |
| H3 | `reading` 90 天滚动清理（v1 F4） | 定时删除超期原始读数，汇总表不受影响 |
| H4 | 报表按 tariff 峰平谷分时段费用明细 | 分时段电费与合计对得上；v0.1.2 起日结算按小时逐段计价并落价格快照 |

### 6.3 P2（远期）

| 编号 | 需求 | 验收要点 |
|---|---|---|
| I1 | 冷却塔电极阈值告警（电导率/电流超限） | 写 `event_log` 并在看板提示 |
| I2 | 报表定时投递（邮件/共享目录文件） | 可按日/周/月定时生成 |
| I3 | 多工厂/多实例配置隔离 | 多套 config/DB 互不冲突 |

## 七、数据与容量约定

**容量基线**

| 项 | 约定 |
|---|---|
| 电表 | 按 100 个预留并预置；每表含 reading（累计电量）与 power（瞬时功率）两类 point |
| 冷却塔电极 | 5 组 × 3 点 = 15 个 point；沿用现有 node/device/point/meter，不新增表 |
| 采集/模拟频率 | 默认 `poll_interval` 60s；模拟器默认 5 分钟写一次 reading（config 可覆盖），以控制 90 天库体积 |
| 数据保留 | `reading` 90 天；`hourly`/`daily`/周/月聚合长期保留 |
| 存储 | 单一 SQLite 文件（WAL），与 exe 同级；周/月不落表，查询时聚合 |

**模拟数据字段范围**

| 数据项 | 类型 / 单位 | 模拟范围 | 说明 |
|---|---|---|---|
| 电表累计电量（reading） | 毫度整数，1 kWh = 1,000,000 毫度 | 单调增，单表日增约 50~800 kWh | 按 purpose/node 差异设定基线 |
| 电表瞬时功率（power） | f32，kW | 5~500 | 在基线上叠加小幅波动 |
| 冷却塔电导率 | f32，μS/cm | 200~3000 | 缓慢漂移 |
| 冷却塔电流 | f32，A | 0~50 | 随排污阀状态联动 |
| 排污阀状态 | u16，0/1 | 0/1 | 按周期开合，开时电流上扬 |
| 冷却塔机械排水量（手动录入） | f32，m³ | 由强哥录入 | 走 `manual_reading`，quality=3 |

## 八、UI 设计草稿（单文件 6 页签）

| 页签 | 关键区块 | 字段 / 操作 |
|---|---|---|
| 登录 | 密码输入、登录按钮；顶部只读展示 `data_mode`（simulate/live） | 复用 `/api/login`，httponly cookie；错密提示 |
| 看板 | 实时概览卡片（最新读数/更新时间/在线状态）；能耗趋势 SVG 折线（小时/日切换、node/purpose 筛选）；分栋与用途汇总；结算与电费 | 读 `/api/latest`、`/api/hourly`、`/api/daily`；空态提示 |
| 配置 | node/device/point/meter/tariff 表单 CRUD；点位试读；`data_mode` 切换（带确认） | device 协议三选一；试读展示原始寄存器十六进制 + 解析值 |
| 报表 | 类型（日/周/月）+ 时间范围 + 筛选（node/purpose/meter）→ SVG 表格/柱图预览 + 导出按钮 | 读 `/api/daily`、`/api/weekly`、`/api/monthly`；导出 .xlsx / .pdf |
| 手动录入 | 点位选择（默认冷却塔机械排水量表）+ ts/value/operator/remark + 最近录入列表 | 提交后写 `manual_reading` 并同步 `reading`/`reading_latest` |
| 事件日志 | 按 level/source/时间筛选的 `event_log` 表格 | 展示 collector/api/user 事件；右上角 CN/EN 全局切换 |

## 九、验收标准

1. **旧测试零回归**：现有 74 项测试全部通过（`pytest` 全绿），12 张原表结构与 API 行为不变。
2. **新增测试覆盖**：`data_mode` 默认与切换；模拟器产数写真实表并产出 hourly/daily；weekly/monthly 聚合与 daily 求和一致；Excel/PDF 导出文件非空、中文正常、数值一致；`manual_reading` 录入后三表事务一致（reading quality=3 + reading_latest 同步）；三协议解析与 CRC16/串口 mock；看板单文件与 6 页签/CN-EN 静态检查。
3. **Win11 包可启动**：CI zip 解压后双击 bat 进入登录页并可登录；config/DB 在 exe 同级；删除/替换 exe 后数据仍在。
4. **模拟数据下看板/报表有数**：默认 simulate 启动后看板实时概览与趋势有值，日/周/月报表非空且均可导出 .xlsx/.pdf；切 live 后模拟器停止、采集器接管。
5. **中文与离线**：PDF 中文不乱码、Excel 中文表头正常；看板单文件无外链、离线可开。

## 十、Open Questions（仅列影响实现的）

1. 周/月边界口径：周首为周一还是周日？月界为自然月 1 日？在 08:00 结算口径下，"周/月首日"从哪天 08:00 起算？（决定聚合 SQL 分组键）
2. 模拟 100 电表在 node/purpose 上的分布（几栋、各用途比例）？是否预置一份可直接导入的 100 表配置？
3. 冷却塔机械排水量表的点位归属（是否单独 device/point）、单位（m³ 还是 L）与量程？
4. 手动录入是否允许补录历史 ts？补录后是否需重算受影响的 hourly/daily/weekly/monthly（回填）？
5. Win11 目标机 Python 位数/架构与 GH Actions artifact 命名；是否需要附带 VC 运行库说明？
6. 报表 PDF 页面规格（A4 纵/横）与是否需要页眉页脚（公司名、生成时间、分页）？
