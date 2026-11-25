"""自写轻量 Modbus 客户端（禁用 pymodbus）：MBAP TCP / RTU-over-TCP / RTU 串口。

三段式：build_adu 组帧 -> read_registers 短连接读写+超时重试 -> parse_response 校验并解寄存器；
decode 按 data_type/byte_order/scale/offset/min-max 把原始寄存器转成工程值与 quality。

v2：新增 BaseModbusClient 抽象与 ModbusRTUTCPClient（CRC16）、ModbusRTUSerialClient（pyserial）；
    保留全部 v1 兼容符号：ModbusClient 别名、模块级 read_registers()/decode()、ModbusError。
"""
from __future__ import annotations

import asyncio
import math
import struct
from abc import ABC, abstractmethod
from typing import Any

from settings import (
    BYTE_ORDER_FACTORS,
    BYTE_ORDERS,
    DEFAULT_BAUDRATE,
    DEFAULT_RETRY,
    DEFAULT_TIMEOUT_MS,
    MODBUS_MAX_BYTE_COUNT,
    MODBUS_MAX_REGISTERS,
    QUALITY_OK,
    QUALITY_OUT_OF_RANGE,
    STRUCT_FORMAT,
    TYPE_COUNT,
)

try:  # pyserial 为可选依赖：仅 modbus_rtu_serial 协议需要
    import serial  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 未安装时惰性报错，不影响 TCP 协议
    serial = None  # type: ignore[assignment]


class ModbusError(Exception):
    """Modbus 读写失败（含异常响应、校验失败、连接失败）。"""


class ModbusTimeoutError(ModbusError):
    """读取超时（用于区分 quality=1）。"""


def decode(registers: list[int], point: dict[str, Any]) -> tuple[float, int]:
    """原始寄存器 -> (工程值, quality)。

    1) 每个寄存器按大端拆 2 字节；2) 按 byte_order 因子表重排；
    3) struct 按 data_type 解包；4) value = raw*scale + offset_const；
    5) 超出 min/max 或非有限数标记 quality=2。
    """
    data_type: str = str(point.get("data_type", "u16")).lower()
    byte_order: str = str(point.get("byte_order", "ABCD")).upper()
    if data_type not in TYPE_COUNT:
        raise ValueError(f"不支持的 data_type: {data_type}")
    if byte_order not in BYTE_ORDERS:
        raise ValueError(f"不支持的 byte_order: {byte_order}")

    need: int = TYPE_COUNT[data_type]
    if len(registers) < need:
        raise ValueError(f"寄存器数量不足：需要 {need}，实际 {len(registers)}")

    raw: bytes = b"".join(struct.pack(">H", int(reg) & 0xFFFF) for reg in registers[:need])
    width: int = len(raw)
    factor: tuple[int, ...] = BYTE_ORDER_FACTORS[byte_order][width]
    ordered: bytes = bytes(raw[index] for index in factor)
    raw_value: float = float(struct.unpack(STRUCT_FORMAT[data_type], ordered)[0])

    def number(key: str, default: float) -> float:
        value: Any = point.get(key, default)
        return default if value is None else float(value)

    value: float = raw_value * number("scale", 1.0) + number("offset_const", 0.0)
    low: Any = point.get("min_val")
    high: Any = point.get("max_val")
    out_of_range: bool = not math.isfinite(value)
    if low is not None and value < float(low):
        out_of_range = True
    if high is not None and value > float(high):
        out_of_range = True
    if out_of_range:
        # 非有限数无法写入 JSON/DB，统一归零并用 quality 标记
        return (0.0 if not math.isfinite(value) else value), QUALITY_OUT_OF_RANGE
    return value, QUALITY_OK


def _crc16(data: bytes) -> int:
    """CRC-16/Modbus：多项式 0xA001，初值 0xFFFF，低字节在前。"""
    crc: int = 0xFFFF
    for byte in data:
        crc ^= int(byte) & 0xFF
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
            crc &= 0xFFFF
    return crc & 0xFFFF


def _rtu_adu(slave: int, pdu: bytes) -> bytes:
    """RTU ADU = 地址(1) + PDU + CRC16(低字节在前)。"""
    crc: int = _crc16(bytes([int(slave) & 0xFF]) + pdu)
    return bytes([int(slave) & 0xFF]) + pdu + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def _parse_rtu_response(data: bytes) -> list[int]:
    """校验 RTU 响应 CRC 并解 >H 寄存器列表；异常响应抛 ModbusError。"""
    if len(data) < 5:
        raise ModbusError("RTU 响应长度过短")
    frame: bytes = data[:-2]
    received: int = int(data[-2]) | (int(data[-1]) << 8)
    if _crc16(frame) != received:
        raise ModbusError("RTU CRC16 校验失败")
    func: int = frame[1]
    if func & 0x80:
        code: int = frame[2] if len(frame) > 2 else 0
        raise ModbusError(f"Modbus 异常响应 func=0x{func:02X} code={code}")
    if func not in (3, 4):
        raise ModbusError(f"非预期功能码: {func}")
    byte_count: int = frame[2]
    if byte_count < 1 or byte_count > MODBUS_MAX_BYTE_COUNT or byte_count % 2 != 0:
        raise ModbusError(f"非法寄存器字节数: {byte_count}")
    payload: bytes = frame[3:3 + byte_count]
    if len(payload) != byte_count:
        raise ModbusError("响应数据长度与字节数不符")
    registers: tuple[int, ...] = struct.unpack(f">{byte_count // 2}H", payload)
    return [int(value) for value in registers]


