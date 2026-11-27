"""自写轻量 Modbus 客户端（禁用 pymodbus）：MBAP TCP / RTU-over-TCP / RTU 串口。

三段式：build_adu 组帧 -> read_registers 短连接读写+超时重试 -> parse_response 校验并解寄存器；
decode 按 data_type/byte_order/scale/offset/min-max 把原始寄存器转成工程值与 quality。

v2：新增 BaseModbusClient 抽象与 ModbusRTUTCPClient（CRC16）、ModbusRTUSerialClient（pyserial）；
    保留全部 v1 兼容符号：ModbusClient 别名、模块级 read_registers()/decode()、ModbusError。
"""
from __future__ import annotations

import asyncio
import math
import os
import struct
import time
import weakref
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


# ---------------------------------------------------------------- 串口总线互斥
# 一个 COM 口（一根 RS485 线）上通常挂多台从站，各点位在 collector 里是独立任务。
# 两帧交错写下去，从站会回错帧或干脆不回：CRC 校验可能通过、站号校验也可能通过
# （同一总线上确有该站），最终表现为「某台表读数偶尔乱跳」这种最难查的现场故障。
# 因此按端口做进程内互斥，并保证帧间静默 ≥ 3.5 字符时间（Modbus RTU 规范要求）。
#
# 锁按「事件循环 + 端口」两级缓存：asyncio.Lock 首次 await 时会绑定当前循环，
# 之后在另一个循环里再用会抛 RuntimeError（「is bound to a different event loop」）。
# 生产环境只有一个循环，但脚本、测试、以及任何 asyncio.run() 包裹的调用都会换循环，
# 届时一把跨循环缓存的锁就是启动即崩的隐藏地雷。用 WeakKeyDictionary 按循环分组，
# 循环被回收后对应的锁自然释放。
_serial_port_locks: "weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]]" = weakref.WeakKeyDictionary()
_serial_last_io: dict[str, float] = {}


def _serial_port_key(port_name: str) -> str:
    """端口键：去空白 + normcase（Windows 上 COM3/com3 视为同一口）。"""
    return os.path.normcase(str(port_name).strip())


def _serial_port_lock(port_name: str) -> asyncio.Lock:
    """取该端口在当前事件循环内的互斥锁（同循环同端口跨客户端实例共享同一把）。"""
    key: str = _serial_port_key(port_name)
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    per_loop: dict[str, asyncio.Lock] | None = _serial_port_locks.get(loop)
    if per_loop is None:
        per_loop = {}
        _serial_port_locks[loop] = per_loop
    lock: asyncio.Lock | None = per_loop.get(key)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[key] = lock
    return lock


def serial_frame_gap_sec(baudrate: int, parity: str = "N") -> float:
    """RTU 帧间静默时间（秒）：3.5 个字符时间。

    一个字符 = 1 起始位 + 8 数据位 +（有校验时 1 位）+ 1 停止位，即 10 或 11 位。
    9600-8N1 约 3.6ms，115200-8N1 约 0.3ms。
    """
    bits: int = 11 if str(parity or "N").upper() in ("E", "O") else 10
    return 3.5 * bits / float(max(1, int(baudrate)))


class ModbusError(Exception):
    """Modbus 读写失败（含异常响应、校验失败、连接失败）。"""


class ModbusTimeoutError(ModbusError):
    """读取超时（用于区分 quality=1）。"""


class ModbusExceptionResponse(ModbusError):
    """设备返回 Modbus 异常码（func|0x80 + code）。

    异常码是设备对「地址非法/功能不支持」的确定性答复，重试只会再收一次同样的
    异常帧，徒增一轮超时等待，因此单独成类并在读循环中直接抛出、不重试。
    """


def _check_unit(received: int, expected: int | None, frame_name: str) -> None:
    """校验响应单元号（站号）与请求一致，防止多站总线/网关串包被当成自己的数据。"""
    if expected is None:
        return
    if int(received) != int(expected):
        raise ModbusError(
            f"{frame_name} 单元号不匹配：请求 {int(expected)}，响应 {int(received)}"
        )


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


