"""小时汇总 / 日汇总（08:00 口径）/ 电费 / purpose 分栋 / 定时结算 / 周月按需聚合。

UTC+8 下：本地 08:00 == UTC 00:00，故 day_start 为 UTC 00:00 时间戳，
hour_start 为 UTC 整点；本地 08:10 == UTC 00:10 自动结算。
电量统一以毫度（1 kWh = 1e6）存 hourly.energy_wh / daily.energy_wh。

v2：新增 week_start()/month_start() 与 Aggregator.weekly()/monthly()——
    从 daily 按 ISO 周（周一为首）/自然月分组，SUM(energy_wh)/SUM(cost_cent)，
    MAX(peak_power)，is_final=区间全部日 final；不落新表，结果与 daily 求和误差 0。
    reading 角色点取 quality IN (0,3)，power 点仍仅 0；settle_day 支持 is_final 参数。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import db
import tariff
from settings import (
    CATCHUP_LOOKBACK_DAYS,
    EVENT_LOG_MAX_ROWS,
    LIVE_ROLLUP_SEC,
    MILLI_PER_KWH,
    RETENTION_BATCH,
    RETENTION_DAYS,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    SECONDS_PER_MINUTE,
    SETTLE_MIN_COVERAGE,
    SETTLE_UTC_MINUTE,
    SIMULATE_ROLLUP_SEC,
    day_floor,
)


def week_start(day_start: int) -> int:
    """返回 day_start 所在 ISO 周的周一 UTC 00:00 时间戳（周一为首）。"""
    day_start = int(day_start)
    weekday: int = datetime.fromtimestamp(day_start, tz=timezone.utc).weekday()
    return day_start - weekday * SECONDS_PER_DAY


def month_start(day_start: int) -> int:
    """返回 day_start 所在自然月的 1 日 UTC 00:00 时间戳。"""
    moment: datetime = datetime.fromtimestamp(int(day_start), tz=timezone.utc)
    first: datetime = moment.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return int(first.timestamp())


class Aggregator:
    """汇总结算服务；所有 DB 调用为同步短事务。"""

    def __init__(self) -> None:
        # simulate 模式由 main.lifespan 按 config.simulate_rollup_sec 覆盖
        self.rollup_interval_sec: int = SIMULATE_ROLLUP_SEC

    def rollup_hour(self, hour_start: int) -> None:
        """汇总单个 UTC 整点：间隔法累计电量 -> energy_wh，power 点求 avg/max。

        间隔法：以 hour_start 时刻（含）之前最后一条有效读数为基准，以 hour_end
        之前最后一条为收口，二者相减即本小时电量。旧实现取窗口内首尾读数，跨小时
        的那个采样间隔不属于任何小时，每小时固定少计一个间隔（60s 轮询约 1.7%，
        300s 约 8.3%）。基准缺失（首个小时/新增点位/换表）时回退窗口首条并留痕。

        reading 点放宽为 quality IN (0,3)，使人工补录的累计读数参与计算；
        power 点仍只取 quality=0，避免人工值污染瞬时功率统计。
        """
        hour_start = int(hour_start)
        hour_end: int = hour_start + SECONDS_PER_HOUR
        meters = db.query("SELECT id FROM meter WHERE is_enabled=1 AND is_counted=1")
        fallback_meters: int = 0

        for meter in meters:
            meter_id: int = int(meter["id"])
            reading_point = db.query_one(
                "SELECT p.id FROM point p JOIN meter_point mp ON mp.point_id=p.id "
                "WHERE mp.meter_id=? AND mp.role='reading' ORDER BY p.id LIMIT 1",
                (meter_id,),
            )
            if reading_point is None:
                continue  # 未配置累计电量点，无法计算小时电量
            point_id: int = int(reading_point["id"])

            # 收口：hour_end 之前最后一条有效读数（本小时必须至少有它，否则无数据）
            end = db.query_one(
                "SELECT value, ts FROM reading WHERE point_id=? AND ts<? "
                "AND quality IN (0,3) ORDER BY ts DESC LIMIT 1",
                (point_id, hour_end),
            )
            if end is None:
                continue  # 该小时无有效读数，不产出汇总行

            # 基准：hour_start 及之前的最后一条；缺失则回退到窗口内首条
            base = db.query_one(
                "SELECT value, ts FROM reading WHERE point_id=? AND ts<=? "
                "AND quality IN (0,3) ORDER BY ts DESC LIMIT 1",
                (point_id, hour_start),
            )
            if base is None:
                base = db.query_one(
                    "SELECT value, ts FROM reading WHERE point_id=? AND ts>=? AND ts<? "
                    "AND quality IN (0,3) ORDER BY ts ASC LIMIT 1",
                    (point_id, hour_start, hour_end),
                )
                fallback_meters += 1
            if base is None:
                continue

            delta_kwh: float = float(end["value"]) - float(base["value"])
            if delta_kwh < 0:
                # 累计值回退（换表/溢出）：本小时记 0 并留审计痕迹
                db.log_event(
                    "warn", "collector", "METER_RESET",
                    f"表 {meter_id} 累计值回退 {delta_kwh:.6f} kWh，按 0 计", meter_id,
                )
                delta_kwh = 0.0
            energy_wh: int = int(round(delta_kwh * MILLI_PER_KWH))

            # 瞬时功率：同表所有 role='power' 的点的平均/最大值（仅 quality=0）
            power_points = db.query(
                "SELECT p.id FROM point p JOIN meter_point mp ON mp.point_id=p.id "
                "WHERE mp.meter_id=? AND mp.role='power'",
                (meter_id,),
            )
            avg_parts: list[float] = []
            max_power: float | None = None
            for power_point in power_points:
                aggreg = db.query_one(
                    "SELECT AVG(value) AS avg_v, MAX(value) AS max_v, COUNT(*) AS cnt "
                    "FROM reading WHERE point_id=? AND ts>=? AND ts<? AND quality=0",
                    (int(power_point["id"]), hour_start, hour_end),
                )
                if aggreg is not None and int(aggreg["cnt"]) > 0:
                    avg_parts.append(float(aggreg["avg_v"]))
                    max_v: Any = aggreg["max_v"]
                    if max_v is not None:
                        max_value = float(max_v)
                        if max_power is None or max_value > max_power:
                            max_power = max_value
            avg_power: float | None = sum(avg_parts) / len(avg_parts) if avg_parts else None

            sample_row = db.query_one(
                "SELECT COUNT(*) AS cnt FROM reading WHERE point_id=? AND ts>=? "
                "AND ts<? AND quality IN (0,3)",
                (point_id, hour_start, hour_end),
            )
            sample_cnt: int = int(sample_row["cnt"]) if sample_row is not None else 0

            db.execute(
                "INSERT OR REPLACE INTO hourly "
                "(meter_id, hour_start, energy_wh, avg_power, max_power, sample_cnt) "
                "VALUES (?,?,?,?,?,?)",
                (meter_id, hour_start, energy_wh, avg_power, max_power, sample_cnt),
            )

        if fallback_meters:
            db.log_event(
                "warn", "collector", "ROLLUP_NO_BASE",
                f"hour_start={hour_start} 有 {fallback_meters} 块表缺基准读数，"
                "已回退窗口内首条（该小时电量可能偏低）",
            )
        db.log_event("info", "collector", "ROLLUP_HOUR_DONE", f"小时汇总完成 hour_start={hour_start}")

    def settle_day(
        self, day_start: int, is_final: int = 1, enforce_coverage: bool = True
    ) -> None:
        """把某日 24 个 UTC 小时汇总成 daily 并计电费；is_final 可指定结算状态。

        定稿门槛：只有「有数据的小时数 ≥ 24×90%」且「有数据的表数 ≥ 应结算表数×90%」
        才允许写 is_final=1，否则保留 is_final=0 并记 SETTLE_INCOMPLETE 事件。
        避免「一天只有 1 小时数据」「100 块表只到 3 块」也被当作最终数用于对账。

        模拟模式（is_final=0）与人工回填（enforce_coverage=False）不受门槛限制：
        前者本就只是当天实时视图，后者是操作员对历史数据的明确修正。
        """
        day_start = int(day_start)
        final_flag: int = 1 if int(is_final) else 0
        day_end: int = day_start + SECONDS_PER_DAY
        rows = db.query(
            "SELECT meter_id, hour_start, energy_wh, avg_power, max_power, sample_cnt "
            "FROM hourly WHERE hour_start>=? AND hour_start<?",
            (day_start, day_end),
        )
        buckets: dict[int, dict[str, Any]] = {}
        hours_covered: set[int] = set()
        for row in rows:
            hours_covered.add(int(row["hour_start"]))
            bucket: dict[str, Any] = buckets.setdefault(
                int(row["meter_id"]),
                {
                    "energy_wh": 0, "weighted_sum": 0.0, "samples": 0,
                    "peak": None, "hours": [],
                },
            )
            bucket["energy_wh"] += int(row["energy_wh"])
            bucket["hours"].append((int(row["hour_start"]), int(row["energy_wh"])))
            samples: int = int(row["sample_cnt"] or 0)
            if row["avg_power"] is not None:
                bucket["weighted_sum"] += float(row["avg_power"]) * samples
                bucket["samples"] += samples
            if row["max_power"] is not None:
                peak: float = float(row["max_power"])
                if bucket["peak"] is None or peak > bucket["peak"]:
                    bucket["peak"] = peak

        # 未来日期不允许定稿：否则手动传一个未来 day_start 就能凭空产生"已结算"数据
        if final_flag and day_start >= day_floor(time.time()):
            final_flag = 0
            db.log_event(
                "warn", "collector", "SETTLE_FUTURE",
                f"day_start={day_start} 尚未结束，按未结算保留",
            )

        if final_flag and enforce_coverage and buckets:
            counted = db.query_one(
                "SELECT COUNT(*) AS cnt FROM meter WHERE is_enabled=1 AND is_counted=1"
            )
            total_meters: int = int(counted["cnt"]) if counted is not None else 0
            hour_ratio: float = len(hours_covered) / 24.0
            meter_ratio: float = len(buckets) / total_meters if total_meters else 0.0
            if hour_ratio < SETTLE_MIN_COVERAGE or meter_ratio < SETTLE_MIN_COVERAGE:
                final_flag = 0
                db.log_event(
                    "warn", "collector", "SETTLE_INCOMPLETE",
                    f"day_start={day_start} 数据不完整，按未结算保留："
                    f"小时覆盖 {len(hours_covered)}/24，表覆盖 {len(buckets)}/{total_meters}",
                )

        for meter_id, bucket in buckets.items():
            energy_wh: int = int(bucket["energy_wh"])
            avg_power: float | None = (
                bucket["weighted_sum"] / bucket["samples"] if bucket["samples"] > 0 else None
            )
            # 分时电价：按当天各小时电量逐段计价，并把「按什么价算的」存成快照
            cost: tariff.CostResult = tariff.cost_for_hourly(bucket["hours"])
            self._warn_if_reprice(meter_id, day_start, cost)
            db.execute(
                "INSERT OR REPLACE INTO daily "
                "(meter_id, day_start, energy_wh, peak_power, avg_power, cost_cent, "
                "is_final, price_cent, tariff_snapshot) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    meter_id, day_start, energy_wh, bucket["peak"], avg_power,
                    cost.cost_cent, final_flag, cost.price_cent,
                    tariff.dumps(cost, len(bucket["hours"])),
                ),
            )

        db.log_event(
            "info", "collector", "SETTLE_DONE",
            f"日结算完成 day_start={day_start}，is_final={final_flag}，结算表数={len(buckets)}",
        )

    @staticmethod
    def _warn_if_reprice(meter_id: int, day_start: int, cost: tariff.CostResult) -> None:
        """已定稿日按新电价改写电费时留痕，避免「历史电费悄悄变了」无从追溯。"""
        previous = db.query_one(
            "SELECT cost_cent, price_cent, is_final FROM daily WHERE meter_id=? AND day_start=?",
            (meter_id, day_start),
        )
        if previous is None or int(previous["is_final"] or 0) != 1:
            return
        old_price: Any = previous["price_cent"]
        if old_price is None or cost.price_cent is None:
            return
        if abs(float(old_price) - float(cost.price_cent)) < 1e-6:
            return
        db.log_event(
            "warn", "collector", "TARIFF_REPRICE",
            f"表 {meter_id} day_start={day_start} 单价由 {float(old_price):.4f} "
            f"变为 {cost.price_cent:.4f} 分/kWh，历史电费已重算", meter_id,
        )

    def calc_cost(
        self, energy_wh: int, day_start: int, hourly: list[tuple[int, int]] | None = None
    ) -> int:
        """电费（分）：有小时明细时按分时电价逐段计费，否则回退单一电价。

        day_start 仅为兼容旧调用保留；分时时段由 hourly 里的 hour_start 决定。
        """
        if hourly:
            return tariff.cost_for_hourly(hourly).cost_cent
        return tariff.flat_cost(energy_wh).cost_cent

    def split_by_purpose(self, day_start: int) -> list[dict[str, Any]]:
        """按 purpose + node 分栋拆分某日汇总（供看板/API 使用）。"""
        rows = db.query(
            "SELECT m.purpose AS purpose, m.node_id AS node_id, "
            "SUM(d.energy_wh) AS energy_wh, SUM(d.cost_cent) AS cost_cent "
            "FROM daily d JOIN meter m ON m.id=d.meter_id "
            "WHERE d.day_start=? GROUP BY m.purpose, m.node_id "
            "ORDER BY m.purpose, m.node_id",
            (int(day_start),),
        )
        return [
            {
                "purpose": row["purpose"],
                "node_id": row["node_id"],
                "energy_wh": int(row["energy_wh"] or 0),
                "cost_cent": int(row["cost_cent"] or 0),
            }
            for row in rows
        ]

    # ---------- 周 / 月按需聚合（不落表） ----------
    def _period_aggregate(
        self,
        bucket: str,
        meter_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        purpose: str | None = None,
        node_id: int | None = None,
        is_final: int | None = None,
    ) -> list[dict[str, Any]]:
        """从 daily 按 ISO 周/自然月分组聚合，结果与区间 daily 求和误差 0。"""
        conditions: list[str] = []
        params: list[Any] = []
        if meter_id is not None:
            conditions.append("d.meter_id=?")
            params.append(int(meter_id))
        if from_ts is not None:
            conditions.append("d.day_start>=?")
            params.append(int(from_ts))
        if to_ts is not None:
            conditions.append("d.day_start<=?")
            params.append(int(to_ts))
        if purpose:
            conditions.append("m.purpose=?")
            params.append(purpose)
        if node_id is not None:
            conditions.append("m.node_id=?")
            params.append(int(node_id))
        if is_final is not None:
            conditions.append("d.is_final=?")
            params.append(int(is_final))
        sql: str = (
            "SELECT d.day_start, d.meter_id, d.energy_wh, d.cost_cent, d.peak_power, "
            "d.avg_power, d.is_final, m.name AS meter_name, m.purpose, m.node_id, "
            "n.name AS node_name FROM daily d JOIN meter m ON m.id=d.meter_id "
            "LEFT JOIN node n ON n.id=m.node_id"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY d.day_start, d.meter_id"
        rows = db.query(sql, tuple(params))

        grouped: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            day_start: int = int(row["day_start"])
            period: int = week_start(day_start) if bucket == "week" else month_start(day_start)
            key: tuple[int, int] = (period, int(row["meter_id"]))
            item: dict[str, Any] = grouped.setdefault(
                key,
                {
                    "period_start": period,
                    "meter_id": int(row["meter_id"]),
                    "meter_name": row["meter_name"],
                    "purpose": row["purpose"],
                    "node_id": row["node_id"],
                    "node_name": row["node_name"],
                    "energy_wh": 0,
                    "cost_cent": 0,
                    "peak_power": None,
                    "avg_power_sum": 0.0,
                    "days": 0,
                    "final_days": 0,
                },
            )
            item["energy_wh"] += int(row["energy_wh"] or 0)
            item["cost_cent"] += int(row["cost_cent"] or 0)
            peak: Any = row["peak_power"]
            if peak is not None:
                peak_value: float = float(peak)
                if item["peak_power"] is None or peak_value > item["peak_power"]:
                    item["peak_power"] = peak_value
            if row["avg_power"] is not None:
                item["avg_power_sum"] += float(row["avg_power"])
            item["days"] += 1
            if int(row["is_final"] or 0) == 1:
                item["final_days"] += 1

        result: list[dict[str, Any]] = []
        for (period, _meter_id), item in sorted(grouped.items()):
            result.append(
                {
                    "period_start": period,
                    "meter_id": item["meter_id"],
                    "meter_name": item["meter_name"],
                    "purpose": item["purpose"],
                    "node_id": item["node_id"],
                    "node_name": item["node_name"],
                    "energy_wh": int(item["energy_wh"]),
                    "cost_cent": int(item["cost_cent"]),
                    "peak_power": item["peak_power"],
                    "avg_power": (
                        item["avg_power_sum"] / item["days"] if item["days"] else None
                    ),
                    "is_final": 1 if item["final_days"] == item["days"] else 0,
                    "days": int(item["days"]),
                }
            )
        return result

    def weekly(
        self,
        meter_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        purpose: str | None = None,
        node_id: int | None = None,
        is_final: int | None = None,
    ) -> list[dict[str, Any]]:
        """ISO 周汇总（周一 UTC 00:00 为首），从 daily 按需聚合。"""
        return self._period_aggregate(
            "week", meter_id, from_ts, to_ts, purpose, node_id, is_final
        )

    def monthly(
        self,
        meter_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        purpose: str | None = None,
        node_id: int | None = None,
        is_final: int | None = None,
    ) -> list[dict[str, Any]]:
        """自然月汇总（1 日 UTC 00:00 为首），从 daily 按需聚合。"""
        return self._period_aggregate(
            "month", meter_id, from_ts, to_ts, purpose, node_id, is_final
        )

    def _catch_up(self) -> None:
        """补算遗漏日：对近 30 天有 hourly 数据但无 final daily 的日子补结算。"""
        today_start: int = day_floor(time.time())
        cutoff: int = today_start - CATCHUP_LOOKBACK_DAYS * SECONDS_PER_DAY
        rows = db.query(
            "SELECT DISTINCT hour_start FROM hourly WHERE hour_start>=? AND hour_start<? "
            "ORDER BY hour_start",
            (cutoff, today_start),
        )
        days: set[int] = {day_floor(int(row["hour_start"])) for row in rows}
        for day_start in sorted(days):
            final_row = db.query_one(
                "SELECT 1 FROM daily WHERE day_start=? AND is_final=1 LIMIT 1", (day_start,)
            )
            if final_row is None:
                self.settle_day(day_start)

    @staticmethod
    def _seconds_to_settle() -> float:
        """距离下一个「日切点 + 10 分钟」的秒数。"""
        now: float = time.time()
        today_start: int = day_floor(now)
        target: int = today_start + SETTLE_UTC_MINUTE * SECONDS_PER_MINUTE
        if now >= target:
            target += SECONDS_PER_DAY
        return max(1.0, target - now)

    async def run_loop(self) -> None:
        """启动即补算遗漏日，此后每天 UTC 00:10 结算昨日、兜底补算并清理超期读数。"""
        try:
            await asyncio.to_thread(self._catch_up)
        except Exception as exc:  # 启动补算失败不能阻断后台任务
            db.log_event("error", "collector", "CATCHUP_FAIL", f"补算遗漏日失败: {exc}")

        while True:
            await asyncio.sleep(self._seconds_to_settle())
            try:
                yesterday: int = day_floor(time.time()) - SECONDS_PER_DAY
                # 结算涉及上百次查询与写入，放线程执行，避免阻塞事件循环里的 HTTP 请求
                await asyncio.to_thread(self.settle_day, yesterday)
                await asyncio.to_thread(self._catch_up)
                await asyncio.to_thread(self.purge_old_readings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.log_event("error", "collector", "SETTLE_FAIL", f"自动结算失败: {exc}")

    def rollup_recent_hours(self, now: int | None = None) -> int:
        """收口上一小时并滚动更新当前小时，返回本次汇总的小时数。

        单独抽成同步方法，既供 run_hourly_loop 放线程执行，也便于用例直接验证
        「读数 -> 小时表」这一步（不必等待 5 分钟轮询）。
        """
        moment: int = int(now if now is not None else time.time())
        hour_start: int = (moment // SECONDS_PER_HOUR) * SECONDS_PER_HOUR
        self.rollup_hour(hour_start - SECONDS_PER_HOUR)
        self.rollup_hour(hour_start)
        return 2

    async def run_hourly_loop(self, interval_sec: int = LIVE_ROLLUP_SEC) -> None:
        """真实采集模式的小时汇总：每 interval_sec 收口上一小时并滚动更新当前小时。

        live 模式此前只起采集器与日结算循环，hourly 没有任何写入者，
        导致日/周/月汇总与报表全部为空；此循环补齐该环节。
        """
        while True:
            try:
                await asyncio.to_thread(self.rollup_recent_hours)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.log_event(
                    "error", "collector", "HOURLY_ROLLUP_FAIL", f"小时汇总失败: {exc}"
                )
            await asyncio.sleep(max(60, int(interval_sec)))

    def purge_old_readings(self, now: int | None = None) -> int:
        """删除超期原始读数并裁剪事件日志，返回删除的读数行数。

        原始读数按 RETENTION_DAYS 保留、按 RETENTION_BATCH 分批删除（每批独立提交），
        避免一次性大事务长时间锁库；事件日志按 EVENT_LOG_MAX_ROWS 保留最新若干条。
        """
        base: int = int(now if now is not None else time.time())
        cutoff: int = day_floor(base) - RETENTION_DAYS * SECONDS_PER_DAY
        removed: int = 0
        while True:
            with db.transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM reading WHERE (point_id, ts) IN "
                    "(SELECT point_id, ts FROM reading WHERE ts<? LIMIT ?)",
                    (cutoff, RETENTION_BATCH),
                )
                batch: int = int(cursor.rowcount or 0)
            removed += batch
            if batch < RETENTION_BATCH:
                break
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM event_log WHERE id IN "
                "(SELECT id FROM event_log ORDER BY id DESC LIMIT -1 OFFSET ?)",
                (EVENT_LOG_MAX_ROWS,),
            )
        if removed:
            db.log_event(
                "info", "collector", "RETENTION_PURGE",
                f"清理 {RETENTION_DAYS} 天前原始读数 {removed} 行",
            )
        return removed

    async def run_simulate_loop(self) -> None:
        """模拟模式专用：每 rollup_interval_sec 汇总当前小时 + 当天非 final 日汇总。"""
        while True:
            try:
                now: int = int(time.time())
                hour_start: int = (now // SECONDS_PER_HOUR) * SECONDS_PER_HOUR
                day_start: int = day_floor(now)
                # 启动回填可能跨小时，前一小时也汇总一次保证趋势连续
                await asyncio.to_thread(self.rollup_hour, hour_start - SECONDS_PER_HOUR)
                await asyncio.to_thread(self.rollup_hour, hour_start)
                await asyncio.to_thread(self.settle_day, day_start, 0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.log_event("error", "api", "SIM_ROLLUP_FAIL", f"模拟汇总失败: {exc}")
            await asyncio.sleep(max(1, int(self.rollup_interval_sec)))


aggregator: Aggregator = Aggregator()