class BaseModbusClient(ABC):
    """三协议共用基类：连接参数 + decode + TCP 连接管理 + 抽象帧方法。"""

    def __init__(
        self,
        host: str,
        port: int,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        retry: int = DEFAULT_RETRY,
    ) -> None:
        self.host: str = str(host)
        self.port: int = int(port)
        self.timeout_ms: int = int(timeout_ms)
        self.retry: int = int(retry)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tx_id: int = 0

    async def connect(self) -> None:
        """建立 TCP 连接；已有可用连接时直接复用（串口子类覆盖为空实现）。"""
        if self._writer is not None and not self._writer.is_closing():
            return
        timeout: float = self.timeout_ms / 1000.0
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise ModbusTimeoutError(f"连接 {self.host}:{self.port} 超时") from exc
        except OSError as exc:
            raise ModbusError(f"连接 {self.host}:{self.port} 失败: {exc}") from exc
        self._reader, self._writer = reader, writer

    async def close(self) -> None:
        """关闭连接并等待底层资源释放（串口子类覆盖为空实现）。"""
        writer: asyncio.StreamWriter | None = self._writer
        self._writer, self._reader = None, None
        if writer is None:
            return
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except (asyncio.TimeoutError, OSError):
            pass  # 关闭超时不影响业务，连接已标记释放

    def decode(self, registers: list[int], point: dict[str, Any]) -> tuple[float, int]:
        """按点位配置解析寄存器（委托模块级 decode，便于试读复用）。"""
        return decode(registers, point)

    @abstractmethod
    def build_adu(self, slave: int, func: int, offset: int, count: int) -> bytes:
        """组帧：由子类实现 MBAP 或 RTU ADU。"""

    @abstractmethod
    def parse_response(self, data: bytes) -> list[int]:
        """解析响应：由子类实现 MBAP/CRC 校验。"""

    @abstractmethod
    async def read_registers(self, slave: int, func: int, offset: int, count: int) -> list[int]:
        """读取寄存器：由子类实现帧边界与重试。"""


class ModbusTCPClient(BaseModbusClient):
    """最小 Modbus TCP 客户端：每次读毕即关，短连接避免占用网关连接数。"""

    def build_adu(self, slave: int, func: int, offset: int, count: int) -> bytes:
        """组 MBAP(7B) + PDU：事务 ID 自增，协议 ID=0，长度=单元号+PDU。"""
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        transaction_id: int = self._tx_id & 0xFFFF
        self._tx_id = (self._tx_id + 1) & 0xFFFF
        pdu: bytes = struct.pack(">BHH", func, int(offset) & 0xFFFF, int(count) & 0xFFFF)
        mbap: bytes = struct.pack(">HHHB", transaction_id, 0, len(pdu) + 1, int(slave) & 0xFF)
        return mbap + pdu

    def parse_response(self, data: bytes) -> list[int]:
        """校验 MBAP/PDU 并解析 >H 寄存器列表；异常响应抛 ModbusError。"""
        if len(data) < 11:
            raise ModbusError("Modbus 响应长度过短")
        _tx, protocol, length, _unit = struct.unpack(">HHHB", data[:7])
        if protocol != 0:
            raise ModbusError("Modbus 协议标识非 0")
        if length < 3 or len(data) < 6 + length:
            raise ModbusError("Modbus 响应字节数不合法")
        pdu: bytes = data[7:6 + length]
        func: int = pdu[0]
        if func & 0x80:
            code: int = pdu[1] if len(pdu) > 1 else 0
            raise ModbusError(f"Modbus 异常响应 func=0x{func:02X} code={code}")
        if func not in (3, 4):
            raise ModbusError(f"非预期功能码: {func}")
        byte_count: int = pdu[1]
        if byte_count < 1 or byte_count > MODBUS_MAX_BYTE_COUNT or byte_count % 2 != 0:
            raise ModbusError(f"非法寄存器字节数: {byte_count}")
        payload: bytes = pdu[2:2 + byte_count]
        if len(payload) != byte_count:
            raise ModbusError("响应数据长度与字节数不符")
        registers: tuple[int, ...] = struct.unpack(f">{byte_count // 2}H", payload)
        return [int(value) for value in registers]

    async def read_registers(self, slave: int, func: int, offset: int, count: int) -> list[int]:
        """短连接读取寄存器，失败按 retry 次数指数退避重试；读毕必定关闭连接。"""
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        timeout: float = self.timeout_ms / 1000.0
        last_error: ModbusError = ModbusError("未知读取错误")

        for attempt in range(self.retry + 1):
            try:
                await self.connect()
                assert self._reader is not None and self._writer is not None
                self._writer.write(self.build_adu(slave, func, offset, count))
                await self._writer.drain()
                header: bytes = await asyncio.wait_for(self._reader.readexactly(7), timeout=timeout)
                # MBAP.length 计入了单元号，而单元号已在这 7 字节头里，故剩余 PDU 为 length-1
                length: int = struct.unpack(">H", header[4:6])[0]
                body: bytes = await asyncio.wait_for(
                    self._reader.readexactly(length - 1), timeout=timeout
                )
                registers: list[int] = self.parse_response(header + body)
                if len(registers) < count:
                    raise ModbusError(f"返回寄存器数量不足：需要 {count}，实际 {len(registers)}")
                return registers[:count]
            except asyncio.TimeoutError:
                last_error = ModbusTimeoutError(f"读取 {self.host}:{self.port} 超时")
            except asyncio.IncompleteReadError as exc:
                last_error = ModbusError(f"响应不完整: {exc}")
            except (OSError, ValueError, struct.error) as exc:
                last_error = ModbusError(str(exc))
            except ModbusError as exc:
                last_error = exc
            finally:
                await self.close()  # 每次读毕即关，包括失败路径
            if attempt < self.retry:
                await asyncio.sleep(min(0.05 * (2 ** attempt), 1.0))
        raise last_error


