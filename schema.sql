-- 自研能耗管理系统 · 13 张业务表（替代 MyEMS 6.10.0）
-- 设计约定：主键 INTEGER 自增；时间存 Unix 秒(UTC)；电量存毫度(1 kWh = 1,000,000)；布尔存 0/1
-- 依据：IMA《自研能耗管理系统-替代MyEMS技术方案》v1.0 + 增量 PRD v2
-- 全部 CREATE TABLE IF NOT EXISTS：旧库升级重跑本脚本自动补表、原数据不丢
-- 旧库缺列的升级由 migrations.py 的版本化迁移负责（schema.sql 不 ALTER 已存在的表）

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

-- ============ 1. 空间节点（栋/楼层/产线/机房） ============
CREATE TABLE IF NOT EXISTS node (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  parent_id   INTEGER REFERENCES node(id),
  kind        TEXT    NOT NULL DEFAULT 'area',  -- area|line|room|device_area
  sort_order  INTEGER NOT NULL DEFAULT 0,
  is_active   INTEGER NOT NULL DEFAULT 1,
  is_demo     INTEGER NOT NULL DEFAULT 0,       -- 1=模拟器预置数据（live 模式不参与结算/报表）
  remark      TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_parent ON node(parent_id);

-- ============ 2. 数据源（一台网关/一个串口服务器 = 一个数据源） ============
CREATE TABLE IF NOT EXISTS device (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  node_id       INTEGER REFERENCES node(id),
  protocol      TEXT    NOT NULL,              -- modbus_tcp | modbus_rtu_tcp | modbus_rtu_serial | manual
  host          TEXT    NOT NULL,
  port          INTEGER NOT NULL DEFAULT 502,  -- TCP=端口；rtu_serial=波特率；manual=占位
  timeout_ms    INTEGER NOT NULL DEFAULT 3000,
  retry         INTEGER NOT NULL DEFAULT 2,
  poll_interval INTEGER NOT NULL DEFAULT 60,   -- 秒
  is_enabled    INTEGER NOT NULL DEFAULT 1,
  is_demo       INTEGER NOT NULL DEFAULT 0,
  last_ok_at    INTEGER,
  last_error    TEXT,
  remark        TEXT
);

-- ============ 3. 点位（JSON 拆成结构化列，核心改动） ============
CREATE TABLE IF NOT EXISTS point (
  id           INTEGER PRIMARY KEY,
  name         TEXT    NOT NULL,
  device_id    INTEGER NOT NULL REFERENCES device(id),
  slave_id     INTEGER NOT NULL,
  func_code    INTEGER NOT NULL DEFAULT 3,     -- 3=保持寄存器 4=输入寄存器
  reg_offset   INTEGER NOT NULL,
  reg_count    INTEGER NOT NULL DEFAULT 2,
  data_type    TEXT    NOT NULL,               -- u16|s16|u32|s32|f32|u64
  byte_order   TEXT    NOT NULL DEFAULT 'ABCD', -- ABCD|CDAB|BADC|DCBA（4 字符格式）
  scale        REAL    NOT NULL DEFAULT 1,
  offset_const REAL    NOT NULL DEFAULT 0,
  min_val      REAL,                           -- 越界判断下限，可空
  max_val      REAL,                           -- 越界判断上限，可空
  unit         TEXT,
  point_role   TEXT    NOT NULL DEFAULT 'energy', -- 已废弃：保留列兼容旧库，采集与聚合只看 meter_point.role
  is_enabled   INTEGER NOT NULL DEFAULT 1,
  is_demo      INTEGER NOT NULL DEFAULT 0,
  remark       TEXT,
  UNIQUE(device_id, slave_id, func_code, reg_offset)
);
CREATE INDEX IF NOT EXISTS idx_point_device ON point(device_id);

-- ============ 4. 能源分类 ============
CREATE TABLE IF NOT EXISTS energy_category (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  name  TEXT NOT NULL,        -- 电 / 水 / 天然气
  unit  TEXT NOT NULL         -- kWh / m3
);

-- ============ 5. 计量表 ============
CREATE TABLE IF NOT EXISTS meter (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  node_id       INTEGER NOT NULL REFERENCES node(id),
  category_id   INTEGER NOT NULL REFERENCES energy_category(id),
  purpose       TEXT    NOT NULL DEFAULT 'total',  -- total|production|hvac|lighting|other
  is_counted    INTEGER NOT NULL DEFAULT 1,
  daily_8am     INTEGER NOT NULL DEFAULT 1,        -- 已废弃：日切点由 EMS_DAY_START_HOUR 全局配置
  is_enabled    INTEGER NOT NULL DEFAULT 1,
  is_demo       INTEGER NOT NULL DEFAULT 0,        -- 1=模拟器预置表，live 模式下自动停用并排除结算
  remark        TEXT
);
CREATE INDEX IF NOT EXISTS idx_meter_node ON meter(node_id);

-- ============ 6. 表-点位关联 ============
CREATE TABLE IF NOT EXISTS meter_point (
  meter_id  INTEGER NOT NULL REFERENCES meter(id),
  point_id  INTEGER NOT NULL REFERENCES point(id),
  role      TEXT    NOT NULL DEFAULT 'reading',  -- reading(累计电量)|power(瞬时功率)
  PRIMARY KEY (meter_id, point_id, role)
);

-- ============ 7. 原始读数（只保留 90 天） ============
CREATE TABLE IF NOT EXISTS reading (
  point_id  INTEGER NOT NULL,
  ts        INTEGER NOT NULL,
  value     REAL    NOT NULL,
  quality   INTEGER NOT NULL DEFAULT 0,  -- 0=正常 1=超时 2=越界 3=人工
  PRIMARY KEY (point_id, ts)
) WITHOUT ROWID;
-- 主键是 (point_id, ts)，按时间扫描（90 天清理、按时段聚合）用不上前缀，单独建 ts 索引
CREATE INDEX IF NOT EXISTS idx_reading_ts ON reading(ts);

-- ============ 8. 最新值（看板轮询只查这张） ============
CREATE TABLE IF NOT EXISTS reading_latest (
  point_id  INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,
  value     REAL    NOT NULL,
  quality   INTEGER NOT NULL DEFAULT 0
);

-- ============ 9. 小时汇总 ============
CREATE TABLE IF NOT EXISTS hourly (
  meter_id   INTEGER NOT NULL,
  hour_start INTEGER NOT NULL,
  energy_wh  INTEGER NOT NULL,   -- 毫度
  avg_power  REAL,
  max_power  REAL,
  sample_cnt INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (meter_id, hour_start)
) WITHOUT ROWID;

-- ============ 10. 日汇总（看板主查询表） ============
CREATE TABLE IF NOT EXISTS daily (
  meter_id    INTEGER NOT NULL,
  day_start   INTEGER NOT NULL,   -- 08:00 口径起点
  energy_wh   INTEGER NOT NULL,
  peak_power  REAL,
  avg_power   REAL,
  cost_cent   INTEGER,            -- 电费，单位：分
  is_final    INTEGER NOT NULL DEFAULT 0,
  covered_hours INTEGER NOT NULL DEFAULT 0,  -- 当日有数据的小时数（0~24），报表据此判断可信度
  price_cent  REAL,               -- 当日加权平均单价（分/kWh），电价快照
  tariff_snapshot TEXT,           -- 计费时实际套用的时段电价明细（JSON）
  PRIMARY KEY (meter_id, day_start)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily(day_start);

-- ============ 11. 电价（峰平谷，简化版） ============
CREATE TABLE IF NOT EXISTS tariff (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  price_cent  INTEGER NOT NULL,   -- 分/kWh
  start_hour  INTEGER,
  end_hour    INTEGER,
  is_default  INTEGER NOT NULL DEFAULT 0
);

-- ============ 12. 告警与审计（合一） ============
CREATE TABLE IF NOT EXISTS event_log (
  id        INTEGER PRIMARY KEY,
  ts        INTEGER NOT NULL,
  level     TEXT NOT NULL,       -- info|warn|error
  source    TEXT NOT NULL,       -- collector|api|user
  code      TEXT,
  message   TEXT NOT NULL,
  ref_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event_log(ts);

-- ============ 13. 手动录入（冷却塔机械排水量 / 未来离线表补录） ============
-- UNIQUE(point_id, ts)：重复录入=覆盖；quality=3 同步写入 reading / reading_latest
CREATE TABLE IF NOT EXISTS manual_reading (
  id        INTEGER PRIMARY KEY,
  point_id  INTEGER NOT NULL REFERENCES point(id),
  ts        INTEGER NOT NULL,          -- Unix 秒 UTC，允许历史补录
  value     REAL    NOT NULL,
  operator  TEXT    NOT NULL,
  remark    TEXT,
  UNIQUE (point_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_manual_point_ts ON manual_reading(point_id, ts);

-- ============ 初始数据：三类能源 ============
INSERT OR IGNORE INTO energy_category (id, name, unit) VALUES
  (1, '电', 'kWh'),
  (2, '水', 'm3'),
  (3, '天然气', 'm3');
