"""v2 Modbus 三协议验证：MBAP(v1)、RTU-over-TCP CRC16（含篡改校验）、
pyserial 串口 mock + to_thread 不阻塞事件循环、三协议试读 ≤3s、仍禁 pymodbus。"""
from __future__ import annotations

import asyncio
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
import modbus
from collector import Collector, collector
from modbus import (
    ModbusError, ModbusRTUSerialClient, ModbusRTUTCPClient, ModbusTCPClient,
)

from helpers import create_device, create_point, run

SRC_ROOT = Path(main.__file__).resolve().parent
VALUES = [0x1234, 0x5678]


def rtu_frame(slave: int, func: int, values: list[int]) -> bytes:
    """独立构造合法 RTU 响应帧：addr+func+bytecount+regs+CRC(低字节在前)。"""
    body = struct.pack(">BB", slave, func) + bytes([len(values) * 2]) + b"".join(
        struct.pack(">H", v & 0xFFFF) for v in values)
    crc = ModbusRTUTCPClient.crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


class FakeRTUGateway:
    """本地 asyncio TCP 假网关，按 RTU(地址+PDU+CRC) 帧回答。"""

    def __init__(self, values: list[int] | None = None, delay: float = 0.0) -> None:
        self.values = values or VALUES
        self.delay = delay
        self.requests: list[bytes] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> "FakeRTUGateway":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readexactly(8)  # 1 地址 + 5 PDU + 2 CRC
            self.requests.append(request)
            if self.delay:
                await asyncio.sleep(self.delay)
            slave, func = request[0], request[1]
            writer.write(rtu_frame(slave, func, self.values))
            await writer.drain()
        finally:
            writer.close()