def _parse_rtu_response(data: bytes, expect_slave: int | None = None) -> list[int]:
    """校验 RTU 响应 CRC 与站号并解 >H 寄存器列表；异常响应抛 ModbusError。"""
    if len(data) < 5:
        raise ModbusError("RTU 响应长度过短")
    frame: bytes = data[:-2]
    received: int = int(data[-2]) | (int(data[-1]) << 8)
    if _crc16(frame) != received:
        raise ModbusError("RTU CRC16 校验失败")
    _check_unit(frame[0], expect_slave, "RTU 响应")
    func: int = frame[1]
    if func & 0x80:
        code: int = frame[2] if len(frame) > 2 else 0
        raise ModbusExceptionResponse(f"Modbus 异常响应 func=0x{func:02X} code={code}")
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
        # 最近一次 build_adu 写入的事务号/单元号，供 parse_response 校验回包归属
        self._pending_tx: int | None = None
        self._pending_unit: int | None = None

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
        self._pending_tx, self._pending_unit = transaction_id, int(slave) & 0xFF
        return mbap + pdu

    def parse_response(
        self, data: bytes, expect_tx: int | None = None, expect_unit: int | None = None
    ) -> list[int]:
        """校验 MBAP/PDU 并解析 >H 寄存器列表；异常响应抛 ModbusError。"""
        if len(data) < 9:
            # 最短合法响应：MBAP 7B + 异常 PDU 2B（func|0x80 + code）
            raise ModbusError("Modbus 响应长度过短")
        tx, protocol, length, unit = struct.unpack(">HHHB", data[:7])
        if protocol != 0:
            raise ModbusError("Modbus 协议标识非 0")
        if expect_tx is not None and int(tx) != int(expect_tx):
            raise ModbusError(f"MBAP 事务号不匹配：请求 {int(expect_tx)}，响应 {int(tx)}")
        _check_unit(unit, expect_unit, "MBAP 响应")
        if length < 3 or len(data) < 6 + length:
            raise ModbusError("Modbus 响应字节数不合法")
        pdu: bytes = data[7:6 + length]
        func: int = pdu[0]
        if func & 0x80:
            code: int = pdu[1] if len(pdu) > 1 else 0
            raise ModbusExceptionResponse(f"Modbus 异常响应 func=0x{func:02X} code={code}")
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
                expected_tx, expected_unit = self._pending_tx, self._pending_unit
                await self._writer.drain()
                header: bytes = await asyncio.wait_for(self._reader.readexactly(7), timeout=timeout)
                # MBAP.length 计入了单元号，而单元号已在这 7 字节头里，故剩余 PDU 为 length-1
                length: int = struct.unpack(">H", header[4:6])[0]
                body: bytes = await asyncio.wait_for(
                    self._reader.readexactly(length - 1), timeout=timeout
                )
                registers: list[int] = self.parse_response(
                    header + body, expected_tx, expected_unit
                )
                if len(registers) < count:
                    raise ModbusError(f"返回寄存器数量不足：需要 {count}，实际 {len(registers)}")
                return registers[:count]
            except asyncio.TimeoutError:
                last_error = ModbusTimeoutError(f"读取 {self.host}:{self.port} 超时")
            except asyncio.IncompleteReadError as exc:
                last_error = ModbusError(f"响应不完整: {exc}")
            except (OSError, ValueError, struct.error) as exc:
                last_error = ModbusError(str(exc))
            except ModbusExceptionResponse:
                # 异常码是设备的确定性答复，重试只会再收一次同样的帧
                raise
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
        self._pending_unit = int(slave) & 0xFF
        return _rtu_adu(slave, pdu)

    def parse_response(self, data: bytes, expect_slave: int | None = None) -> list[int]:
        """校验 CRC16 与站号后解析寄存器列表；异常响应抛 ModbusExceptionResponse。"""
        return _parse_rtu_response(data, expect_slave)

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
                expected_unit: int | None = self._pending_unit
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
                registers: list[int] = self.parse_response(head + tail, expected_unit)
                if len(registers) < count:
                    raise ModbusError(f"返回寄存器数量不足：需要 {count}，实际 {len(registers)}")
                return registers[:count]
            except asyncio.TimeoutError:
                last_error = ModbusTimeoutError(f"读取 {self.host}:{self.port} 超时")
            except asyncio.IncompleteReadError as exc:
                last_error = ModbusError(f"响应不完整: {exc}")
            except (OSError, ValueError, struct.error) as exc:
                last_error = ModbusError(str(exc))
            except ModbusExceptionResponse:
                raise
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
        self._pending_unit = int(slave) & 0xFF
        return _rtu_adu(slave, pdu)

    def parse_response(self, data: bytes, expect_slave: int | None = None) -> list[int]:
        """校验 CRC16 与站号后解析寄存器列表。"""
        return _parse_rtu_response(data, expect_slave)

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
        """线程池内完成串口 I/O，失败按 retry 重试。

        整段（含重试）持有该 COM 口的互斥锁：同一条 485 总线上的多台从站必须排队
        发帧，否则两帧会交错写进同一根线。锁内先补足帧间静默（3.5 字符时间）再发。
        """
        if func not in (3, 4):
            raise ValueError("func 仅支持 3 或 4")
        if not 1 <= count <= MODBUS_MAX_REGISTERS:
            raise ValueError(f"count 必须在 1~{MODBUS_MAX_REGISTERS} 之间")
        adu: bytes = self.build_adu(slave, func, offset, count)
        expected_unit: int | None = self._pending_unit
        last_error: ModbusError = ModbusError("未知读取错误")
        key: str = _serial_port_key(self.port_name)
        gap: float = serial_frame_gap_sec(self.baudrate, self.parity())
        async with _serial_port_lock(self.port_name):
            for attempt in range(self.retry + 1):
                # 帧间静默：距上一帧（本口任意从站）不足 3.5 字符时间就等够再发
                wait: float = gap - (time.monotonic() - _serial_last_io.get(key, 0.0))
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    frame: bytes = await asyncio.wait_for(
                        asyncio.to_thread(self._read_sync, adu),
                        timeout=max(0.1, self.timeout_ms / 1000.0 + 0.5),
                    )
                    registers: list[int] = self.parse_response(frame, expected_unit)
                    if len(registers) < count:
                        raise ModbusError(f"返回寄存器数量不足：需要 {count}，实际 {len(registers)}")
                    return registers[:count]
                except asyncio.TimeoutError:
                    last_error = ModbusTimeoutError(f"串口 {self.port_name} 读取超时")
                except (OSError, ValueError, struct.error) as exc:
                    last_error = ModbusError(str(exc))
                except ModbusExceptionResponse:
                    raise
                except ModbusError as exc:
                    last_error = exc
                finally:
                    _serial_last_io[key] = time.monotonic()
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
    "ModbusExceptionResponse",
    "ModbusRTUSerialClient",
    "ModbusRTUTCPClient",
    "ModbusTCPClient",
    "ModbusTimeoutError",
    "decode",
    "read_registers",
]
