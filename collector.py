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
        # device_id -> 建任务时的配置签名：签名变化说明 host/port/interval/protocol
        # 被改过，旧任务必须重建，否则会一直连旧地址直到重启。
        self._sigs: dict[int, str] = {}
        self.reload_event: asyncio.Event = asyncio.Event()
        # 调度器所在的事件循环；reload() 可能由线程池里的同步路由调用，
        # 跨线程置事件必须走 call_soon_threadsafe。
        self._loop: asyncio.AbstractEventLoop | None = None
        # 复活退避：任务若崩溃后立即又崩（配置本身有问题），不能每秒重建一次，
        # 否则 CPU 空转 + 每秒一条日志。device_id -> 允许下次复活的时间（单调时钟）。
        self._revive_hold: dict[int, float] = {}
        self._revive_delay: dict[int, float] = {}

    async def start(self) -> None:
        """常驻调度器：监听 reload_event，增删设备采集协程，并定期复活异常退出的任务。"""
        self._loop = asyncio.get_running_loop()
        self.reload_event.set()  # 首次启动立即加载配置
        while True:
            try:
                if self.reload_event.is_set():
                    self.reload_event.clear()
                    self._sync_tasks()
                await asyncio.wait_for(self.reload_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                self._revive_dead_tasks()  # 每秒巡检：任务若因未捕获异常退出则重建
                continue
            except asyncio.CancelledError:
                raise

    def _revive_dead_tasks(self) -> None:
        """把已结束的采集任务从映射里摘掉并重建，避免设备静默停止采集。

        采集协程的异常分支虽已兜住常见错误，但内存错误、第三方库异常等仍可能
        让任务结束。旧实现只在配置变更时重建，任务一旦退出就永久不采集，
        与「每秒巡检兜底恢复」的注释不符。

        重建带指数退避（1s→60s）：设备配置本身有错时任务会秒崩，无退避就是
        「每秒建一次任务 + 每秒一条 COLLECTOR_REVIVE」，既烧 CPU 又冲掉审计日志。
        采集恢复正常后由 _clear_revive_backoff() 把退避清零。
        """
        now: float = time.monotonic()
        dead: dict[int, str] = {}
        for device_id, task in list(self.tasks.items()):
            if not task.done():
                continue
            self.tasks.pop(device_id, None)
            self._sigs.pop(device_id, None)
            if task.cancelled():
                dead[device_id] = "cancelled"
                continue
            exc: BaseException | None = task.exception()
            dead[device_id] = f"{type(exc).__name__}: {exc}" if exc else "ended"
        if not dead:
            return

        revived: list[str] = []
        revived_ids: set[int] = set()
        holding: list[str] = []
        for device_id, reason in dead.items():
            if now < self._revive_hold.get(device_id, 0.0):
                holding.append(f"device {device_id}")
                continue
            delay: float = self._revive_delay.get(device_id, RECONNECT_BACKOFF_BASE_SEC)
            self._revive_hold[device_id] = now + delay
            self._revive_delay[device_id] = min(
                delay * 2.0, RECONNECT_BACKOFF_MAX_SEC
            )
            revived_ids.add(device_id)
            revived.append(f"device {device_id} ({reason})")

        db.log_event_throttled(
            "warn", "collector", "COLLECTOR_REVIVE",
            f"采集任务异常退出 {len(dead)} 个，本次重建 {len(revived)} 个"
            + (f"，退避中 {len(holding)} 个" if holding else "")
            + (": " + "; ".join(revived) if revived else ""),
        )
        # 本轮判定要重建的设备必须立即建起来，hold 只拦「下一轮」的重复复活
        self._sync_tasks(forced=revived_ids)

    def _clear_revive_backoff(self, device_id: int) -> None:
        """设备恢复正常采集后清掉复活退避，下次真崩了仍能按 1 秒起步重建。"""
        self._revive_hold.pop(device_id, None)
        self._revive_delay.pop(device_id, None)

    @staticmethod
    def _device_sig(device: dict[str, Any]) -> str:
        """设备配置签名：只包含会改变采集行为的字段。

        只比「有没有这台设备」是不够的：现场改错 IP 或端口后接口照样返回保存成功，
        采集却继续连旧地址，直到重启才生效——最难排查的一类「配置不生效」。
        """
        keys: tuple[str, ...] = (
            "protocol", "host", "port", "timeout_ms", "retry", "poll_interval", "is_enabled",
        )
        return "|".join(f"{key}={device.get(key)}" for key in keys)

    def _sync_tasks(self, forced: set[int] | None = None) -> None:
        """按当前 device 配置快照调整任务：失效的取消，新增的建，改配置的重建。

        forced：本次必须重建的设备（复活流程判定过，跳过退避检查）。
        """
        force: set[int] = set(forced or ())
        # manual 设备（模拟器预置的手动表/机械表）不参与 Modbus 采集
        devices: list[dict[str, Any]] = [
            dict(row) for row in db.query(
                "SELECT * FROM device WHERE is_enabled=1 AND protocol!='manual' ORDER BY id"
            )
        ]
        wanted: dict[int, dict[str, Any]] = {int(device["id"]): device for device in devices}

        changed: list[str] = []
        for device_id, task in list(self.tasks.items()):
            if device_id not in wanted or task.done():
                if not task.done():
                    task.cancel()  # 配置变更：旧任务会在下一个 await 处退出
                del self.tasks[device_id]
                self._sigs.pop(device_id, None)
                continue
            signature: str = self._device_sig(wanted[device_id])
            if signature != self._sigs.get(device_id):
                # host/port/interval/protocol 改了：旧任务手里的配置快照已过期
                task.cancel()
                del self.tasks[device_id]
                self._sigs.pop(device_id, None)
                changed.append(str(device_id))

        now: float = time.monotonic()
        for device_id, device in wanted.items():
            if device_id in self.tasks:
                continue
            if device_id not in force and now < self._revive_hold.get(device_id, 0.0):
                continue  # 复活退避中，等巡检下一轮
            self.tasks[device_id] = asyncio.create_task(
                self.poll_device(device), name=f"ems-poll-device-{device_id}"
            )
            self._sigs[device_id] = self._device_sig(device)

        if changed:
            db.log_event(
                "info", "collector", "COLLECTOR_HOT_RELOAD",
                f"设备配置变更，已重建采集任务: device {', '.join(changed)}",
            )

        self.config_sig = "|".join(
            f"{device['id']}:{self._device_sig(device)}" for device in devices
        )

    def _client_for(self, device: dict[str, Any]) -> BaseModbusClient:
        """按 device.protocol 分派三协议客户端；manual 设备不参与 Modbus 采集。"""
        protocol: str = str(device.get("protocol") or "modbus_tcp").lower()
        timeout_ms: int = int(device.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        retry: int = int(device.get("retry") if device.get("retry") is not None else DEFAULT_RETRY)
        host: str = str(device.get("host") or "127.0.0.1")
        raw_port: Any = device.get("port")
        if protocol == "modbus_rtu_serial":
            # 串口设备：host=COM 口，port=波特率。原样使用配置值——
            # 低波特率（1200/2400/4800）是现场老表常见设置，静默改写会直接读不到数据。
            # 注意不能用 `port or 502` 兜底：0 会被吞成 502，等于静默改写波特率。
            try:
                baudrate: int = int(raw_port)
            except (TypeError, ValueError) as exc:
                raise ModbusError(f"串口波特率非法: {raw_port!r}") from exc
            if baudrate <= 0:
                raise ModbusError(f"串口波特率必须为正整数，当前为 {raw_port!r}")
            # 现场不少电表出厂是 8E1；校验位必须透传，写死 N 时一帧都收不到
            parity: str = str(device.get("parity") or "N")
            return ModbusRTUSerialClient(host, baudrate, timeout_ms, retry, parity=parity)
        port: int = int(raw_port or 502)
        if protocol == "modbus_tcp":
            return ModbusClient(host, port, timeout_ms, retry)
        if protocol == "modbus_rtu_tcp":
            return ModbusRTUTCPClient(host, port, timeout_ms, retry)
        if protocol == "manual":
            raise ModbusError("manual 设备不参与 Modbus 采集")
        raise ModbusError(f"不支持的协议: {protocol}")

    async def poll_device(self, device: dict[str, Any]) -> None:
        """单设备轮询：读配置快照 -> 合并读取 -> 解码写库 -> 休眠 -> 重连退避。

        所有 DB 操作都丢进线程池（asyncio.to_thread）。这些调用看着都是「毫秒级短查询」，
        但 db 用的是全局单连接 + 进程内互斥锁：报表导出、日结补算、90 天清理这类
        长查询持锁期间，直接在事件循环线程里等这把锁，会把整个循环按住——
        /healthz 变慢、其它设备的采集也一起停摆。卸载到线程池后，等锁的是工作线程。
        """
        device_id: int = int(device["id"])
        backoff: float = RECONNECT_BACKOFF_BASE_SEC

        while True:
            try:
                point_rows = await asyncio.to_thread(
                    db.query,
                    "SELECT * FROM point WHERE device_id=? AND is_enabled=1 "
                    "ORDER BY slave_id, func_code, reg_offset",
                    (device_id,),
                )
                points: list[dict[str, Any]] = [dict(row) for row in point_rows]
                if not points:
                    # 无点位时仅休眠，等待配置热生效
                    await asyncio.sleep(int(device.get("poll_interval") or DEFAULT_POLL_INTERVAL))
                    continue

                # 一轮采集共用一个时间戳，便于按轮次对齐与缺口判定
                now_ts: int = int(time.time())
                groups: list[dict[str, Any]] = self.merge_reads(device, points)
                rows: list[tuple[int, int, float, int]] = []
                any_ok: bool = False
                for group in groups:
                    client: BaseModbusClient = self._client_for(device)
                    try:
                        registers: list[int] = await client.read_registers(
                            group["slave_id"], group["func_code"], group["offset"], group["count"]
                        )
                    except (ModbusError, OSError, ValueError) as exc:
                        # 读取失败只影响本组：其余分组照常采集，不整轮丢弃；
                        # 本组点位按「保持上一值 + 质量码 1」补点，缺口在看板上可见
                        carried: list[tuple[int, int, float, int]] = await asyncio.to_thread(
                            self._carry_forward, group["points"], now_ts
                        )
                        rows.extend(carried)
                        code: str = (
                            "READ_TIMEOUT" if isinstance(exc, ModbusTimeoutError) else "READ_FAIL"
                        )
                        # 首次掉线与「已经连续掉了几轮」要分开说：第 2 轮起没有可保持的
                        # 正常值，文案还写「0 个点位保持上一值并标记超时」，现场会以为
                        # 采集器只处理了 0 个点位（而不是全部都在超时）。
                        detail: str = (
                            f"{len(carried)} 个点位保持上一值并标记超时"
                            if carried else "持续离线（本轮没有可保持的正常值）"
                        )
                        await asyncio.to_thread(
                            db.log_event_throttled,
                            "warn", "collector", code,
                            f"设备 {device_id} 分组读取失败，{detail}: {exc}", device_id,
                        )
                        await asyncio.to_thread(
                            db.execute,
                            "UPDATE device SET last_error=? WHERE id=?", (str(exc), device_id),
                        )
                        continue
                    finally:
                        await client.close()  # read_registers 已关，双重保护

                    any_ok = True
                    for point in group["points"]:
                        start: int = int(point["reg_offset"]) - group["offset"]
                        count: int = int(point["reg_count"])
                        chunk: list[int] = registers[start:start + count]
                        try:
                            value, quality = client.decode(chunk, point)
                        except (ValueError, IndexError) as exc:
                            await asyncio.to_thread(
                                db.log_event,
                                "warn", "collector", "DECODE_FAIL",
                                f"点位 {point['id']} 解析失败: {exc}", int(point["id"]),
                            )
                            continue
                        rows.append((int(point["id"]), now_ts, value, quality))
                        if quality == QUALITY_TIMEOUT:
                            await asyncio.to_thread(
                                db.log_event_throttled,
                                "warn", "collector", "READ_TIMEOUT",
                                f"点位 {point['id']} 读取超时", int(point["id"]),
                            )
                        elif quality == QUALITY_OUT_OF_RANGE:
                            await asyncio.to_thread(
                                db.log_event_throttled,
                                "warn", "collector", "VALUE_OUT_OF_RANGE",
                                f"点位 {point['id']} 越界: {value}", int(point["id"]),
                            )

                await asyncio.to_thread(
                    self.write_batch, rows, device_id if any_ok else None
                )
                if any_ok:
                    self._clear_revive_backoff(device_id)
                    backoff = RECONNECT_BACKOFF_BASE_SEC
                    await asyncio.sleep(
                        max(1, int(device.get("poll_interval") or DEFAULT_POLL_INTERVAL))
                    )
                else:
                    # 整轮全部失败：按指数退避重连，避免对掉线设备高频打点
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX_SEC)

            except asyncio.CancelledError:
                raise  # 热生效/关停时正常退出
            except (ModbusError, OSError, ValueError, sqlite3.Error) as exc:
                await asyncio.to_thread(
                    db.log_event_throttled,
                    "error", "collector", "READ_FAIL",
                    f"设备 {device_id}({device.get('host')}) 采集失败: {exc}", device_id,
                )
                await asyncio.to_thread(
                    db.execute,
                    "UPDATE device SET last_error=? WHERE id=?", (str(exc), device_id),
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

    def write_batch(
        self, rows: list[tuple[int, int, float, int]], mark_ok_device_id: int | None = None
    ) -> None:
        """同步短事务写原始读数与最新值；latest 仅在时间戳更新时覆盖。

        两条 executemany 必须同生共死：旧实现用全局连接，第一条成功后第二条失败时
        不会回滚，随后任意线程的提交（例如 log_event 里的 execute）会把「reading 写进去了、
        reading_latest 没写」的半截状态一并提交，于是最新值与历史读数长期不一致。

        mark_ok_device_id 把「本轮采集成功」的设备状态更新并进同一个事务：
        分两条语句写时，中间被取消（热重载/关停）会留下「读数已落库、last_ok_at 还是旧的」，
        看上去像这一轮没成功；而且这两件事本来就该同生共死。
        """
        if not rows:
            return
        try:
            with db.transaction() as conn:
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
                if mark_ok_device_id is not None:
                    conn.execute(
                        "UPDATE device SET last_ok_at=?, last_error=NULL WHERE id=?",
                        (int(rows[0][1]), int(mark_ok_device_id)),
                    )
        except sqlite3.Error as exc:
            # 事务已回滚，raw 与 latest 都没有落库，下个轮询周期会重试
            db.log_event("error", "collector", "WRITE_FAIL", f"写读数失败（整批已回滚）: {exc}")

    def _carry_forward(
        self, points: list[dict[str, Any]], ts: int
    ) -> list[tuple[int, int, float, int]]:
        """读取失败时按「保持上一值 + 质量码 1」补点，让缺口在看板上可见。

        没有历史读数的点位无法伪造，直接跳过（由覆盖不足判定兜底）。
        """
        point_ids: list[int] = [int(point["id"]) for point in points]
        if not point_ids:
            return []
        placeholders: str = ",".join("?" for _ in point_ids)
        latest: list[Any] = db.query(
            f"SELECT point_id, value FROM reading_latest WHERE point_id IN ({placeholders}) "
            "AND quality IN (0,3)",
            tuple(point_ids),
        )
        return [
            (int(row["point_id"]), int(ts), float(row["value"]), QUALITY_TIMEOUT)
            for row in latest
        ]

    def reload(self) -> None:
        """配置变更后调用：置事件，调度器在下一轮起点重建采集任务。

        这个方法经常从线程池（FastAPI 的同步路由）里被调用，而 asyncio.Event 不是
        线程安全的：直接 set() 只是把 waiter 的 future 标成已完成，不会唤醒事件循环，
        极端情况下还会和循环里的 wait/clear 抢同一批 waiter。表现是「点了保存，
        配置过一会儿才生效」——旧实现完全靠 start() 里那 1 秒超时轮询兜着。
        跨线程一律走 call_soon_threadsafe，把 set 排进循环线程执行。
        """
        loop: asyncio.AbstractEventLoop | None = self._loop
        if loop is not None and loop.is_running():
            try:
                running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                self.reload_event.set()
            else:
                loop.call_soon_threadsafe(self.reload_event.set)
        else:
            self.reload_event.set()  # 调度器还没起来（启动前/测试里）：先置位，start() 会读
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
        self._sigs.clear()


collector: Collector = Collector()
