"""全局常量集中地：时区/08:00 口径、毫度换算、采集参数、字节序因子表、路径解析。

设计前提：现场 UTC+8，本地 08:00 == UTC 00:00。
因此 day_start 存 UTC 00:00 的 Unix 秒，hour_start 存 UTC 整点，
每日结算时刻 08:10(本地) == UTC 00:10。

v2 新增 frozen 双路径规则：
  可写数据（config.json / energy.db） -> app_dir()：frozen=exe 同级，源码=项目根；
  只读资源（schema.sql / dashboard）  -> resource_dir()：frozen=_MEIPASS，源码=项目根。
"""
from __future__ import annotations

import math
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

# ---------- 时区与日切点（可配置） ----------
SECONDS_PER_MINUTE: int = 60
SECONDS_PER_HOUR: int = 3600
SECONDS_PER_DAY: int = 86400
SECONDS_PER_WEEK: int = 7 * SECONDS_PER_DAY


def _env_int(name: str, default: int) -> int:
    """读取整数型环境变量，非法值回退默认（配置错误不应让服务起不来）。"""
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """读取浮点型环境变量；非法值与 inf/nan 一律回退默认值。

    inf 必须挡住：`EMS_SHUTDOWN_POWER_W=inf` 会让「功率 < 阈值」恒成立，
    全天 24 小时都被判成停机、覆盖率分母变成 0，报表直接失去意义。
    """
    try:
        value: float = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


# 时区偏移：现场默认 UTC+8。报表的「本地时刻」「分时电价时段」都按它换算。
TZ_OFFSET_HOURS: int = _env_int("EMS_TZ_OFFSET_HOURS", 8) % 24
# 日切点：本地几点算新的一天，默认 08:00（现场抄表口径）。
# 08:00 本地 = UTC 00:00，故默认值与历史行为完全一致；改成别的整点需重新结算历史。
DAY_START_LOCAL_HOUR: int = _env_int("EMS_DAY_START_HOUR", 8) % 24
# 日切点对应的 UTC 整点：08:00 本地 - 8 小时 = 00:00 UTC
DAY_BOUNDARY_UTC_HOUR: int = (DAY_START_LOCAL_HOUR - TZ_OFFSET_HOURS) % 24
DAY_START_UTC_HOUR: int = DAY_BOUNDARY_UTC_HOUR  # 兼容旧命名
SETTLE_UTC_HOUR: int = DAY_BOUNDARY_UTC_HOUR     # 日结在日切点后 10 分钟
SETTLE_UTC_MINUTE: int = 10
CATCHUP_LOOKBACK_DAYS: int = 30      # 启动时补算最近 30 天遗漏的日结算


