# 能耗管理系统 energy_ems · 产品需求文档（简单 PRD）

> 依据 IMA《自研能耗管理系统-替代 MyEMS 技术方案》v1.0 与现场实测；数据模型以现有 `schema.sql` 12 张表为准。
> 约定：下文的**优先级 P0/P1/P2** 表示需求重要性（Must / Should / Nice）；**Phase 0~6** 表示实施里程碑，二者为不同维度。

## 一、项目信息

| 项目 | 内容 |
|---|---|
| Language | 中文 |
| Programming Language | Python + FastAPI + SQLite + 原生 JS 单文件看板 |
| Project Name | `energy_ems` |
| 架构约束 | 单进程、单个 SQLite 文件、一个网页；替代 MyEMS 6 进程 + 13 库 + 166 表 |
| 明确排除 | Vite/React/MUI/Tailwind、Docker、微服务、ORM、Redis/MQ、用户角色权限 |
| 认证 | 一个固定密码 + session cookie（httponly，无角色） |
| 现场环境 | Windows；采集机 192.0.2.50；E90 双通道网关 192.0.2.11:8886、192.0.2.21:8886（Modbus TCP） |
| 现状 | 已有 P0 骨架：`schema.sql`（12 表）、`db.py`、`auth.py`、`requirements.txt` |
| 本轮范围 | **Phase 0~3**（数据采进来、配起来、算出来）；Phase 4~6 后置 |

**原始需求复述**：强哥工厂内网部署的 MyEMS 6.10.0 实际仅用到不到 5% 功能，现场实测确认自研只做这 5%——用单进程 + 单 SQLite 文件 + 一个网页，实现 Modbus TCP 采集、点位配置与试读、08:00 口径小时/日汇总与电费、按用途分栋拆分、CSV 导出与看板展示；砍掉碳排、计费系统、预测、FDD、工单、储能/微电网/光伏等全部非必需模块。

## 二、产品定义

### 2.1 产品目标

1. **极简替代**：以单进程、单库、单页替代 MyEMS 的多进程多库，只保留实际使用的 5% 能力，把部署运维降到最低——重启自动恢复、备份即复制 `.db`。
2. **稳定采集**：自写轻量 Modbus TCP 客户端稳定采集电表，连续 7 天数据完整率 >99%、断网自动重连、与 MyEMS 同表同日累计偏差 <0.5%。
3. **可信结算与可视**：按 08:00→次日 08:00 口径产出小时/日能耗与电费，支持按 purpose 分栋拆分，通过配置后台与单文件看板让数据看得见、配得动。

### 2.2 用户故事

1. As a 工厂运维（强哥），I want 单进程单库部署且 Windows 重启后自动拉起，so that 不再维护 MyEMS 的多服务多库，备份只需复制一个 `.db` 文件。
2. As a 工厂运维，I want 系统自写客户端经 Modbus TCP 从两台 E90 网关读表并断网重连，so that 数据连续完整且不引入 pymodbus 等重依赖。
3. As a 工厂运维，I want 在网页表单里配置设备/点位参数并一键试读，so that 接入新电表不用改 MyEMS 的 JSON 或写代码。
4. As a 工厂管理者，I want 按 08:00 结算口径查看分栋/按用途的日能耗与电费，so that 与 MyEMS 对账偏差控制在 0.5% 以内。
5. As a 使用者，I want 单文件看板直接读取真实 API 数据，so that 一打开就看到最新读数、能耗趋势与结算结果。

## 三、技术规范

### 3.1 需求池

> 优先级 P0/P1/P2 = 重要性；「范围」列标注所属里程碑及是否本轮交付。整体验收标准见 3.1.4。

#### 3.1.1 P0（Must have — 本轮全部交付）

| 编号 | 需求 | 验收要点 | 范围 |
|---|---|---|---|
| A1 | 单进程 FastAPI + Uvicorn 服务，全局单 SQLite 连接 | 仅 1 进程、单个 `energy.db`；无 Docker/Redis/MQ | 本轮·Phase0 |
| A2 | 12 张表 schema 初始化（以 `schema.sql` 为准） | `init_db` 幂等；三类能源初始数据 | 本轮·Phase0 |
| A3 | 固定密码登录 + session cookie | 错密 401、未登录拒绝；无角色权限 | 本轮·Phase0 |
| B1 | 自写 asyncio Modbus TCP 客户端（约 120 行，不用 pymodbus） | 支持 func 3/4；按 device 超时/重试参数 | 本轮·Phase1 |
| B2 | 寄存器合并读取 | 相邻寄存器合并为单次请求，减少往返 | 本轮·Phase1 |
| B3 | 按 point 配置解析（data_type/byte_order/scale/offset） | 越界标记 quality | 本轮·Phase1 |
| B4 | 写 `reading` 并更新 `reading_latest` | (point_id, ts) 唯一；最新值可供看板轮询 | 本轮·Phase1 |
| B5 | 断网自动重连 + 事件日志 | 恢复后自动重连并写入 `event_log` | 本轮·Phase1 |
| C1 | node/device/point/meter/meter_point/tariff/energy_category 配置 CRUD | 表单增删改查；点位参数由 MyEMS JSON 拆成结构化列 | 本轮·Phase2 |
| C2 | 点位试读接口 | POST 试读，3 秒内返回原始寄存器与解析值 | 本轮·Phase2 |
| C3 | 采集配置热生效 | 修改 device/point 后重载采集任务，无需重启 | 本轮·Phase2 |
| D1 | 小时汇总 `hourly` | 每表每小时 energy_wh / avg_power / max_power / sample_cnt | 本轮·Phase3 |
| D2 | 日汇总 `daily`（08:00→次日 08:00 口径） | day_start=08:00；is_final 标记；08:10 自动结算 | 本轮·Phase3 |
| D3 | 按 purpose + node 分栋拆分统计 | purpose = total/production/hvac/lighting/other 分组 | 本轮·Phase3 |
| D4 | 电费计算 `cost_cent` | 电量 × 单价（tariff）；仅简单电费，不做计费系统 | 本轮·Phase3 |

