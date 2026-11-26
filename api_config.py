"""配置后台 API：7 类资源 CRUD、点位试读、设备连接测试、事件日志、采集热生效。

全部路由挂 require_auth 依赖；成功 {ok:true,data?}，失败 {ok:false,error} + 状态码。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable, Coroutine

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

import db
from auth import require_auth
from collector import collector
from modbus import ModbusError
from schemas import (
    DeviceIn,
    EnergyCategoryIn,
    MeterIn,
    MeterPointIn,
    NodeIn,
    PointIn,
    TariffIn,
    fail,
    ok,
    validate_point,
)
from settings import DEFAULT_TIMEOUT_MS, PROBE_TIMEOUT_MS, RESOURCES

router = APIRouter(prefix="/api", tags=["config"], dependencies=[Depends(require_auth)])

MODEL_MAP: dict[str, type[BaseModel]] = {
    "node": NodeIn,
    "device": DeviceIn,
    "point": PointIn,
    "energy_category": EnergyCategoryIn,
    "meter": MeterIn,
    "meter_point": MeterPointIn,
    "tariff": TariffIn,
}


class ConfigService:
    """配置资源服务层：通用 CRUD + 点位校验 + 设备连接测试。"""

    # 外键前置校验表：字段 -> 被引用表。写入前显式查一次，错误码从 500 降到 400。
    FK_RULES: dict[str, dict[str, str]] = {
        "node": {"parent_id": "node"},
        "device": {"node_id": "node"},
        "point": {"device_id": "device"},
        "meter": {"node_id": "node", "category_id": "energy_category"},
        "meter_point": {"meter_id": "meter", "point_id": "point"},
    }

    @classmethod
    def _check_refs(cls, resource: str, data: dict[str, Any]) -> None:
        """校验被引用记录是否存在；不存在抛 ValueError（映射为 400）。"""
        for field, table in cls.FK_RULES.get(resource, {}).items():
            if field not in data:
                continue
            value: Any = data.get(field)
            if value is None:
                continue
            if db.query_one(f"SELECT 1 FROM {table} WHERE id=?", (int(value),)) is None:
                raise ValueError(f"{field} 指向的 {table} id={value} 不存在")

    @staticmethod
    def _check_resource(resource: str) -> None:
        if resource not in RESOURCES:
            raise ValueError(
                f"不支持的资源: {resource}，可选 {', '.join(sorted(RESOURCES))}"
            )

    @staticmethod
    def _id_col(resource: str) -> str:
        # meter_point 为复合主键表，用隐式 rowid 作为对外 id
        return "rowid" if resource == "meter_point" else "id"

    @staticmethod
    def _cols(resource: str) -> str:
        return "rowid AS id, meter_id, point_id, role" if resource == "meter_point" else "*"

    @staticmethod
    def _order(resource: str) -> str:
        if resource == "meter_point":
            return " ORDER BY meter_id, point_id, role"
        return " ORDER BY id"

    def list(self, resource: str) -> list[dict[str, Any]]:
        """列出某资源的全部记录。"""
        self._check_resource(resource)
        rows = db.query(f"SELECT {self._cols(resource)} FROM {resource}{self._order(resource)}")
        return [dict(row) for row in rows]

    def get(self, resource: str, item_id: int) -> dict[str, Any]:
        """按 id 读取单条记录。"""
        self._check_resource(resource)
        row = db.query_one(
            f"SELECT {self._cols(resource)} FROM {resource} WHERE {self._id_col(resource)}=?",
            (int(item_id),),
        )
        if row is None:
            raise LookupError(f"{resource} id={item_id} 不存在")
        return dict(row)

    def create(self, resource: str, body: dict[str, Any]) -> dict[str, Any]:
        """新增记录；point 保存时执行硬性校验，写入前校验外键存在性。"""
        self._check_resource(resource)
        data: dict[str, Any] = MODEL_MAP[resource](**body).model_dump()
        if resource == "point":
            validate_point(data)
        self._check_refs(resource, data)
        columns: list[str] = list(data.keys())
        placeholders: str = ", ".join("?" for _ in columns)
        sql: str = (
            f"INSERT INTO {resource} ({', '.join(columns)}) VALUES ({placeholders})"
        )
        new_id: int = db.execute(sql, tuple(data[column] for column in columns))
        if resource in ("device", "point"):
            collector.reload()  # 采集配置变更热生效
        return self.get(resource, new_id)

    def update(self, resource: str, item_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """局部更新记录；point 校验时排除自身 id 的区间。"""
        self._check_resource(resource)
        old: dict[str, Any] = self.get(resource, item_id)
        merged: dict[str, Any] = dict(old)
        merged.update(body)
        data: dict[str, Any] = MODEL_MAP[resource](**merged).model_dump()
        if resource == "point":
            validate_point(merged, exclude_id=int(item_id))

        fields: dict[str, Any] = {
            key: value for key, value in data.items() if key in body and key != "id"
        }
        self._check_refs(resource, fields)
        if fields:
            assignments: str = ", ".join(f"{key}=?" for key in fields)
            db.execute(
                f"UPDATE {resource} SET {assignments} WHERE {self._id_col(resource)}=?",
                (*fields.values(), int(item_id)),
            )
        if resource in ("device", "point"):
            collector.reload()
        return self.get(resource, item_id)

    def delete(self, resource: str, item_id: int) -> bool:
        """删除记录；device/point 删除后触发采集重载。"""
        self._check_resource(resource)
        self.get(resource, item_id)  # 不存在则抛 LookupError -> 404
        db.execute(
            f"DELETE FROM {resource} WHERE {self._id_col(resource)}=?", (int(item_id),)
        )
        if resource in ("device", "point"):
            collector.reload()
        return True

    async def test_device(self, device_id: int) -> dict[str, Any]:
        """连接测试：有点位则实际读一次寄存器，无点位只验证链路可达。

        必须按 device.protocol 分派客户端（走 collector._client_for）：
        旧实现固定用 ModbusClient(host, port)，串口设备（host=COM3、port=4800 波特率）
        会被当成 TCP 去连 COM3:4800，报「nodename nor servname provided」，
        现场只会以为串口线坏了——而同一台设备的「点位试读」走的是正确分派，
        两处结果不一致最难排查。
        """
        row: Any = db.query_one("SELECT * FROM device WHERE id=?", (int(device_id),))
        if row is None:
            raise LookupError(f"device id={device_id} 不存在")
        device: dict[str, Any] = dict(row)
        timeout_ms: int = min(
            int(device.get("timeout_ms") or DEFAULT_TIMEOUT_MS), PROBE_TIMEOUT_MS
        )
        probe_device: dict[str, Any] = dict(device)
        probe_device["timeout_ms"] = timeout_ms
        probe_device["retry"] = 0
        client = collector._client_for(probe_device)
        is_serial: bool = str(device.get("protocol") or "").lower() == "modbus_rtu_serial"
        started: float = time.monotonic()
        try:
            point_row: Any = db.query_one(
                "SELECT * FROM point WHERE device_id=? AND is_enabled=1 ORDER BY id LIMIT 1",
                (int(device_id),),
            )
            if point_row is None:
                await client.connect()  # 无点位：只验证链路可达
                registers: list[int] = []
                link: str = "串口已打开" if is_serial else "TCP 连接成功"
                message: str = f"{link}（该设备未配置点位，未读取寄存器）"
            else:
                point: dict[str, Any] = dict(point_row)
                registers = await client.read_registers(
                    int(point["slave_id"]), int(point["func_code"]),
                    int(point["reg_offset"]), int(point["reg_count"]),
                )
                message = f"读取成功，返回 {len(registers)} 个寄存器"
            db.execute(
                "UPDATE device SET last_ok_at=?, last_error=NULL WHERE id=?",
                (int(time.time()), int(device_id)),
            )
        except (ModbusError, OSError, ValueError) as exc:
            db.execute(
                "UPDATE device SET last_error=? WHERE id=?", (str(exc), int(device_id))
            )
            db.log_event("error", "api", "DEVICE_TEST_FAIL", str(exc), int(device_id))
            raise RuntimeError(f"设备连接测试失败: {exc}") from exc
        finally:
            await client.close()

        return {
            "device_id": int(device_id),
            "ok": True,
            "message": message,
            "raw_regs": registers,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


config_service: ConfigService = ConfigService()


def _err_text(exc: Exception) -> str:
    """Pydantic 错误只取首条简明信息，避免把模型内部细节暴露给前端。"""
    if isinstance(exc, ValidationError):
        errors: list[dict[str, Any]] = exc.errors()
        if errors:
            return str(errors[0].get("msg", str(exc)))
    return str(exc)


def _fail_internal(code: str, exc: Exception) -> JSONResponse:
    """内部错误统一对外话术：原文只进事件日志，不回显给调用方。"""
    db.log_event("error", "api", code, f"未预期的内部错误: {type(exc).__name__}: {exc}")
    return fail("服务器内部错误", 500)


def _conflict_text(exc: sqlite3.IntegrityError) -> tuple[str, str]:
    """把 sqlite3.IntegrityError 拆成 (事件码, 人话)。

    同一个异常类覆盖两种完全不同的现场问题：
      · UNIQUE  —— 重复关联（同一 meter 重复挂同一 point），操作员要的是「已存在」；
      · FOREIGN KEY —— 删除/改键被别的表引用，操作员要的是「先解除关联」。
    旧实现一律回「该记录仍被其它数据引用」，重复关联时人会被引到完全错误的方向。
    """
    text: str = str(exc).upper()
    if "UNIQUE" in text:
        return "DUPLICATE_CONFLICT", "该关联已存在，请勿重复添加"
    if "FOREIGN KEY" in text:
        return "FK_CONFLICT", "该记录仍被其它数据引用，请先解除关联后再操作"
    return "CONSTRAINT_CONFLICT", "数据约束冲突，请检查关联关系"


def _run(action: Callable[[], Any]) -> Any:
    """同步动作统一错误映射。"""
    try:
        return ok(action())
    except LookupError as exc:
        return fail(str(exc), 404)
    except sqlite3.IntegrityError as exc:
        # 不把 SQLite 原文抛给前端，但保留事件码与原文方便运维定位
        code, text = _conflict_text(exc)
        db.log_event("warn", "api", code, f"约束冲突: {exc}")
        return fail(text, 409)
    except (ValueError, TypeError, ValidationError) as exc:
        return fail(_err_text(exc), 400)
    except RuntimeError as exc:
        return fail(str(exc), 502)
    except Exception as exc:
        return _fail_internal("CONFIG_OP_FAIL", exc)


async def _arun(action: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """异步动作统一错误映射。"""
    try:
        return ok(await action())
    except LookupError as exc:
        return fail(str(exc), 404)
    except sqlite3.IntegrityError as exc:
        code, text = _conflict_text(exc)
        db.log_event("warn", "api", code, f"约束冲突: {exc}")
        return fail(text, 409)
    except (ValueError, TypeError, ValidationError) as exc:
        return fail(_err_text(exc), 400)
    except RuntimeError as exc:
        return fail(str(exc), 502)
    except Exception as exc:
        return _fail_internal("CONFIG_OP_FAIL", exc)


@router.get("/config/{resource}")
def list_config(resource: str) -> Any:
    """GET /api/config/{resource}：资源列表。"""
    return _run(lambda: config_service.list(resource))


@router.get("/config/{resource}/{item_id}")
def get_config(resource: str, item_id: int) -> Any:
    """GET /api/config/{resource}/{id}：单条详情。"""
    return _run(lambda: config_service.get(resource, item_id))


@router.post("/config/{resource}")
def create_config(resource: str, body: dict[str, Any]) -> Any:
    """POST /api/config/{resource}：新增（point 自动校验）。"""
    return _run(lambda: config_service.create(resource, body))


@router.put("/config/{resource}/{item_id}")
def update_config(
    resource: str, item_id: int, body: dict[str, Any]
) -> Any:
    """PUT /api/config/{resource}/{id}：更新（point 校验 + 热生效）。"""
    return _run(lambda: config_service.update(resource, item_id, body))


@router.delete("/config/{resource}/{item_id}")
def delete_config(resource: str, item_id: int) -> Any:
    """DELETE /api/config/{resource}/{id}：删除。"""
    return _run(lambda: config_service.delete(resource, item_id))


@router.post("/point/{item_id}/read")
async def read_point(item_id: int) -> Any:
    """POST /api/point/{id}/read：试读原始寄存器与解析值。"""
    return await _arun(lambda: collector.probe_point(item_id))


@router.post("/device/{item_id}/test")
async def test_device(item_id: int) -> Any:
    """POST /api/device/{id}/test：设备连接测试。"""
    return await _arun(lambda: config_service.test_device(item_id))


@router.post("/collector/reload")
def reload_collector() -> Any:
    """POST /api/collector/reload：采集配置热生效。"""
    def action() -> bool:
        collector.reload()
        return True

    return _run(action)


@router.get("/events")
def list_events(
    level: str | None = None,
    source: str | None = None,
    since: int | None = None,
    limit: int = 100,
) -> Any:
    """GET /api/events：事件日志（level/source/since/limit 过滤）。"""
    def action() -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if level:
            conditions.append("level=?")
            params.append(level)
        if source:
            conditions.append("source=?")
            params.append(source)
        if since is not None:
            conditions.append("ts>=?")
            params.append(int(since))
        sql: str = "SELECT * FROM event_log"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [dict(row) for row in db.query(sql, tuple(params))]

    return _run(action)