def day_floor(ts: int) -> int:
    """返回 ts 所属「日切点」那一天的起点（Unix 秒 UTC）。

    默认日切点 UTC 00:00 时等价于 round-down 到整日，
    因此与 v1/v2 的 day_start 完全一致；改 EMS_DAY_START_HOUR 后才整体平移。
    """
    offset: int = DAY_BOUNDARY_UTC_HOUR * SECONDS_PER_HOUR
    return ((int(ts) - offset) // SECONDS_PER_DAY) * SECONDS_PER_DAY + offset


def local_hour(ts: int) -> int:
    """Unix 秒 -> 现场本地小时（0~23），用于分时电价时段匹配。"""
    return (int(ts) // SECONDS_PER_HOUR + TZ_OFFSET_HOURS) % 24

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

# ---------- 人工录入边界 ----------
# 单条人工读数绝对值上限（工程值）。超过这个量级不可能是真实抄见值，
# 且 ×1e6 转毫度后会溢出 SQLite INTEGER，直接把整轮汇总打断。
MANUAL_MAX_ABS_VALUE: float = 1e9

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

# ---------- 停机判定（影响日结算覆盖率） ----------
# 现场口径：车间夜间停产时设备照常在线，功率掉到待机值（几十瓦），电量≈0。
# 这种小时不该算进「应有数据」的分母，否则 24 小时里 22 小时停机的一天永远定稿不了。
# 判定依据是 hourly.avg_power（该小时功率点均值）< 阈值 → 该小时计入停机。
# 全局默认 0 表示**关闭**：升级后行为与旧版完全一致，需现场显式给阈值才生效。
# 单表可在「计量表」里填 shutdown_power_w 覆盖（不同产线待机功率差很多）。
SHUTDOWN_POWER_W: float = _env_float("EMS_SHUTDOWN_POWER_W", 0.0)
if SHUTDOWN_POWER_W < 0:
    SHUTDOWN_POWER_W = 0.0
# 缺口推断上限：某段「没有小时数据」的缺口，只有夹在两段已观测停机之间、
# 且连续长度不超过该值时，才按停机处理。超过它的缺口一律算作「采集中断」，
# 记成生产小时、拖住定稿——半天的采集空白本来就不该由系统替人签字。
SHUTDOWN_GAP_MAX_HOURS: int = max(0, _env_int("EMS_SHUTDOWN_GAP_MAX_HOURS", 3))

# ---------- 数据保留与结算门槛 ----------
RETENTION_DAYS: int = 90             # 原始读数保留天数（超期按批删除）
RETENTION_BATCH: int = 5000          # 单批删除行数，避免长事务锁库
EVENT_LOG_MAX_ROWS: int = 20000      # 事件日志保留条数上限
SETTLE_MIN_COVERAGE: float = 0.9     # 日结算定稿门槛：小时覆盖与表覆盖均需 ≥90%

# ---------- 查询默认时间窗（无参查询必须收敛，否则整表进内存） ----------
# 100 块表一年 ≈ 87.6 万行 hourly，单次响应可达 180 MB，浏览器与工控机都撑不住。
QUERY_HOURLY_DEFAULT_DAYS: int = 3   # /api/hourly 未给时间窗时默认最近 3 天
# daily 给足一年：既保住「不传参数查年报」的老用法，又能挡住「三年历史一次拉完」
QUERY_DAILY_DEFAULT_DAYS: int = 366
QUERY_MAX_ROWS: int = 50000          # 单次查询行数硬上限，超出按时间倒序截断
EVENT_LOG_THROTTLE_SEC: int = 60     # 同类事件在该窗口内折叠为一条，避免刷爆审计日志

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
# 反向代理后取真实来源：默认关闭。
# 挂在 Nginx/Caddy 后面时 request.client.host 恒为 127.0.0.1，登录失败锁定会变成
# 「任何人输错 5 次就锁全厂」——包括正在输正确密码的管理员。开启后按
# X-Forwarded-For 第一跳识别来源；只能在后端不可被直连（仅代理可达）时开启，
# 否则伪造该头即可绕过锁定。
TRUST_PROXY_HEADERS: bool = os.environ.get("EMS_TRUST_PROXY_HEADERS", "") not in ("", "0", "false")

# ---------- HTTP 入口（单进程 uvicorn） ----------
# 默认只绑本机：默认密码是公开的，一旦直接监听 0.0.0.0，同一网段任何人都能抢先
# 用默认口令登录并把密码改掉，把现场管理员锁在外面（v0.1.3 复审 P1-6）。
# 需要手机/其他电脑访问时显式设置 EMS_ALLOW_LAN=1 或 EMS_HOST=0.0.0.0。
ALLOW_LAN: bool = os.environ.get("EMS_ALLOW_LAN", "") not in ("", "0", "false")
HTTP_HOST: str = os.environ.get("EMS_HOST") or ("0.0.0.0" if ALLOW_LAN else "127.0.0.1")
HTTP_PORT: int = _env_int("EMS_PORT", 8000)

# ---------- 版本 ----------
# 单一事实源：FastAPI 元信息、/healthz、看板侧栏都读它，避免三处各写一个版本号
APP_VERSION: str = "0.1.9"