#### 3.1.2 P1（Should have — 后置）

| 编号 | 需求 | 验收要点 | 范围 |
|---|---|---|---|
| E1 | 单文件看板读取真实 API 数据 | 原生 JS 单文件；读 `/api/latest`、`/api/daily`、`/api/hourly`；无 mock | 后置·Phase4 |
| E2 | 看板关键页面（实时概览 / 能耗趋势 / 分栋电费） | 字段与布局见 3.2 | 后置·Phase4 |
| E3 | CSV 导出（日报 / 明细） | 按日期、按表导出 | 后置·Phase5 |

#### 3.1.3 P2（Nice to have — 后置）

| 编号 | 需求 | 验收要点 | 范围 |
|---|---|---|---|
| F1 | Windows 重启自动拉起 | 开机自启脚本/服务 | 后置·Phase6 |
| F2 | 事件日志查看页 | `event_log` 展示 collector/api/user 事件 | 后置·Phase6 |
| F3 | 备份 = 复制 `.db` 文件 | 备份说明（WAL 一致性快照） | 后置·Phase6 |
| F4 | `reading` 90 天滚动清理 | 定时删除超期原始读数 | 后置·Phase6 |

#### 3.1.4 整体验收标准

连续运行 7 天数据完整率 >99%；与 MyEMS 同表同日累计偏差 <0.5%；08:10 自动结算；试读接口 3 秒内返回原始寄存器；断网恢复自动重连；Windows 重启自动拉起；备份即复制 `.db`。（为整体验收口径，不要求本轮全部达成，后置项按 Phase 交付。）

### 3.2 UI 设计草稿

#### 配置后台（本轮 Phase2，网页表单）

| 页面 | 关键字段 | 操作 |
|---|---|---|
| 设备管理 `device` | name / protocol(modbus_tcp\|modbus_rtu_tcp) / host / port(默认 8886) / timeout_ms / retry / poll_interval / is_enabled | 增改查、启停、连接测试；预置网关 192.0.2.11:8886、192.0.2.21:8886 |
| 点位管理 `point` | name / device_id / slave_id / func_code(3\|4) / reg_offset / reg_count / data_type(u16\|s16\|u32\|s32\|f32\|u64) / byte_order(ABCD\|CDAB\|BADC\|DCBA) / scale / offset_const / min_val / max_val / unit / point_role / is_enabled | 增改查；**试读按钮** → 展示原始寄存器（十六进制）+ 按配置解析值 |
| 计量表管理 `meter` | name / node_id / category_id / purpose / is_counted / daily_8am / is_enabled | 增改查；关联 `meter_point`（选 point + role=reading\|power） |
| 节点 / 电价 `node`/`tariff` | 节点树增删改；tariff: name / price_cent / start_hour / end_hour / is_default | 树形维护、默认电价设定 |

#### 看板（后置 Phase4，单文件 HTML + 原生 JS，数据全部来自真实 API）

| 区块 | 内容 | 数据源 |
|---|---|---|
| 实时概览 | 各表最新读数、更新时间、采集在线/离线状态卡片 | `/api/latest`（`reading_latest`） |
| 能耗趋势 | 按日/小时折线或柱状；按 node / purpose 筛选与分栋展示 | `/api/daily`、`/api/hourly` |
| 结算与电费 | 当日/累计电量、电费（cost_cent）、is_final 结算状态；CSV 导出入口（Phase5） | `/api/daily` |

### 3.3 Open Questions

1. 采集机 192.0.2.50 上 MyEMS 是否继续运行？两系统同读 E90 网关是否会因 Modbus 连接数冲突？对账期是否需要切换采集窗口？
2. 08:10 自动结算的实现方式：内置 asyncio 定时 vs Windows 任务计划调用接口？停机/断网错过结算是否补算、如何补算？
3. tariff 峰平谷的具体时段与单价（分/kWh）如何设定？MyEMS 的 08:00 结算口径是否含费率切换？
4. 固定密码的修改方式：当前需改 `config.json` + 重启，是否需要网页表单入口？
5. `reading` 仅保留 90 天，超期清理由系统定时执行还是人工？`hourly`/`daily` 是否永久保留？
6. 点位总量与采集频率（poll_interval）预期？用于评估单进程采集压力与合并读取收益。
