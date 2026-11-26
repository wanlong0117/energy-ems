"""Pydantic v2 请求模型 + 点位保存校验规则 + 统一 API 响应辅助。

所有写库请求先经这里的模型做类型/范围校验；point 另需满足四条硬规则并做
寄存器区间重叠检查（见 PointValidator / validate_point）。
"""
from __future__ import annotations

import math
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

import db
from settings import (
    BYTE_ORDERS,
    DATA_MODES,
    MANUAL_MAX_ABS_VALUE,
    PROTOCOLS,
    PURPOSES,
    TYPE_COUNT,
)


def ok(data: Any = None) -> dict[str, Any]:
    """统一成功响应：无业务数据时省略 data 字段。"""
    if data is None:
        return {"ok": True}
    return {"ok": True, "data": data}


def fail(error: str, status_code: int = 400) -> JSONResponse:
    """统一失败响应：{ok:false,error} + 明确 HTTP 状态码。"""
    return JSONResponse(status_code=status_code, content={"ok": False, "error": str(error)})


class PointValidator:
    """点位参数的硬性校验规则（失败一律抛 ValueError，便于 API 转 400）。"""

    @staticmethod
    def validate_reg_offset(value: Any) -> int:
        """reg_offset 必须是 [0, 65535] 的整数。"""
        try:
            offset = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("reg_offset 必须为整数") from exc
        if not 0 <= offset <= 65535:
            raise ValueError("reg_offset 必须在 0~65535 之间")
        return offset

    @staticmethod
    def validate_reg_count(value: Any) -> int:
        """reg_count 必须是 [1, 125] 的整数（Modbus 单次读上限）。"""
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("reg_count 必须为整数") from exc
        if not 1 <= count <= 125:
            raise ValueError("reg_count 必须在 1~125 之间")
        return count

    @staticmethod
    def validate_type_count(data_type: str, reg_count: int) -> None:
        """data_type 与 reg_count 必须严格匹配。"""
        dt = str(data_type).lower()
        if dt not in TYPE_COUNT:
            raise ValueError(f"不支持的 data_type: {data_type}")
        if int(reg_count) != TYPE_COUNT[dt]:
            raise ValueError(
                f"data_type={dt} 要求 reg_count={TYPE_COUNT[dt]}，当前为 {reg_count}"
            )

    @staticmethod
    def check_overlap(
        device_id: int,
        slave_id: int,
        func_code: int,
        offset: int,
        count: int,
        exclude_id: int | None = None,
    ) -> None:
        """同 device + slave + func 下，[offset, offset+count) 不得与已有区间重叠。"""
        rows = db.query(
            "SELECT id, reg_offset, reg_count FROM point "
            "WHERE device_id=? AND slave_id=? AND func_code=?",
            (int(device_id), int(slave_id), int(func_code)),
        )
        start: int = int(offset)
        end: int = start + int(count)
        for row in rows:
            if exclude_id is not None and int(row["id"]) == int(exclude_id):
                continue  # 更新时排除自身
            other_start: int = int(row["reg_offset"])
            other_end: int = other_start + int(row["reg_count"])
            if start < other_end and other_start < end:
                raise ValueError(
                    f"寄存器区间 [{start},{end}) 与点位 id={row['id']} "
                    f"的 [{other_start},{other_end}) 重叠"
                )


