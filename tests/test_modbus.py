"""modbus.py 验证：MBAP 帧、字序解码、异常帧与 asyncio TCP 往返。"""
from __future__ import annotations

import asyncio
import struct

import pytest

from helpers import FakeGateway, run
from modbus import ModbusClient, ModbusError, ModbusTimeoutError, decode


def _response_bytes(values: list[int], func: int = 3, tx: int = 1) -> bytes:
    pdu = struct.pack(">BB", func, len(values) * 2) + b"".join(
        struct.pack(">H", value & 0xFFFF) for value in values
    )
    return struct.pack(">HHHB", tx, 0, len(pdu) + 1, 1) + pdu


def test_build_adu_layout_and_transaction_id(isolated_db):
    client = ModbusClient("127.0.0.1", 502)
    assert client.build_adu(1, 3, 0x10, 2) == (
        b"\x00\x00\x00\x00\x00\x06\x01\x03\x00\x10\x00\x02"
    )
    assert client.build_adu(1, 4, 0x10, 2) == (
        b"\x00\x01\x00\x00\x00\x06\x01\x04\x00\x10\x00\x02"
    )
    with pytest.raises(ValueError):
        client.build_adu(1, 5, 0, 2)
    with pytest.raises(ValueError):
        client.build_adu(1, 3, 0, 126)


def test_parse_response_registers_and_errors(isolated_db):
    client = ModbusClient("127.0.0.1", 502)
    assert client.parse_response(_response_bytes([0x1234, 0x5678])) == [0x1234, 0x5678]
    assert client.parse_response(_response_bytes([1], func=4, tx=7)) == [1]

    exception_frame = struct.pack(">HHHB", 1, 0, 3, 1) + b"\x83\x02"
    with pytest.raises(ModbusError):
        client.parse_response(exception_frame)
    with pytest.raises(ModbusError):
        client.parse_response(b"\x00" * 5)
    with pytest.raises(ModbusError, match="协议"):
        client.parse_response(struct.pack(">HHHB", 1, 1, 7, 1) + b"\x03\x02\x12\x34")
    with pytest.raises(ModbusError, match="字节数"):
        client.parse_response(struct.pack(">HHHB", 1, 0, 6, 1) + b"\x03\x03\x12\x34\x56")


@pytest.mark.parametrize(
    "registers,data_type,byte_order,expected",
    [
        ([0x1234], "u16", "ABCD", 4660.0),
        ([0xFFFF], "s16", "ABCD", -1.0),
        ([0x0001, 0x0002], "u32", "ABCD", 65538.0),
        ([0x0002, 0x0001], "u32", "CDAB", 65538.0),
        ([0x0002, 0x0001], "u32", "ABCD", 131073.0),
        ([0xFFFF, 0xFFFE], "s32", "CDAB", -65537.0),
        ([0x4148, 0x0000], "f32", "ABCD", 12.5),
        ([0x0000, 0x4148], "f32", "CDAB", 12.5),
        ([0, 1, 0, 2], "u64", "ABCD", 4294967298.0),
        ([0x1234], "u16", "BADC", 13330.0),
        ([0x1234], "u16", "DCBA", 13330.0),
    ],
)
def test_decode_data_types_and_byte_orders(isolated_db, registers, data_type, byte_order, expected):
    value, quality = decode(registers, {"data_type": data_type, "byte_order": byte_order})
    assert value == pytest.approx(expected)
    assert quality == 0


def test_decode_scale_offset_and_quality(isolated_db):
    point = {"data_type": "u16", "byte_order": "ABCD", "scale": 0.1,
             "offset_const": 5.0, "min_val": 0, "max_val": 20}
    value, quality = decode([100], point)
    assert value == pytest.approx(15.0)
    assert quality == 0

    value, quality = decode([200], point)  # 25 > max_val
    assert quality == 2
    assert value == pytest.approx(25.0)

    point["min_val"] = 30
    point["max_val"] = None
    value, quality = decode([100], point)  # 15 < min_val
    assert quality == 2

    inf_value, inf_quality = decode([0x7F80, 0x0000], {"data_type": "f32"})
    assert inf_quality == 2
    assert inf_value == 0.0

    with pytest.raises(ValueError):
        decode([1], {"data_type": "u64"})
    with pytest.raises(ValueError):
        decode([1, 2], {"data_type": "u32", "byte_order": "AB"})


def test_read_registers_func3_and_func4_over_local_gateway(isolated_db):
    async def scenario():
        async with FakeGateway(values=[0x1234, 0x5678]) as gateway:
            client = ModbusClient("127.0.0.1", gateway.port, timeout_ms=300, retry=0)
            first = await client.read_registers(1, 3, 0x10, 2)
            second = await client.read_registers(2, 4, 0x20, 2)
            await asyncio.sleep(0)  # 让服务端 finally 执行
            assert first == second == [0x1234, 0x5678]
            assert gateway.last_request == struct.pack(">HHHBBHH", 1, 0, 6, 2, 4, 0x20, 2)
            assert struct.unpack(">H", gateway.requests[0][:2])[0] == 0
            assert struct.unpack(">H", gateway.requests[1][:2])[0] == 1
            assert client._writer is None  # 读毕关连接
            assert gateway.active == 0

    run(scenario())


def test_read_registers_timeout_retries_then_fails(isolated_db):
    async def scenario():
        async with FakeGateway(behavior="drop") as gateway:
            client = ModbusClient("127.0.0.1", gateway.port, timeout_ms=50, retry=1)
            with pytest.raises(ModbusTimeoutError):
                await client.read_registers(1, 3, 0, 2)
            assert gateway.connects == 2  # 首次 + 一次重试
            assert client._writer is None

    run(scenario())


def test_parse_response_validates_transaction_and_unit(isolated_db):
    """Arrange: 事务号/单元号与请求不符的回包。Act: 带期望值解析。Assert: 一律拒绝。"""
    client = ModbusClient("127.0.0.1", 502)
    frame = _response_bytes([0x1234], tx=9)
    # 单元号固定写成 1（_response_bytes 的 unit）
    assert client.parse_response(frame, 9, 1) == [0x1234]
    with pytest.raises(ModbusError, match="事务号"):
        client.parse_response(frame, 8, 1)
    with pytest.raises(ModbusError, match="单元号"):
        client.parse_response(frame, 9, 2)


def test_rtu_parse_response_validates_slave(isolated_db):
    """RTU 回包站号必须与请求一致，否则视为总线串包。"""
    from modbus import ModbusRTUTCPClient

    client = ModbusRTUTCPClient("127.0.0.1", 502)
    body = bytes([3, 3, 4, 0, 1, 0, 2])  # 站号 3 / func 3 / 4 字节 / 寄存器 1,2
    crc = client.crc16(body)
    frame = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    assert client.parse_response(frame, 3) == [1, 2]
    with pytest.raises(ModbusError, match="单元号"):
        client.parse_response(frame, 4)


def test_read_registers_does_not_retry_on_exception_response(isolated_db):
    """异常码是设备的确定性答复：重试只会再收一次同样的帧，必须立即失败。"""
    async def scenario():
        async with FakeGateway(values=[0xABCD], behavior="exception") as gateway:
            client = ModbusClient("127.0.0.1", gateway.port, timeout_ms=300, retry=2)
            with pytest.raises(ModbusError):
                await client.read_registers(1, 3, 0, 2)
            assert gateway.connects == 1, "异常响应不得重试"

    run(scenario())


def test_client_decode_delegates_to_module_function(isolated_db):
    client = ModbusClient("127.0.0.1", 502)
    assert client.decode([12], {"data_type": "u16"}) == (12.0, 0)
