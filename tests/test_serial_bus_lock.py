"""串口总线互斥：同一 COM 口的多从站必须排队发帧，且帧间静默 ≥ 3.5 字符时间。

背景（v0.1.9）：现场一根 RS485 线上挂多台表，各点位在 collector 里是独立任务。
修复前每个 read_registers 各自 `with serial.Serial(...)` 开-写-读-关，没有任何
按端口的互斥，两帧会交错写进同一根线——从站回错帧或静默，表现为「某台表读数
偶尔乱跳」，CRC 与站号校验都拦不住。这里锁死三个契约：
1. 同端口不同客户端共享同一把锁；不同端口互不干扰（键做 normcase 归一）；
2. 并发读取被真正串行化（同时在读的线程数恒为 1）；
3. 同一端口相邻两帧之间至少隔 3.5 字符时间。
"""
from __future__ import annotations

import asyncio
import struct
import threading
import time
from types import SimpleNamespace

import modbus
from modbus import ModbusRTUSerialClient, ModbusRTUTCPClient, serial_frame_gap_sec

from helpers import run


def _rtu_response(slave: int, func: int, values: list[int]) -> bytes:
    """构造合法 RTU 响应帧：addr+func+bytecount+regs+CRC(低字节在前)。"""
    body = (
        struct.pack(">BB", slave, func)
        + bytes([len(values) * 2])
        + b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
    )
    crc = ModbusRTUTCPClient.crc16(body)
    return body + bytes([crc & 0xFF, crc >> 8])


def _fake_pyserial(response: bytes, hold_sec: float = 0.02) -> tuple[SimpleNamespace, dict]:
    """构造假 pyserial：read 时睡 hold_sec，并记录进出时间与并发度。"""
    state: dict = {"concurrent": 0, "max_concurrent": 0, "frames": []}
    guard = threading.Lock()

    class FakeSerial:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._call = 0
            self.entered_at = 0.0

        def __enter__(self):
            with guard:
                state["concurrent"] += 1
                state["max_concurrent"] = max(state["max_concurrent"], state["concurrent"])
            self.entered_at = time.monotonic()
            return self

        def __exit__(self, *exc):
            with guard:
                state["concurrent"] -= 1
            return False

        def reset_input_buffer(self):
            pass

        def write(self, data):
            self._call = 0
            state["frames"].append(("write", time.monotonic(), bytes(data)))

        def read(self, n):
            time.sleep(hold_sec)
            idx = self._call
            self._call += 1
            chunk = response[:3] if idx == 0 else response[3:3 + n]
            return chunk

    fake = SimpleNamespace(
        EIGHTBITS=8, STOPBITS_ONE=1, PARITY_NONE="N", PARITY_E="E", PARITY_O="O",
        Serial=FakeSerial,
    )
    return fake, state


def test_same_port_clients_share_one_lock_and_distinct_ports_do_not():
    """同端口（含大小写/空白差异）共享锁；不同端口各持一把。"""

    async def scenario():
        a = modbus._serial_port_lock("COM3")
        b = modbus._serial_port_lock("  COM3  ")
        c = modbus._serial_port_lock("COM4")
        return a, b, c

    a, b, c = run(scenario())
    assert a is b, "同一 COM 口必须共享同一把锁"
    assert a is not c, "不同端口不得共用锁"


def test_lock_is_not_reused_across_event_loops(monkeypatch):
    """同端口跨两个事件循环读取不得抛「bound to a different event loop」。

    asyncio.Lock 首次 await 会绑定循环，模块级缓存一把锁会让第二个 asyncio.run()
    直接 RuntimeError——生产只有一个循环，但脚本/测试/工具函数换循环就会炸。
    """
    response = _rtu_response(1, 3, [0x1234, 0x5678])
    fake, _ = _fake_pyserial(response, hold_sec=0.0)
    monkeypatch.setattr(modbus, "serial", fake)

    async def one_read() -> list[int]:
        client = ModbusRTUSerialClient("COM_LOOP", 9600, timeout_ms=500, retry=0)
        return await client.read_registers(1, 3, 0, 2)

    for _ in range(3):  # 每次 asyncio.run 都是新循环
        assert run(one_read()) == [0x1234, 0x5678]


def test_serial_reads_on_same_port_are_serialized(monkeypatch):
    """两个从站并发读同一个 COM 口：同时在读的线程数恒为 1。"""
    response = _rtu_response(1, 3, [0x1234, 0x5678])
    fake, state = _fake_pyserial(response)
    monkeypatch.setattr(modbus, "serial", fake)

    async def scenario():
        c1 = ModbusRTUSerialClient("COM9", 9600, timeout_ms=500, retry=0)
        c2 = ModbusRTUSerialClient("COM9", 9600, timeout_ms=500, retry=0)
        results = await asyncio.gather(
            *(c.read_registers(1, 3, 0, 2) for c in (c1, c2) for _ in range(2)),
            return_exceptions=True,
        )
        return results

    results = run(scenario())
    assert state["max_concurrent"] == 1, f"同端口出现并发串口访问：{state['max_concurrent']}"
    assert all(r == [0x1234, 0x5678] for r in results), f"串行化后应全部读到正确值：{results}"


def test_inter_frame_gap_enforced_between_transactions(monkeypatch):
    """同一端口相邻两帧之间必须 ≥ 3.5 字符时间（9600-8N1 约 3.65ms）。"""
    response = _rtu_response(1, 3, [0x1234, 0x5678])
    fake, state = _fake_pyserial(response, hold_sec=0.0)
    monkeypatch.setattr(modbus, "serial", fake)
    gap = serial_frame_gap_sec(9600, "N")
    assert gap == 3.5 * 10 / 9600

    async def scenario():
        client = ModbusRTUSerialClient("COM7", 9600, timeout_ms=500, retry=0)
        for _ in range(3):
            assert await client.read_registers(1, 3, 0, 2) == [0x1234, 0x5678]

    run(scenario())
    writes = [ts for kind, ts, _ in state["frames"] if kind == "write"]
    assert len(writes) == 3
    gaps = [b - a for a, b in zip(writes, writes[1:])]
    assert min(gaps) >= gap * 0.9, f"帧间隔不足：{min(gaps) * 1000:.2f}ms < {gap * 1000:.2f}ms"


def test_frame_gap_scales_with_baudrate_and_parity():
    """波特率越高间隔越短；带校验位时字符按 11 位计。"""
    assert serial_frame_gap_sec(9600, "N") > serial_frame_gap_sec(115200, "N")
    assert serial_frame_gap_sec(9600, "E") > serial_frame_gap_sec(9600, "N")
    assert serial_frame_gap_sec(0, "N") > 0, "异常波特率不能产生除零"