def validate_point(body: dict[str, Any], exclude_id: int | None = None) -> None:
    """点位保存的四条硬校验 + 区间重叠检查，失败抛 ValueError。"""
    offset: int = PointValidator.validate_reg_offset(body.get("reg_offset"))
    count: int = PointValidator.validate_reg_count(body.get("reg_count"))
    data_type: str = str(body.get("data_type", "")).lower()
    byte_order: str = str(body.get("byte_order", "ABCD")).upper()
    PointValidator.validate_type_count(data_type, count)

    try:
        func_code = int(body.get("func_code", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("func_code 必须为整数") from exc
    if func_code not in (3, 4):
        raise ValueError("func_code 仅支持 3（保持寄存器）或 4（输入寄存器）")
    if byte_order not in BYTE_ORDERS:
        raise ValueError(f"byte_order 仅支持 {', '.join(BYTE_ORDERS)}")

    device_id: Any = body.get("device_id")
    slave_id: Any = body.get("slave_id")
    if device_id is None or slave_id is None:
        raise ValueError("device_id 与 slave_id 为必填项")
    PointValidator.check_overlap(device_id, slave_id, func_code, offset, count, exclude_id)


class _BaseIn(BaseModel):
    """所有请求模型共享：忽略多余字段，避免前端多传字段导致 422。"""
    model_config = ConfigDict(extra="ignore")


class LoginIn(_BaseIn):
    password: str


class PasswordIn(_BaseIn):
    old_password: str
    new_password: str


class NodeIn(_BaseIn):
    name: str
    parent_id: int | None = None
    kind: str = "area"
    sort_order: int = 0
    is_active: int = 1
    remark: str | None = None


class DeviceIn(_BaseIn):
    name: str
    node_id: int | None = None
    protocol: str = "modbus_tcp"
    host: str
    port: int = 502
    timeout_ms: int = 3000
    retry: int = 2
    poll_interval: int = 60
    is_enabled: int = 1
    last_ok_at: int | None = None
    last_error: str | None = None
    remark: str | None = None

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, value: str) -> str:
        if value not in PROTOCOLS:
            raise ValueError(f"protocol 仅支持 {', '.join(PROTOCOLS)}")
        return value

    @field_validator("port")
    @classmethod
    def _port(cls, value: int, info: ValidationInfo) -> int:
        protocol: str = str(info.data.get("protocol", "modbus_tcp"))
        if protocol == "modbus_rtu_serial":
            # 串口设备 port 承载波特率（如 115200），不受 TCP 端口上限约束；
            # 0 或负数不是合法波特率，必须报错而不是静默回退 9600
            if not 50 <= value <= 4_000_000:
                raise ValueError("串口波特率必须在 50~4000000 之间")
            return value
        if not 1 <= value <= 65535:
            raise ValueError("port 必须在 1~65535 之间")
        return value

    @field_validator("timeout_ms")
    @classmethod
    def _timeout(cls, value: int) -> int:
        if not 1 <= value <= 60000:
            raise ValueError("timeout_ms 必须在 1~60000 之间")
        return value

    @field_validator("retry")
    @classmethod
    def _retry(cls, value: int) -> int:
        if not 0 <= value <= 10:
            raise ValueError("retry 必须在 0~10 之间")
        return value

    @field_validator("poll_interval")
    @classmethod
    def _poll(cls, value: int) -> int:
        if value < 1:
            raise ValueError("poll_interval 必须 >= 1 秒")
        return value


class PointIn(_BaseIn):
    name: str
    device_id: int
    slave_id: int
    func_code: int = 3
    reg_offset: int
    reg_count: int = 2
    data_type: str
    byte_order: str = "ABCD"
    scale: float = 1.0
    offset_const: float = 0.0
    min_val: float | None = None
    max_val: float | None = None
    unit: str | None = None
    is_enabled: int = 1
    remark: str | None = None

    @field_validator("reg_offset")
    @classmethod
    def _offset(cls, value: int) -> int:
        return PointValidator.validate_reg_offset(value)

    @field_validator("reg_count")
    @classmethod
    def _count(cls, value: int) -> int:
        return PointValidator.validate_reg_count(value)

    @field_validator("data_type")
    @classmethod
    def _data_type(cls, value: str) -> str:
        dt = str(value).lower()
        if dt not in TYPE_COUNT:
            raise ValueError(f"不支持的 data_type: {value}")
        return dt

    @field_validator("byte_order")
    @classmethod
    def _byte_order(cls, value: str) -> str:
        order = str(value).upper()
        if order not in BYTE_ORDERS:
            raise ValueError(f"byte_order 仅支持 {', '.join(BYTE_ORDERS)}")
        return order

    @field_validator("func_code")
    @classmethod
    def _func(cls, value: int) -> int:
        if int(value) not in (3, 4):
            raise ValueError("func_code 仅支持 3 或 4")
        return int(value)

    @model_validator(mode="after")
    def _check_type_count(self) -> "PointIn":
        """跨字段校验：data_type 与 reg_count 必须匹配。"""
        PointValidator.validate_type_count(self.data_type, self.reg_count)
        return self


class EnergyCategoryIn(_BaseIn):
    """能源分类请求模型。

    v0.1.4 起只接受电（kWh）：聚合、电价与报表都按「累计电量 kWh」口径实现，
    水表/气表接进来会被标成 kWh 并按电价计费，比不支持更危险。
    """

    name: str
    unit: str

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        unit: str = str(value).strip()
        if unit != "kWh":
            raise ValueError("当前版本仅支持电（unit=kWh）；水/气计量需等按分类换算的口径实现")
        return unit


class MeterIn(_BaseIn):
    """计量表请求模型。

    daily_8am 已于 v0.1.4 废弃：日切点是全局配置（EMS_DAY_START_HOUR），
    按表设置从未被聚合读取，留着只会让现场以为「这块表单独按 08:00 结算」。
    数据库列保留用于兼容旧库，但接口不再接受该字段。

    shutdown_power_w 是停机判定阈值（W）：该小时平均功率低于它就当停机，
    不计入日结算的覆盖率分母。留空则用全局 EMS_SHUTDOWN_POWER_W（0=不判定）。
    """

    name: str
    node_id: int
    category_id: int
    purpose: str = "total"
    is_counted: int = 1
    is_enabled: int = 1
    shutdown_power_w: float | None = None
    remark: str | None = None

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        if value not in PURPOSES:
            raise ValueError(f"purpose 仅支持 {', '.join(sorted(PURPOSES))}")
        return value

    @field_validator("shutdown_power_w")
    @classmethod
    def _shutdown_power(cls, value: float | None) -> float | None:
        """阈值必须有限且非负；inf/nan 会让「功率 < 阈值」恒成立，整天都被判停机。"""
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError("停机阈值必须是有限数值")
        if value < 0:
            raise ValueError("停机阈值不能为负（0 表示不判定停机）")
        if value > MANUAL_MAX_ABS_VALUE:
            raise ValueError("停机阈值过大，请确认单位是 W")
        return value


class MeterPointIn(_BaseIn):
    meter_id: int
    point_id: int
    role: str = "reading"

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        if value not in ("reading", "power"):
            raise ValueError("role 仅支持 reading 或 power")
        return value


class TariffIn(_BaseIn):
    name: str
    price_cent: int
    start_hour: int | None = None
    end_hour: int | None = None
    is_default: int = 0

    @field_validator("price_cent")
    @classmethod
    def _price(cls, value: int) -> int:
        if value < 0:
            raise ValueError("price_cent 不能为负数")
        return value

    @model_validator(mode="after")
    def _hours(self) -> "TariffIn":
        """时段电价必须成对出现且 0 ≤ start < end ≤ 24（本地小时，右开区间）。"""
        if (self.start_hour is None) != (self.end_hour is None):
            raise ValueError("start_hour 与 end_hour 必须同时填写或同时留空")
        if self.start_hour is not None and self.end_hour is not None:
            if not 0 <= int(self.start_hour) < int(self.end_hour) <= 24:
                raise ValueError("时段电价须满足 0 ≤ start_hour < end_hour ≤ 24")
        return self


class SettleIn(_BaseIn):
    day_start: int | None = None  # 省略则结算最近一个完整日（昨日 UTC 00:00）


class ManualIn(_BaseIn):
    """POST /api/manual 手动录入请求模型。"""

    point_id: int
    ts: int                       # Unix 秒 UTC，允许历史补录
    value: float
    operator: str
    remark: str | None = None

    @field_validator("ts")
    @classmethod
    def _ts(cls, value: int) -> int:
        if int(value) <= 0:
            raise ValueError("ts 必须为正的 Unix 秒时间戳")
        return int(value)

    @field_validator("operator")
    @classmethod
    def _operator(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("operator 不能为空")
        return str(value).strip()

    @field_validator("value")
    @classmethod
    def _value(cls, value: float) -> float:
        import math

        number: float = float(value)
        if not math.isfinite(number):
            raise ValueError("value 必须为有限数值（不接受 inf/nan）")
        if abs(number) > MANUAL_MAX_ABS_VALUE:
            raise ValueError(
                f"value 超出合理范围（|value| ≤ {MANUAL_MAX_ABS_VALUE:g}），请核对单位与小数点"
            )
        return number


class ReportQueryIn(_BaseIn):
    """报表导出查询模型（GET 用 Query 参数，模型定义供内部组装过滤项复用）。"""

    kind: str = "daily"
    fmt: str = "xlsx"
    meter_id: int | None = None
    from_: int | None = Field(default=None, alias="from")
    to: int | None = None
    purpose: str | None = None
    node_id: int | None = None
    is_final: int | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in ("daily", "weekly", "monthly"):
            raise ValueError("kind 仅支持 daily/weekly/monthly")
        return value

    @field_validator("fmt")
    @classmethod
    def _fmt(cls, value: str) -> str:
        if value not in ("xlsx", "pdf"):
            raise ValueError("fmt 仅支持 xlsx/pdf")
        return value


class ModeIn(_BaseIn):
    """POST /api/mode/switch 请求模型。"""

    data_mode: str

    @field_validator("data_mode")
    @classmethod
    def _data_mode(cls, value: str) -> str:
        mode: str = str(value).lower()
        if mode not in DATA_MODES:
            raise ValueError(f"data_mode 仅支持 {', '.join(DATA_MODES)}")
        return mode