class ModbusRTUTCPClient(BaseModbusClient):
    """RTU 帧（地址+PDU+CRC16）走 TCP；响应按 head(3)+payload+crc 定长解析。"""

    @staticmethod
    def crc16(data: bytes) -> int:
        """CRC-16/Modbus 多项式 0xA001（低字节在前由 build_adu 处理）。"""
        return _crc16(data)

    def build_adu(self, slave: int, func: int, offset: int, count: int) -> bytes:
        """组 RTU ADU：addr + func/offset/count + CRC(低字节在前)。"""
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        pdu: bytes = struct.pack(">BHH", func, int(offset) & 0xFFFF, int(count) & 0xFFFF)
        return _rtu_adu(slave, pdu)

    def parse_response(self, data: bytes) -> list[int]:
        """校验 CRC16 后解析寄存器列表；异常响应抛 ModbusError。"""
        return _parse_rtu_response(data)

    async def read_registers(self, slave: int, func: int, offset: int, count: int) -> list[int]:
        """短连接读取 RTU-over-TCP，按帧头字节数定长读取，失败重试。"""
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        timeout: float = self.timeout_ms / 1000.0
        last_error: ModbusError = ModbusError("未知读取错误")

        for attempt in range(self.retry + 1):
            try:
                await self.connect()
                assert self._reader is not None and self._writer is not None
                self._writer.write(self.build_adu(slave, func, offset, count))
                await self._writer.drain()
                head: bytes = await asyncio.wait_for(self._reader.readexactly(3), timeout=timeout)
                if head[1] & 0x80:
                    tail: bytes = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout)
                else:
                    byte_count: int = head[2]
                    if byte_count < 1 or byte_count > MODBUS_MAX_BYTE_COUNT or byte_count % 2:
                        raise ModbusError(f"非法寄存器字节数: {byte_count}")
                    tail = await asyncio.wait_for(
                        self._reader.readexactly(byte_count + 2), timeout=timeout
                    )
                registers: list[int] = self.parse_response(head + tail)
                if len(registers) < count:
                    raise ModbusError(f"返回寄存器数量不足：需要 {count}，实际 {len(registers)}")
                return registers[:count]
            except asyncio.TimeoutError:
                last_error = ModbusTimeoutError(f"读取 {self.host}:{self.port} 超时")
            except asyncio.IncompleteReadError as exc:
                last_error = ModbusError(f"响应不完整: {exc}")
            except (OSError, ValueError, struct.error) as exc:
                last_error = ModbusError(str(exc))
            except ModbusError as exc:
                last_error = exc
            finally:
                await self.close()
            if attempt < self.retry:
                await asyncio.sleep(min(0.05 * (2 ** attempt), 1.0))
        raise last_error


