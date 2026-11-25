"""QA 独立验证用的数据工厂与本地 TCP 假网关。"""
from __future__ import annotations

import asyncio
import struct
from typing import Any, Callable

import db


def run(coro: Any) -> Any:
    """在同步 pytest 测试里运行协程，避免依赖 pytest-asyncio。"""
    return asyncio.run(coro)


def create_node(name: str = "A栋", **extra: Any) -> int:
    fields: dict[str, Any] = {"name": name, **extra}
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    return int(db.execute(f"INSERT INTO node ({columns}) VALUES ({placeholders})", tuple(fields.values())))


def create_device(
    host: str = "127.0.0.1",
    port: int = 502,
    name: str = "E90网关",
    timeout_ms: int = 200,
    retry: int = 0,
    poll_interval: int = 60,
    is_enabled: int = 1,
) -> int:
    return int(db.execute(
        "INSERT INTO device (name, protocol, host, port, timeout_ms, retry, poll_interval, is_enabled) "
        "VALUES (?, 'modbus_tcp', ?, ?, ?, ?, ?, ?)",
        (name, host, port, timeout_ms, retry, poll_interval, is_enabled),
    ))


def create_point(
    device_id: int,
    *,
    name: str = "总电表电量",
    slave_id: int = 1,
    func_code: int = 3,
    reg_offset: int = 0,
    reg_count: int = 2,
    data_type: str = "u16",
    byte_order: str = "ABCD",
    scale: float = 1.0,
    offset_const: float = 0.0,
    min_val: float | None = None,
    max_val: float | None = None,
    unit: str = "kWh",
    point_role: str = "energy",
    is_enabled: int = 1,
) -> int:
    return int(db.execute(
        "INSERT INTO point (name, device_id, slave_id, func_code, reg_offset, reg_count, "
        "data_type, byte_order, scale, offset_const, min_val, max_val, unit, point_role, is_enabled) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, device_id, slave_id, func_code, reg_offset, reg_count, data_type, byte_order,
         scale, offset_const, min_val, max_val, unit, point_role, is_enabled),
    ))


def create_meter(
    node_id: int,
    *,
    name: str = "1号表",
    category_id: int = 1,
    purpose: str = "total",
) -> int:
    return int(db.execute(
        "INSERT INTO meter (name, node_id, category_id, purpose, is_counted, daily_8am, is_enabled) "
        "VALUES (?,?,?,?,1,1,1)",
        (name, node_id, category_id, purpose),
    ))


def link_point(meter_id: int, point_id: int, role: str = "reading") -> None:
    db.execute(
        "INSERT OR IGNORE INTO meter_point (meter_id, point_id, role) VALUES (?,?,?)",
        (meter_id, point_id, role),
    )


def add_reading(point_id: int, ts: int, value: float, quality: int = 0) -> None:
    db.execute(
        "INSERT OR REPLACE INTO reading (point_id, ts, value, quality) VALUES (?,?,?,?)",
        (point_id, ts, value, quality),
    )


def create_tariff(price_cent: int = 80, is_default: int = 1, name: str = "平价") -> int:
    return int(db.execute(
        "INSERT INTO tariff (name, price_cent, is_default) VALUES (?,?,?)",
        (name, price_cent, is_default),
    ))


class FakeGateway:
    """本地 asyncio TCP 假网关，按 MBAP 帧回答寄存器，并记录收到的请求。"""

    def __init__(
        self,
        values: list[int] | None = None,
        behavior: str = "normal",
        delay: float = 0.0,
    ) -> None:
        self.values = values
        self.behavior = behavior
        self.delay = delay
        self.requests: list[bytes] = []
        self.connects = 0
        self.active = 0
        self.server: asyncio.AbstractServer | None = None
        self.port = 0

    async def __aenter__(self) -> "FakeGateway":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connects += 1
        self.active += 1
        try:
            request = await reader.readexactly(12)  # MBAP(7) + 读寄存器 PDU(5)
            self.requests.append(request)
            if self.behavior == "delay":
                await asyncio.sleep(self.delay)
                return
            if self.behavior == "drop":
                # 保持连接但不应答，让客户端触发读取超时（而非 EOF）
                await asyncio.sleep(self.delay or 0.3)
                return
            tx, _protocol, _length, unit, func, offset, count = struct.unpack(
                ">HHHBBHH", request
            )
            if self.behavior == "exception" or (
                self.behavior == "fail_first" and self.connects == 1
            ):
                body = struct.pack(">BB", func | 0x80, 2)
            else:
                values = self.values if self.values is not None else [0x1234] * count
                body = struct.pack(">BB", func, len(values) * 2) + b"".join(
                    struct.pack(">H", int(value) & 0xFFFF) for value in values
                )
            writer.write(struct.pack(">HHHB", tx, 0, len(body) + 1, unit) + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.active -= 1
            writer.close()

    @property
    def last_request(self) -> bytes:
        return self.requests[-1]


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """轮询等待后台任务产生预期结果。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False