def test_pymodbus_is_forbidden_in_source_and_dependencies(isolated_db):
    """Arrange/Act: 扫描源码与依赖声明。Assert: 没有任何 pymodbus import/依赖。"""
    import re

    py_files = [p for p in SRC_ROOT.glob("*.py")]
    for path in py_files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:from\s+pymodbus|import\s+pymodbus)\b", text, re.M), (
            f"{path.name} 不得 import pymodbus")
    requirements = (SRC_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert not re.search(r"^\s*pymodbus\b", requirements, re.M | re.I)
    # 打包规格中显式排除 pymodbus
    spec = (SRC_ROOT / "energy-ems.spec").read_text(encoding="utf-8")
    assert "pymodbus" in spec and "excludes" in spec


def test_crc16_matches_standard_modbus_vector_and_low_byte_first(isolated_db):
    """Arrange: 标准 CRC-16/MODBUS 校验向量。Act: 计算与组帧。Assert: 值正确且 CRC 低字节在前。"""
    # Wikipedia: CRC-16/MODBUS("123456789") == 0x4B37
    assert ModbusRTUTCPClient.crc16(b"123456789") == 0x4B37
    client = ModbusRTUTCPClient("127.0.0.1", 502)
    adu = client.build_adu(0x11, 3, 0x6B, 3)
    crc = ModbusRTUTCPClient.crc16(adu[:-2])
    assert adu[:6] == bytes.fromhex("1103006b0003")
    assert adu[-2:] == bytes([crc & 0xFF, crc >> 8]), "CRC 必须低字节在前"
    assert adu[-2:].hex() == "7687"  # 经典 Modbus 协议示例帧


def test_rtu_parse_valid_frame_and_reject_tampered_crc(isolated_db):
    """Arrange: 合法/篡改 RTU 帧。Act: parse_response。Assert: 合法解寄存器、CRC 错必须抛错。"""
    client = ModbusRTUTCPClient("127.0.0.1", 502)
    frame = rtu_frame(1, 3, VALUES)
    assert client.parse_response(frame) == VALUES

    flipped = bytearray(frame)
    flipped[4] ^= 0xFF  # 篡改数据字节，CRC 不再匹配
    with pytest.raises(ModbusError, match="CRC"):
        client.parse_response(bytes(flipped))

    bad_crc = bytearray(frame)
    bad_crc[-1] ^= 0xFF  # 直接篡改 CRC 字节
    with pytest.raises(ModbusError, match="CRC"):
        client.parse_response(bytes(bad_crc))

    exception = bytes([1, 0x83, 0x02])
    exception += bytes([ModbusRTUTCPClient.crc16(exception) & 0xFF,
                        ModbusRTUTCPClient.crc16(exception) >> 8])
    with pytest.raises(ModbusError):
        client.parse_response(exception)


def test_rtu_tcp_read_registers_over_tcp_within_3_seconds(isolated_db):
    """Arrange: RTU-over-TCP 假网关。Act: 试读。Assert: 解析正确、发帧合法、耗时 ≤3s。"""
    async def scenario():
        async with FakeRTUGateway() as gateway:
            client = ModbusRTUTCPClient("127.0.0.1", gateway.port, timeout_ms=500, retry=0)
            started = time.monotonic()
            registers = await client.read_registers(1, 3, 0, 2)
            elapsed = time.monotonic() - started
            assert registers == VALUES
            assert elapsed <= 3.0, "三协议试读必须 3 秒内返回"
            sent = gateway.requests[0]
            assert sent[:6] == bytes.fromhex("010300000002")
            assert sent[-2:] == bytes([ModbusRTUTCPClient.crc16(sent[:-2]) & 0xFF,
                                      ModbusRTUTCPClient.crc16(sent[:-2]) >> 8])
            assert client._writer is None  # 读毕关连接

    run(scenario())


def test_mbap_tcp_v1_behavior_still_works(isolated_db):
    """Arrange: MBAP 假网关（复用 v1 FakeGateway 帧格式）。Act: v1 客户端读取。Assert: 兼容未回归。"""
    from helpers import FakeGateway

    async def scenario():
        async with FakeGateway(values=VALUES) as gateway:
            client = ModbusTCPClient("127.0.0.1", gateway.port, timeout_ms=300, retry=0)
            assert await client.read_registers(1, 3, 0, 2) == VALUES

    run(scenario())


def test_serial_client_reads_via_mock_pyserial_without_blocking_loop(isolated_db, monkeypatch):
    """Arrange: mock pyserial（read 内睡眠并记录线程）。Act: to_thread 读取。Assert: 解析正确、事件循环未被阻塞。"""
    response = rtu_frame(1, 3, VALUES)
    read_state = {"call": 0, "thread_ids": []}

    class FakeSerial:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.written = b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def reset_input_buffer(self):
            pass

        def write(self, data):
            self.written = bytes(data)

        def read(self, n):
            read_state["thread_ids"].append(threading.get_ident())
            time.sleep(0.03)  # 模拟慢速串口 I/O
            idx = read_state["call"]
            read_state["call"] += 1
            if idx == 0:
                return response[:3]
            return response[3:3 + n]

    fake_serial = SimpleNamespace(
        EIGHTBITS=8, STOPBITS_ONE=1, PARITY_NONE="N", PARITY_E="E", PARITY_O="O",
        Serial=FakeSerial,
    )
    monkeypatch.setattr(modbus, "serial", fake_serial)

    async def scenario():
        ticks = {"n": 0}

        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        client = ModbusRTUSerialClient("COM3", 9600, timeout_ms=300, retry=0, parity="N")
        try:
            registers = await client.read_registers(1, 3, 0, 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert registers == VALUES
        assert ticks["n"] >= 2, "串口阻塞 I/O 不得阻塞事件循环"
        assert client.parity() == "N"
        assert fake_serial is not None

    run(scenario())
    main_thread = threading.get_ident()
    assert read_state["thread_ids"], "应发生串口 read"
    assert all(tid != main_thread for tid in read_state["thread_ids"]), (
        "串口 I/O 必须在 to_thread 工作线程中执行")


def test_collector_dispatches_all_three_protocols(isolated_db):
    """Arrange: 三类 device 字典。Act: _client_for。Assert: 正确分派客户端类型。"""
    service = Collector()
    assert isinstance(service._client_for({
        "protocol": "modbus_tcp", "host": "127.0.0.1", "port": 502}), ModbusTCPClient)
    assert isinstance(service._client_for({
        "protocol": "modbus_rtu_tcp", "host": "127.0.0.1", "port": 502}), ModbusRTUTCPClient)
    serial_client = service._client_for({
        "protocol": "modbus_rtu_serial", "host": "COM3", "port": 115200})
    assert isinstance(serial_client, ModbusRTUSerialClient)
    assert serial_client.port_name == "COM3"
    assert serial_client.baudrate == 115200
    with pytest.raises(ModbusError):
        service._client_for({"protocol": "manual", "host": "x", "port": 1})