class ModbusRTUSerialClient(BaseModbusClient):
    """pyserial COM 口客户端；同步串口 I/O 用 asyncio.to_thread 包装，不阻塞事件循环。

    字段约定（沿用 device 表）：host = COM 口名，port = 波特率（如 9600/115200）。
    """

    def __init__(
        self,
        host: str,
        port: int,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        retry: int = DEFAULT_RETRY,
        baudrate: int | None = None,
        parity: str = "N",
    ) -> None:
        super().__init__(host, port, timeout_ms, retry)
        self.port_name: str = str(host)  # 类图中 str port：COM 口
        self.baudrate: int = int(baudrate if baudrate is not None else (port or DEFAULT_BAUDRATE))
        self._parity: str = str(parity or "N").upper()[:1]

    def parity(self) -> str:
        """串口校验位（N/E/O），默认 N。"""
        return self._parity if self._parity in ("N", "E", "O") else "N"

    @staticmethod
    def crc16(data: bytes) -> int:
        """与 RTU-over-TCP 共用 CRC-16/Modbus。"""
        return _crc16(data)

    def build_adu(self, slave: int, func: int, offset: int, count: int) -> bytes:
        """组 RTU ADU（同 ModbusRTUTCPClient）。"""
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        pdu: bytes = struct.pack(">BHH", func, int(offset) & 0xFFFF, int(count) & 0xFFFF)
        return _rtu_adu(slave, pdu)

    def parse_response(self, data: bytes) -> list[int]:
        """校验 CRC16 后解析寄存器列表。"""
        return _parse_rtu_response(data)

    async def connect(self) -> None:
        """串口无需持久连接，空实现以兼容统一接口。"""
        return None

    async def close(self) -> None:
        """串口帧读毕即关，空实现以兼容统一接口。"""
        return None

    def _read_sync(self, adu: bytes) -> bytes:
        """同步打开 COM 口、写一帧并读取响应帧（运行在线程池）。"""
        if serial is None:
            raise ModbusError("未安装 pyserial，无法使用 modbus_rtu_serial 协议")
        try:
            with serial.Serial(
                port=self.port_name,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=getattr(serial, f"PARITY_{self.parity()}", serial.PARITY_NONE),
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout_ms / 1000.0,
            ) as handle:
                handle.reset_input_buffer()
                handle.write(adu)
                head: bytes = handle.read(3)
                if len(head) < 3:
                    raise ModbusError("串口响应头不完整")
                if head[1] & 0x80:
                    tail: bytes = handle.read(2)
                else:
                    byte_count: int = head[2]
                    if byte_count < 1 or byte_count > MODBUS_MAX_BYTE_COUNT or byte_count % 2:
                        raise ModbusError(f"非法寄存器字节数: {byte_count}")
                    tail = handle.read(byte_count + 2)
                return head + tail
        except ModbusError:
            raise
        except Exception as exc:
            raise ModbusError(f"串口读取失败: {exc}") from exc

    async def read_registers(self, slave: int, func: int, offset: int, count: int) -> list[int]:
        """线程池内完成串口 I/O，失败按 retry 重试。"""
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        adu: bytes = self.build_adu(slave, func, offset, count)
        last_error: ModbusError = ModbusError("未知读取错误")
        for attempt in range(self.retry + 1):
            try:
                frame: bytes = await asyncio.wait_for(
                    asyncio.to_thread(self._read_sync, adu),
                    timeout=max(0.1, self.timeout_ms / 1000.0 + 0.5),
                )
                registers: list[int] = self.parse_response(frame)
                if len(registers) < count:
                    raise ModbusError(f"返回寄存器数量不足：需要 {count}，实际 {len(registers)}")
                return registers[:count]
            except asyncio.TimeoutError:
                last_error = ModbusTimeoutError(f"串口 {self.port_name} 读取超时")
            except (OSError, ValueError, struct.error) as exc:
                last_error = ModbusError(str(exc))
            except ModbusError as exc:
                last_error = exc
            if attempt < self.retry:
                await asyncio.sleep(min(0.05 * (2 ** attempt), 1.0))
        raise last_error


# v1 兼容符号：旧代码/测试仍以 ModbusClient 构造 MBAP TCP 客户端
ModbusClient = ModbusTCPClient


async def read_registers(
    host: str,
    port: int,
    slave: int,
    func: int,
    offset: int,
    count: int,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    retry: int = DEFAULT_RETRY,
) -> list[int]:
    """v1 兼容模块级便捷函数：短连接读取 MBAP TCP 寄存器。"""
    client: ModbusTCPClient = ModbusTCPClient(host, port, timeout_ms, retry)
    try:
        return await client.read_registers(slave, func, offset, count)
    finally:
        await client.close()


__all__ = [
    "BaseModbusClient",
    "ModbusClient",
    "ModbusError",
    "ModbusRTUSerialClient",
    "ModbusRTUTCPClient",
    "ModbusTCPClient",
    "ModbusTimeoutError",
    "decode",
    "read_registers",
]
