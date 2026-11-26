"""全局常量集中地：时区/08:00 口径、毫度换算、采集参数、字节序因子表、路径解析。

设计前提：现场 UTC+8，本地 08:00 == UTC 00:00。
因此 day_start 存 UTC 00:00 的 Unix 秒，hour_start 存 UTC 整点，
每日结算时刻 08:10(本地) == UTC 00:10。

v2 新增 frozen 双路径规则：
  可写数据（config.json / energy.db） -> app_dir()：frozen=exe 同级，源码=项目根；
  只读资源（schema.sql / dashboard）  -> resource_dir()：frozen=_MEIPASS，源码=项目根。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_SRC_DIR: Path = Path(__file__).resolve().parent
BASE_DIR: Path = _SRC_DIR  # 兼容 v1 引用；新代码一律使用 app_dir()/resource_dir()


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的环境中。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """可写数据目录：frozen = sys.executable 所在目录；源码 = 项目根目录。"""
    return Path(sys.executable).resolve().parent if is_frozen() else _SRC_DIR


def resource_dir() -> Path:
    """只读资源目录：frozen = sys._MEIPASS；源码 = 项目根目录。"""
    meipass: Any = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else _SRC_DIR


STATIC_DIR: Path = _SRC_DIR / "static"  # v1 兼容保留
DASHBOARD_DIR: Path = resource_dir() / "dashboard"  # v2 单文件看板只读目录

# ---------- 时区与 08:00 结算口径 ----------
TZ_OFFSET_HOURS: int = 8
SECONDS_PER_MINUTE: int = 60
SECONDS_PER_HOUR: int = 3600
SECONDS_PER_DAY: int = 86400
SECONDS_PER_WEEK: int = 7 * SECONDS_PER_DAY
DAY_START_UTC_HOUR: int = 0          # 本地 08:00 = UTC 00:00
SETTLE_UTC_HOUR: int = 0             # 本地 08:10 = UTC 00:10
SETTLE_UTC_MINUTE: int = 10
CATCHUP_LOOKBACK_DAYS: int = 30      # 启动时补算最近 30 天遗漏的日结算

# ---------- 周 / 月聚合口径 ----------
WEEK_FIRST_WEEKDAY: int = 0          # 0=周一（ISO 周），weekday() 直接可用
MONTH_FIRST_DAY: int = 1             # 自然月 1 日 UTC 00:00
WEEK_BUCKET_LABEL: str = "week"
MONTH_BUCKET_LABEL: str = "month"

# ---------- 电量单位 ----------
# schema.sql 中 energy_wh/cost_cent 为整数，1 kWh = 1,000,000 毫度
MILLI_PER_KWH: int = 1_000_000

# ---------- 采集参数 ----------
DEFAULT_POLL_INTERVAL: int = 60      # 秒
DEFAULT_TIMEOUT_MS: int = 3000
DEFAULT_RETRY: int = 2
PROBE_TIMEOUT_MS: int = 3000         # 试读/连接测试硬性上限，保证 3 秒内返回
RECONNECT_BACKOFF_BASE_SEC: float = 1.0
RECONNECT_BACKOFF_MAX_SEC: float = 60.0
MODBUS_MAX_REGISTERS: int = 125      # 单次读寄存器上限（协议约束）
MODBUS_MAX_BYTE_COUNT: int = 250

# ---------- 数据质量 ----------
QUALITY_OK: int = 0
QUALITY_TIMEOUT: int = 1
QUALITY_OUT_OF_RANGE: int = 2
QUALITY_MANUAL: int = 3

# ---------- 数据模式（simulate / live 互斥） ----------
DATA_MODE_DEFAULT: str = "simulate"
DATA_MODES: tuple[str, ...] = ("simulate", "live")
SIMULATE_INTERVAL_SEC: int = 300     # 模拟器写读数间隔，默认 5 分钟
SIMULATE_ROLLUP_SEC: int = 600       # 模拟模式小时/日汇总间隔，默认 10 分钟
LIVE_ROLLUP_SEC: int = 300           # 真实模式小时汇总间隔，默认 5 分钟
DEFAULT_BAUDRATE: int = 9600         # modbus_rtu_serial 默认波特率

# ---------- 数据保留与结算门槛 ----------
RETENTION_DAYS: int = 90             # 原始读数保留天数（超期按批删除）
RETENTION_BATCH: int = 5000          # 单批删除行数，避免长事务锁库
EVENT_LOG_MAX_ROWS: int = 20000      # 事件日志保留条数上限
SETTLE_MIN_COVERAGE: float = 0.9     # 日结算定稿门槛：小时覆盖与表覆盖均需 ≥90%

# ---------- 寄存器解析（data_type × byte_order） ----------
TYPE_COUNT: dict[str, int] = {
    "u16": 1, "s16": 1,
    "u32": 2, "s32": 2, "f32": 2,
    "u64": 4,
}
STRUCT_FORMAT: dict[str, str] = {
    "u16": ">H", "s16": ">h",
    "u32": ">I", "s32": ">i", "f32": ">f",
    "u64": ">Q",
}
BYTE_ORDERS: tuple[str, ...] = ("ABCD", "CDAB", "BADC", "DCBA")

# 字节序因子表：给定网络字节序(高字节在前)的原始字节，按下标重排后即为标准大端值。
# ABCD=不交换；CDAB=16 位字交换；BADC=字内字节交换；DCBA=整体倒序。
# 单寄存器(2 字节)没有"字交换"概念，CDAB 等价于 ABCD、BADC 等价于 DCBA。
BYTE_ORDER_FACTORS: dict[str, dict[int, tuple[int, ...]]] = {
    "ABCD": {
        2: (0, 1),
        4: (0, 1, 2, 3),
        8: (0, 1, 2, 3, 4, 5, 6, 7),
    },
    "CDAB": {
        2: (0, 1),
        4: (2, 3, 0, 1),
        8: (2, 3, 0, 1, 6, 7, 4, 5),
    },
    "BADC": {
        2: (1, 0),
        4: (1, 0, 3, 2),
        8: (1, 0, 3, 2, 5, 4, 7, 6),
    },
    "DCBA": {
        2: (1, 0),
        4: (3, 2, 1, 0),
        8: (7, 6, 5, 4, 3, 2, 1, 0),
    },
}

# ---------- 协议枚举 ----------
PROTOCOLS: tuple[str, ...] = (
    "modbus_tcp", "modbus_rtu_tcp", "modbus_rtu_serial", "manual",
)

# ---------- API 共享约定 ----------
RESOURCES: frozenset[str] = frozenset({
    "node", "device", "point", "energy_category",
    "meter", "meter_point", "tariff",
})
PURPOSES: frozenset[str] = frozenset({
    "total", "production", "hvac", "lighting", "other",
})
SESSION_COOKIE_NAME: str = "ems_session"
SESSION_TTL_SEC: int = 7 * SECONDS_PER_DAY
# 会话 Cookie 的 Secure 标志：纯内网 HTTP 下必须为 False，否则浏览器不回传 Cookie；
# 叠反向代理启用 HTTPS 后设 EMS_COOKIE_SECURE=1 即可打开。
SESSION_COOKIE_SECURE: bool = os.environ.get("EMS_COOKIE_SECURE", "") not in ("", "0", "false")

# ---------- HTTP 入口（单进程 uvicorn） ----------
HTTP_HOST: str = "0.0.0.0"
HTTP_PORT: int = 8000
