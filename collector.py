"""采集调度引擎：按 device 起协程、合并相邻寄存器、写 reading/reading_latest、
断网指数退避重连、配置热生效（reload_event）、点位试读（probe_point）。

所有 DB 操作均为同步短事务，不在 await 之间持有游标（db.py 全局单连接）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

import db
from modbus import (
    BaseModbusClient,
    ModbusClient,
    ModbusError,
    ModbusRTUSerialClient,
    ModbusRTUTCPClient,
    ModbusTimeoutError,
    decode,
)
from settings import (
    BYTE_ORDERS,
    DEFAULT_BAUDRATE,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RETRY,
    DEFAULT_TIMEOUT_MS,
    MODBUS_MAX_REGISTERS,
    PROBE_TIMEOUT_MS,
    QUALITY_OUT_OF_RANGE,
    QUALITY_TIMEOUT,
    RECONNECT_BACKOFF_BASE_SEC,
    RECONNECT_BACKOFF_MAX_SEC,
)


class Collector:
    """单进程采集器：维护 device_id -> Task 的映射，配置变化后整体重建任务。"""

    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.config_sig: str = ""
        self.reload_event: asyncio.Event = asyncio.Event()

    async def start(self) -> None:
        """常驻调度器：监听 reload_event，增删设备采集协程。"""
        self.reload_event.set()  # 首次启动立即加载配置
        while True:
            try:
                if self.reload_event.is_set():
                    self.reload_event.clear()
                    self._sync_tasks()
                await asyncio.wait_for(self.reload_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue  # 每秒巡检一次，便于兜底恢复意外结束的任务
            except asyncio.CancelledError:
                raise

    def _sync_tasks(self) -> None:
        """依据当前 device 配置快照，取消失效任务并为新增设备建任务。"""
        # manual 设备（模拟器预置的手动表/机械表）不参与 Modbus 采集
        devices: list[dict[str, Any]] = [
            dict(row) for row in db.query(
                "SELECT * FROM device WHERE is_enabled=1 AND protocol!='manual' ORDER BY id"
            )
        ]
        wanted: dict[int, dict[str, Any]] = {int(device["id"]): device for device in devices}

        for device_id, task in list(self.tasks.items()):
            if device_id not in wanted or task.done():
                if not task.done():
                    task.cancel()  # 配置变更：旧任务会在下一个 await 处退出
                del self.tasks[device_id]

        for device_id, device in wanted.items():
            if device_id not in self.tasks:
                self.tasks[device_id] = asyncio.create_task(
                    self.poll_device(device), name=f"ems-poll-device-{device_id}"
                )

        self.config_sig = "|".join(
            f"{device['id']}:{device.get('host')}:{device.get('port')}:{device.get('poll_interval')}"
            for device in devices
        )

    def _client_for(self, device: dict[str, Any]) -> BaseModbusClient:
        """按 device.protocol 分派三协议客户端；manual 设备不参与 Modbus 采集。"""
        protocol: str = str(device.get("protocol") or "modbus_tcp").lower()
        timeout_ms: int = int(device.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        retry: int = int(device.get("retry") if device.get("retry") is not None else DEFAULT_RETRY)
        host: str = str(device.get("host") or "127.0.0.1")
        port: int = int(device.get("port") or 502)
        if protocol == "modbus_tcp":
            return ModbusClient(host, port, timeout_ms, retry)
        if protocol == "modbus_rtu_tcp":
            return ModbusRTUTCPClient(host, port, timeout_ms, retry)
        if protocol == "modbus_rtu_serial":
            # 串口设备：host=COM 口，port=波特率（非法的占位值回退默认波特率）
            baudrate: int = port if port >= DEFAULT_BAUDRATE else DEFAULT_BAUDRATE
            return ModbusRTUSerialClient(host, baudrate, timeout_ms, retry)
        if protocol == "manual":
            raise ModbusError("manual 设备不参与 Modbus 采集")
        raise ModbusError(f"不支持的协议: {protocol}")

    async def poll_device(self, device: dict[str, Any]) -> None:
        """单设备轮询：读配置快照 -> 合并读取 -> 解码写库 -> 休眠 -> 重连退避。"""
        device_id: int = int(device["id"])
        backoff: float = RECONNECT_BACKOFF_BASE_SEC

        while True:
            try:
                points: list[dict[str, Any]] = [
                    dict(row)
                    for row in db.query(
                        "SELECT * FROM point WHERE device_id=? AND is_enabled=1 "
                        "ORDER BY slave_id, func_code, reg_offset",
                        (device_id,),
                    )
                ]
                if not points:
                    # 无点位时仅休眠，等待配置热生效
                    await asyncio.sleep(int(device.get("poll_interval") or DEFAULT_POLL_INTERVAL))
                    continue

                groups: list[dict[str, Any]] = self.merge_reads(device, points)
                rows: list[tuple[int, int, float, int]] = []
                for group in groups:
                    client: BaseModbusClient = self._client_for(device)
                    try:
                        registers: list[int] = await client.read_registers(
                            group["slave_id"], group["func_code"], group["offset"], group["count"]
                        )
                    finally:
                        await client.close()  # read_registers 已关，双重保护

                    for point in group["points"]:
                        start: int = int(point["reg_offset"]) - group["offset"]
                        count: int = int(point["reg_count"])
                        chunk: list[int] = registers[start:start + count]
                        try:
                            value, quality = client.decode(chunk, point)
                        except (ValueError, IndexError) as exc:
                            db.log_event(
                                "warn", "collector", "DECODE_FAIL",
                                f"点位 {point['id']} 解析失败: {exc}", int(point["id"]),
                            )
                            continue
                        rows.append((int(point["id"]), int(time.time()), value, quality))
                        if quality == QUALITY_TIMEOUT:
                            db.log_event(
                                "warn", "collector", "READ_TIMEOUT",
                                f"点位 {point['id']} 读取超时", int(point["id"]),
                            )
                        elif quality == QUALITY_OUT_OF_RANGE:
                            db.log_event(
                                "warn", "collector", "VALUE_OUT_OF_RANGE",
                                f"点位 {point['id']} 越界: {value}", int(point["id"]),
                            )

                self.write_batch(rows)
                db.execute(
                    "UPDATE device SET last_ok_at=?, last_error=NULL WHERE id=?",
                    (int(time.time()), device_id),
                )
                backoff = RECONNECT_BACKOFF_BASE_SEC
                await asyncio.sleep(max(1, int(device.get("poll_interval") or DEFAULT_POLL_INTERVAL)))

            except asyncio.CancelledError:
                raise  # 热生效/关停时正常退出
            except (ModbusError, OSError, ValueError, sqlite3.Error) as exc:
                db.log_event(
                    "error", "collector", "READ_FAIL",
                    f"设备 {device_id}({device.get('host')}) 采集失败: {exc}", device_id,
                )
                db.execute(
                    "UPDATE device SET last_error=? WHERE id=?", (str(exc), device_id)
                )
                # 断网/异常：指数退避重连，上限 60 秒
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX_SEC)

    def merge_reads(self, device: dict[str, Any], points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按 (slave_id, func_code) 分组并按 offset 排序，合并相邻/重叠区间。

        合并后单次寄存器数不超过 125，超限则拆成新请求（device 参数保留供扩展）。
        """
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for point in points:
            key = (int(point["slave_id"]), int(point["func_code"]))
            grouped.setdefault(key, []).append(point)

        merged: list[dict[str, Any]] = []
        for (slave_id, func_code), items in sorted(grouped.items()):
            items.sort(key=lambda item: int(item["reg_offset"]))
            current: dict[str, Any] | None = None
            for point in items:
                start: int = int(point["reg_offset"])
                end: int = start + int(point["reg_count"])
                if current is None:
                    current = {
                        "slave_id": slave_id,
                        "func_code": func_code,
                        "offset": start,
                        "count": end - start,
                        "points": [point],
                    }
                    continue
                current_end: int = int(current["offset"]) + int(current["count"])
                # 仅合并重叠或真正相邻（start <= current_end）的区间；
                # 中间有空洞时不合并，避免一次读取大量无关寄存器。
                if start > current_end:
                    merged.append(current)
                    current = {
                        "slave_id": slave_id,
                        "func_code": func_code,
                        "offset": start,
                        "count": end - start,
                        "points": [point],
                    }
                    continue
                new_start: int = min(int(current["offset"]), start)
                new_end: int = max(current_end, end)
                if new_end - new_start <= MODBUS_MAX_REGISTERS:
                    current["offset"] = new_start
                    current["count"] = new_end - new_start
                    current["points"].append(point)
                else:
                    merged.append(current)
                    current = {
                        "slave_id": slave_id,
                        "func_code": func_code,
                        "offset": start,
                        "count": end - start,
                        "points": [point],
                    }
            if current is not None:
                merged.append(current)
        return merged

    def write_batch(self, rows: list[tuple[int, int, float, int]]) -> None:
        """同步短事务写原始读数与最新值；latest 仅在时间戳更新时覆盖。"""
        if not rows:
            return
        conn = db.get_conn()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO reading (point_id, ts, value, quality) VALUES (?,?,?,?)",
                rows,
            )
            conn.executemany(
                "INSERT INTO reading_latest (point_id, ts, value, quality) VALUES (?,?,?,?) "
                "ON CONFLICT(point_id) DO UPDATE SET "
                "ts=excluded.ts, value=excluded.value, quality=excluded.quality "
                "WHERE excluded.ts >= reading_latest.ts",
                rows,
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            db.log_event("error", "collector", "WRITE_FAIL", f"写读数失败: {exc}")

    def reload(self) -> None:
        """配置变更后调用：置事件，调度器在下一轮起点重建采集任务。"""
        self.reload_event.set()
        db.log_event("info", "api", "COLLECTOR_RELOAD", "采集配置热生效触发")

    async def probe_point(self, point_id: int) -> dict[str, Any]:
        """点位试读：返回原始寄存器 hex、解析值与各字节序对照，3 秒内返回。"""
        row: Any = db.query_one("SELECT * FROM point WHERE id=?", (point_id,))
        if row is None:
            raise LookupError(f"点位 {point_id} 不存在")
        point: dict[str, Any] = dict(row)
        device_row: Any = db.query_one("SELECT * FROM device WHERE id=?", (point["device_id"],))
        if device_row is None:
            raise LookupError(f"点位关联的设备 {point['device_id']} 不存在")
        device: dict[str, Any] = dict(device_row)

        slave_id: int = int(point["slave_id"])
        func_code: int = int(point["func_code"])
        offset: int = int(point["reg_offset"])
        count: int = int(point["reg_count"])
        timeout_ms: int = min(int(device.get("timeout_ms") or DEFAULT_TIMEOUT_MS), PROBE_TIMEOUT_MS)
        # 试读使用设备原始超时上限（≤3s）；失败统一映射为 RuntimeError -> 502
        probe_device: dict[str, Any] = dict(device)
        probe_device["timeout_ms"] = timeout_ms
        probe_device["retry"] = 0
        client: BaseModbusClient = self._client_for(probe_device)
        started: float = time.monotonic()
        try:
            registers: list[int] = await client.read_registers(slave_id, func_code, offset, count)
        except (ModbusError, OSError, ValueError) as exc:
            raise RuntimeError(f"点位试读失败: {exc}") from exc
        finally:
            await client.close()

        parsed_value: float
        quality: int
        parsed_value, quality = decode(registers, point)
        # 现场 byte_order 需实测：返回四种字节序解析值供比对确认（设计假设 3）
        parsed_orders: dict[str, float | None] = {}
        for order in BYTE_ORDERS:
            probe: dict[str, Any] = dict(point)
            probe["byte_order"] = order
            try:
                parsed_orders[order] = decode(registers, probe)[0]
            except (ValueError, KeyError):
                parsed_orders[order] = None

        return {
            "point_id": point_id,
            "raw_hex": " ".join(f"{value:04X}" for value in registers),
            "raw_regs": registers,
            "parsed_value": parsed_value,
            "quality": quality,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "data_type": point["data_type"],
            "byte_order": point["byte_order"],
            "parsed_orders": parsed_orders,
        }

    async def shutdown(self) -> None:
        """关停所有设备任务（供 main.lifespan 退出时调用）。"""
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


collector: Collector = Collector()
