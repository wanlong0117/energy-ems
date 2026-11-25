"""内置模拟引擎（simulate 模式专用）：预置配置基线并持续向真实表写读数。

原则：无 mock 分支、无单独假表。所有数据写入 reading / reading_latest，
与 live 采集器共用同一套 API/看板/报表链路；模拟模式启动时采集器不运行，避免主键冲突。

基线规模：4 栋 × 25 电表（每表 reading 累计 + power 瞬时 2 点）+
5 组冷却塔电极 × 3 点（电导率 f32 μS/cm、电流 f32 A、排污阀 u16 0/1）+
5 个机械排水量手动点（unit=m³，protocol=manual device）。
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import db
from collector import collector
from settings import (
    QUALITY_OK,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
)

# 4 栋 × 25 表
BUILDINGS: tuple[str, ...] = ("A", "B", "C", "D")
METERS_PER_BUILDING: int = 25
PURPOSE_CYCLE: tuple[str, ...] = ("production", "hvac", "lighting", "other")

# 各用途日增电量范围（kWh/日）：-production/hvac/lighting/other
PURPOSE_KWH_PER_DAY: dict[str, tuple[float, float]] = {
    "production": (300.0, 800.0),
    "hvac": (200.0, 600.0),
    "lighting": (50.0, 150.0),
    "other": (80.0, 200.0),
    "total": (100.0, 500.0),
}
PURPOSE_POWER_BASE: dict[str, float] = {
    "production": 300.0,
    "hvac": 220.0,
    "lighting": 80.0,
    "other": 120.0,
    "total": 150.0,
}

TOWERS: int = 5
CONDUCTIVITY_RANGE: tuple[float, float] = (200.0, 3000.0)
CURRENT_RANGE: tuple[float, float] = (0.0, 50.0)
POWER_RANGE: tuple[float, float] = (5.0, 500.0)

# reading.value 沿用 v1 工程值语义（kWh），hourly/daily.energy_wh 才是毫度；
# 若此处直接写毫度，rollup_hour 的 ×1e6 换算会放大一万亿倍。
INITIAL_KWH: float = 1000.0  # 起步累计电量（kWh），保证累计值为正


class Simulator:
    """模拟数据服务：ensure_baseline 幂等预置配置，start 周期写真实读数。"""

    def __init__(self, interval_sec: int, config: dict[str, Any] | None = None) -> None:
        self.interval_sec: int = max(1, int(interval_sec))
        self.config: dict[str, Any] = dict(config or {})
        # point_id -> 上次写入的工程值（累计电量 / 电导率 / 电流 / 功率）
        self.baselines: dict[int, float] = {}
        self.task: asyncio.Task[None] | None = None
        self._rng: random.Random = random.Random(20261125)
        self._tick_no: int = 0
        self._last_ts: int = 0
        # 运行期点位目录，由 ensure_baseline() 从真实表读取
        self.meter_points: dict[int, int] = {}
        self.meter_purposes: dict[int, str] = {}
        self.power_points: dict[int, int] = {}
        self.conductivity_points: list[int] = []
        self.current_points: list[int] = []
        self.valve_points: list[int] = []
        self.manual_points: list[int] = []

    # ---------- 基线预置 ----------
    def _ensure_node(self, name: str) -> int:
        row = db.query_one("SELECT id FROM node WHERE name=? LIMIT 1", (name,))
        if row is not None:
            return int(row["id"])
        return int(db.execute(
            "INSERT INTO node (name, kind, sort_order, is_active) VALUES (?, 'area', 0, 1)",
            (name,),
        ))

    def _ensure_device(
        self, name: str, node_id: int, protocol: str = "manual", host: str = "127.0.0.1"
    ) -> int:
        row = db.query_one("SELECT id FROM device WHERE name=? LIMIT 1", (name,))
        if row is not None:
            return int(row["id"])
        return int(db.execute(
            "INSERT INTO device (name, node_id, protocol, host, port, timeout_ms, "
            "retry, poll_interval, is_enabled) VALUES (?,?,?,?,502,3000,2,60,1)",
            (name, node_id, protocol, host),
        ))

    def _ensure_point(
        self,
        name: str,
        device_id: int,
        slave_id: int,
        reg_offset: int,
        data_type: str,
        reg_count: int,
        unit: str | None,
        point_role: str,
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> int:
        """按 name 幂等取点；寄存器区间按 (device, slave, func, offset) 唯一。"""
        row = db.query_one("SELECT id FROM point WHERE name=? LIMIT 1", (name,))
        if row is not None:
            return int(row["id"])
        return int(db.execute(
            "INSERT INTO point (name, device_id, slave_id, func_code, reg_offset, "
            "reg_count, data_type, byte_order, scale, offset_const, min_val, max_val, "
            "unit, point_role, is_enabled) "
            "VALUES (?,?,?,3,?,?,?,'ABCD',1,0,?,?,?,?,1)",
            (
                name, device_id, slave_id, reg_offset, reg_count, data_type,
                min_val, max_val, unit, point_role,
            ),
        ))

    def ensure_baseline(self) -> None:
        """首次启动补齐 4×25 电表 + 5 电极×3 点 + 5 机械排水点（可重复调用）。"""
        building_nodes: dict[str, int] = {
            building: self._ensure_node(f"模拟-{building}栋") for building in BUILDINGS
        }
        tower_node: int = self._ensure_node("模拟-冷却塔")

        building_devices: dict[str, int] = {
            building: self._ensure_device(
                f"模拟-{building}栋网关", building_nodes[building], "manual"
            )
            for building in BUILDINGS
        }
        electrode_device: int = self._ensure_device(
            "模拟-冷却塔电极设备", tower_node, "manual"
        )
        manual_device: int = self._ensure_device(
            "模拟-冷却塔机械表设备", tower_node, "manual"
        )

        index: int = 0
        for building in BUILDINGS:
            for number in range(1, METERS_PER_BUILDING + 1):
                index += 1
                purpose: str = PURPOSE_CYCLE[(index - 1) % len(PURPOSE_CYCLE)]
                meter_name: str = f"模拟-{building}栋-{number:02d}号电表"
                meter_row = db.query_one("SELECT id FROM meter WHERE name=? LIMIT 1", (meter_name,))
                if meter_row is not None:
                    meter_id = int(meter_row["id"])
                else:
                    meter_id = int(db.execute(
                        "INSERT INTO meter (name, node_id, category_id, purpose, "
                        "is_counted, daily_8am, is_enabled) VALUES (?,?,1,?,1,1,1)",
                        (meter_name, building_nodes[building], purpose),
                    ))

                reading_id: int = self._ensure_point(
                    f"{meter_name}-累计电量", building_devices[building], number,
                    0, "u64", 4, "kWh", "energy", 0.0, None,
                )
                power_id: int = self._ensure_point(
                    f"{meter_name}-瞬时功率", building_devices[building], number,
                    4, "f32", 2, "kW", "power", POWER_RANGE[0], POWER_RANGE[1],
                )
                db.execute(
                    "INSERT OR IGNORE INTO meter_point (meter_id, point_id, role) VALUES (?,?,'reading')",
                    (meter_id, reading_id),
                )
                db.execute(
                    "INSERT OR IGNORE INTO meter_point (meter_id, point_id, role) VALUES (?,?,'power')",
                    (meter_id, power_id),
                )

        for tower in range(1, TOWERS + 1):
            # 电极三参数：电导率 / 电流 / 排污阀
            conductivity_id: int = self._ensure_point(
                f"模拟-冷却塔T{tower}-电导率", electrode_device, tower,
                0, "f32", 2, "μS/cm", "other", *CONDUCTIVITY_RANGE,
            )
            current_id: int = self._ensure_point(
                f"模拟-冷却塔T{tower}-电流", electrode_device, tower,
                2, "f32", 2, "A", "other", *CURRENT_RANGE,
            )
            valve_id: int = self._ensure_point(
                f"模拟-冷却塔T{tower}-排污阀", electrode_device, tower,
                4, "u16", 1, "", "status", 0.0, 1.0,
            )
            self.conductivity_points.append(conductivity_id)
            self.current_points.append(current_id)
            self.valve_points.append(valve_id)

            # 机械排水量：只由手动录入写，模拟器不产数
            drain_id: int = self._ensure_point(
                f"模拟-冷却塔T{tower}-机械排水量", manual_device, tower,
                0, "f32", 2, "m³", "other", 0.0, None,
            )
            self.manual_points.append(drain_id)

        # 默认电价，保证模拟日报有电费数值
        db.execute(
            "INSERT INTO tariff (name, price_cent, is_default) "
            "SELECT '默认电价', 80, 1 WHERE NOT EXISTS (SELECT 1 FROM tariff)"
        )
        self._load_catalogs()
        self._seed_baselines()
        db.log_event(
            "info", "api", "BASELINE_READY",
            f"模拟基线就绪：电表 100 / 电极点 {len(self.conductivity_points) * 3} "
            f"/ 手动点 {len(self.manual_points)}",
        )

    def _load_catalogs(self) -> None:
        """从真实表读取点位目录，避免内存与 DB 不一致。"""
        meter_points: dict[int, int] = {}
        meter_purposes: dict[int, str] = {}
        power_points: dict[int, int] = {}
        rows = db.query(
            "SELECT mp.meter_id, mp.role, mp.point_id, m.purpose "
            "FROM meter_point mp JOIN meter m ON m.id=mp.meter_id "
            "WHERE m.name LIKE '模拟-%' AND m.is_enabled=1"
        )
        for row in rows:
            role: str = str(row["role"])
            if role == "reading":
                meter_points[int(row["meter_id"])] = int(row["point_id"])
                meter_purposes[int(row["meter_id"])] = str(row["purpose"])
            elif role == "power":
                power_points[int(row["meter_id"])] = int(row["point_id"])
        self.meter_points = meter_points
        self.meter_purposes = meter_purposes
        self.power_points = power_points

        self.conductivity_points = self._point_ids_by_name("模拟-冷却塔T%-电导率")
        self.current_points = self._point_ids_by_name("模拟-冷却塔T%-电流")
        self.valve_points = self._point_ids_by_name("模拟-冷却塔T%-排污阀")
        self.manual_points = self._point_ids_by_name("模拟-冷却塔T%-机械排水量")

    @staticmethod
    def _point_ids_by_name(pattern: str) -> list[int]:
        rows = db.query(
            "SELECT id FROM point WHERE name LIKE ? ORDER BY id", (pattern,)
        )
        return [int(row["id"]) for row in rows]

    def _seed_baselines(self) -> None:
        """以 reading_latest 为初值，缺失时按用途给确定性起始值。"""
        for index, (meter_id, point_id) in enumerate(sorted(self.meter_points.items())):
            current: float | None = self._latest_value(point_id)
            if current is None:
                current = INITIAL_KWH + index * 500.0
            self.baselines[point_id] = current
        for index, point_id in enumerate(self.conductivity_points):
            current = self._latest_value(point_id)
            self.baselines[point_id] = (
                current if current is not None else 800.0 + index * 120.0
            )
        for index, point_id in enumerate(self.current_points):
            current = self._latest_value(point_id)
            self.baselines[point_id] = current if current is not None else 8.0 + index
        for point_id, purpose in self.meter_purposes.items():
            power_id = self.power_points.get(point_id)
            if power_id is None:
                continue
            current = self._latest_value(power_id)
            self.baselines[power_id] = (
                current if current is not None else PURPOSE_POWER_BASE.get(purpose, 100.0)
            )

    @staticmethod
    def _latest_value(point_id: int) -> float | None:
        row = db.query_one(
            "SELECT value FROM reading_latest WHERE point_id=?", (point_id,)
        )
        return float(row["value"]) if row is not None else None

    # ---------- 产数 ----------
    def tick(self, ts_override: int | None = None) -> None:
        """生成一个时间片的所有点位读数，累计电量单调增。

        ts_override 仅供启动回填历史时间点使用，正常调度不传参。
        """
        now: int = int(time.time())
        ts: int = (
            int(ts_override) if ts_override is not None
            else (now // self.interval_sec) * self.interval_sec
        )
        rows: list[tuple[int, int, float, int]] = []
        fraction: float = self.interval_sec / SECONDS_PER_DAY

        for meter_id, point_id in sorted(self.meter_points.items()):
            purpose: str = self.meter_purposes.get(meter_id, "other")
            low, high = PURPOSE_KWH_PER_DAY.get(purpose, (100.0, 500.0))
            delta_kwh: float = self._rng.uniform(low, high) * fraction
            previous: float = self.baselines.get(point_id, INITIAL_KWH)
            value: float = previous + max(0.001, delta_kwh)  # 严格单调，单位 kWh
            self.baselines[point_id] = value
            rows.append((point_id, ts, value, QUALITY_OK))

            power_id: int | None = self.power_points.get(meter_id)
            if power_id is not None:
                base: float = PURPOSE_POWER_BASE.get(purpose, 100.0)
                power: float = base * self._rng.uniform(0.9, 1.1)
                power = max(POWER_RANGE[0], min(POWER_RANGE[1], power))
                self.baselines[power_id] = power
                rows.append((power_id, ts, power, QUALITY_OK))

        for index, (point_id, valve_id) in enumerate(
            zip(self.conductivity_points, self.valve_points)
        ):
            previous = self.baselines.get(point_id, 800.0 + index * 120.0)
            drift: float = self._rng.uniform(-30.0, 30.0)
            conductivity: float = max(
                CONDUCTIVITY_RANGE[0], min(CONDUCTIVITY_RANGE[1], previous + drift)
            )
            self.baselines[point_id] = conductivity
            rows.append((point_id, ts, conductivity, QUALITY_OK))

            valve_open: int = 1 if ((self._tick_no // 3) + index) % 2 == 0 else 0
            rows.append((valve_id, ts, float(valve_open), QUALITY_OK))

            current_id: int = (
                self.current_points[index] if index < len(self.current_points) else 0
            )
            if current_id:
                base_current: float = 32.0 if valve_open else 8.0
                current: float = max(
                    CURRENT_RANGE[0],
                    min(CURRENT_RANGE[1], base_current + self._rng.uniform(-2.0, 2.0)),
                )
                self.baselines[current_id] = current
                rows.append((current_id, ts, current, QUALITY_OK))

        self.write_points(rows)
        self._tick_no += 1
        self._last_ts = ts

    def write_points(self, rows: list[tuple[int, int, float, int]]) -> None:
        """与 Collector.write_batch 完全一致的 UPSERT 语义，复用同一实现。"""
        collector.write_batch(rows)

    def _backfill_if_needed(self) -> None:
        """首次启动回填最近 5 个时间片，保证小时/日汇总首屏即有非零电量。"""
        if not self.meter_points:
            return
        first_point: int = sorted(self.meter_points.values())[0]
        if self._latest_value(first_point) is not None:
            return  # 已有历史读数，重启不重复回填
        aligned: int = (int(time.time()) // self.interval_sec) * self.interval_sec
        timestamps: list[int] = []
        candidate: int = aligned - self.interval_sec
        while len(timestamps) < 5 and candidate > aligned - SECONDS_PER_HOUR:
            timestamps.append(candidate)
            candidate -= self.interval_sec
        for past_ts in reversed(timestamps):
            self.tick(past_ts)

    async def start(self) -> None:
        """每 interval_sec 写一次真实表；启动回填 + 立即产数，保证看板首屏有值。"""
        self.task = asyncio.current_task()
        self._load_catalogs()
        self._seed_baselines()
        try:
            self._backfill_if_needed()
        except Exception as exc:
            db.log_event("warn", "api", "SIM_BACKFILL_FAIL", f"模拟历史回填失败: {exc}")
        while True:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.log_event("error", "api", "SIMULATE_FAIL", f"模拟产数失败: {exc}")
            await asyncio.sleep(self.interval_sec)

    def shutdown(self) -> None:
        """取消模拟任务（幂等）。"""
        task: asyncio.Task[None] | None = self.task
        self.task = None
        if task is not None and not task.done():
            task.cancel()
