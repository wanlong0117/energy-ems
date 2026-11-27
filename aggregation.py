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
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import db
import settings as settings_module
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
    SHUTDOWN_GAP_MAX_HOURS,
    SIMULATE_ROLLUP_SEC,
    day_floor,
)


def _rollover_max(per_meter: Any) -> float:
    """单表累计电量量程（kWh）：NULL/非法值一律视为「不修正」。

    只有正有限值才启用翻转判定。填错量程（比如把 10000 写成 100000）会让一次
    正常的换表回退被算成「翻了半圈」，产出几十万度的假电量，因此宁可保守：
    值不合理就退回「按 0 计 + 留痕」，让人在事件日志里看到原始读数。
    """
    if per_meter is None:
        return 0.0
    try:
        value: float = float(per_meter)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return value


def _shutdown_threshold(per_meter: Any) -> float:
    """单表停机阈值（W）：表上填了就用表上的，否则回退全局 EMS_SHUTDOWN_POWER_W。

    返回 0 表示**关闭判定**。负数、inf、nan 一律当关闭——这些值只会来自手误，
    若放行会让「功率 < 阈值」恒成立，把整天判成停机、覆盖率分母归零。
    """
    value: Any = per_meter
    if value is None:
        value = getattr(settings_module, "SHUTDOWN_POWER_W", 0.0)
    try:
        threshold: float = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(threshold) or threshold <= 0:
        return 0.0
    return threshold


def _assumed_shutdown_hours(observed: set[int], shutdown: set[int], max_gap: int) -> int:
    """把「夹在两段已观测停机之间、长度不超过 max_gap」的缺口小时也算作停机。

    为什么需要它（v0.1.6 修正）：覆盖率门槛的分子与分母必须同源。分母剔除停机
    小时后，分子若仍把停机小时算成「有生产数据的小时」，比值会被凭空放大——实测
    「12 小时待机有数 + 12 小时生产时段全缺」会算出 12/12=1.0 而定稿，等于给一次
    采集中断发了「已结算」证书；停机占比过半的表上，门槛干脆完全失效。

    但只做严格同源会走向另一头：现场待机时段本来可能没有数据（设备随产线断电），
    把这些缺口按生产小时计，会让「0 度停产日」永远定不了稿，正是本次要解决的问题。
    折中办法是只用**已有证据**推断：缺口左右两侧最近的有数据小时都判定为停机，
    且缺口连续长度不超过 max_gap 时才一并当停机。日界一侧的缺口不推断
    （凌晨/收班前后正是可能开工的时段），长缺口也不推断（那是采集中断，
    半天的空白不该由系统替人签字）。

    observed / shutdown 都是当日 0~23 的小时序号集合，shutdown ⊆ observed。
    """
    if not shutdown or max_gap <= 0:
        return 0
    missing: list[int] = [hour for hour in range(24) if hour not in observed]
    assumed: int = 0
    index: int = 0
    while index < len(missing):
        end: int = index
        while end + 1 < len(missing) and missing[end + 1] == missing[end] + 1:
            end += 1
        run: int = end - index + 1
        left_ok: bool = (missing[index] - 1) in shutdown
        right_ok: bool = (missing[end] + 1) in shutdown
        if run <= max_gap and left_ok and right_ok:
            assumed += run
        index = end + 1
    return assumed


def _tz_shift() -> int:
    """本地时区偏移的秒数（运行期读取，便于按环境变量切换口径与用例覆盖）。"""
    return int(settings_module.TZ_OFFSET_HOURS) * SECONDS_PER_HOUR


def week_start(day_start: int) -> int:
    """返回 day_start 所在 ISO 周的周一，且与日切点对齐（周一为首）。

    先换算到本地日历再取周一，最后换回 UTC；日切点可配后仍然成立——
    默认（UTC+8 / 08:00）下结果与「直接减 weekday 天」完全一致。
    """
    day_start = day_floor(day_start)
    shift: int = _tz_shift()
    local: datetime = datetime.fromtimestamp(day_start + shift, tz=timezone.utc)
    monday: datetime = local - timedelta(days=local.weekday())
    return int(monday.timestamp()) - shift


def month_start(day_start: int) -> int:
    """返回 day_start 所在自然月 1 日，且与日切点对齐（保留本地日切小时）。"""
    day_start = day_floor(day_start)
    shift: int = _tz_shift()
    local: datetime = datetime.fromtimestamp(day_start + shift, tz=timezone.utc)
    first: datetime = local.replace(day=1, second=0, microsecond=0)
    return int(first.timestamp()) - shift


class Aggregator:
    """汇总结算服务；所有 DB 调用为同步短事务。"""

    def __init__(self) -> None:
        # simulate 模式由 main.lifespan 按 config.simulate_rollup_sec 覆盖
        self.rollup_interval_sec: int = SIMULATE_ROLLUP_SEC
        # 历史重算等批量场景置 True：跳过每小时的 ROLLUP_HOUR_DONE，
        # 否则重算 30 天会往 2 万条上限的事件表里灌 700 多条流水账
        self.silent: bool = False

    def rollup_hour(self, hour_start: int) -> None:
        """汇总单个 UTC 整点：间隔法累计电量 -> energy_wh，power 点求 avg/max。

        窗口口径为**左开右闭** `(hour_start, hour_end]`，与相邻小时首尾相接、
        无重叠也无遗漏：

          base = ts <= hour_start 的最后一条（上一小时的收口）
          end  = hour_start < ts <= hour_end 的最后一条（本小时的收口）

        旧实现是 `[hour_start, hour_end)`：正好落在整点的那条读数只被下一小时
        当成基准，本小时收口看不到它，于是跨整点的一个采样间隔两边都不算，
        每小时固定少计（300s 轮询实测少 8.3%）。改成右闭后逐小时精确闭合。

        本小时窗口内没有任何有效读数（设备掉线/点位新增）时不产出行——
        旧实现会退化成「取几小时前的旧值相减 = 0」，把「没采到」记成「没用电」
        并且照样参与定稿；不产出行后，覆盖率门槛才能真实拦住这种日子。

        基准缺失（首个小时/新增点位/换表）时回退窗口首条并留痕。

        reading 点放宽为 quality IN (0,3)，使人工补录的累计读数参与计算；
        power 点仍只取 quality=0，避免人工值污染瞬时功率统计。
        """
        hour_start = int(hour_start)
        hour_end: int = hour_start + SECONDS_PER_HOUR
        # 演示表只在「纯演示实例」里参与汇总；接入真表后一律让位（P0）
        meters = db.query(
            "SELECT id FROM meter WHERE is_enabled=1 AND is_counted=1" + db.demo_guard("meter")
        )
        fallback_meters: int = 0

        for meter in meters:
            meter_id: int = int(meter["id"])
            try:
                if self._rollup_one_meter(meter_id, hour_start, hour_end):
                    fallback_meters += 1
            except Exception as exc:
                # 单表异常（脏数据/整数溢出/字段缺失）只影响该表：
                # 旧实现让异常冒到循环外，故障表之后的所有表整轮停止更新，
                # 而小时任务每 5 分钟重试一次，等于每 5 分钟重复失败一次。
                db.log_event_throttled(
                    "error", "collector", "METER_ROLLUP_FAIL",
                    f"表 {meter_id} 小时汇总失败，本轮跳过：{type(exc).__name__}: {exc}",
                    meter_id,
                )
                continue

        if fallback_meters:
            db.log_event(
                "warn", "collector", "ROLLUP_NO_BASE",
                f"hour_start={hour_start} 有 {fallback_meters} 块表缺基准读数，"
                "已回退窗口内首条（该小时电量可能偏低）",
            )
        if not self.silent:
            db.log_event(
                "info", "collector", "ROLLUP_HOUR_DONE", f"小时汇总完成 hour_start={hour_start}"
            )

    def _resolve_backward_reading(
        self,
        meter_id: int,
        point_id: int,
        base: dict[str, Any],
        end: dict[str, Any],
        hour_start: int,
        hour_end: int,
        delta_kwh: float,
    ) -> float:
        """累计读数回退时的三种处置：满量程翻转 / 换表 / 无法判定。

        「累计值回退」只有三种成因，旧实现一律按 0 计，代价是那一小时的电量直接
        丢掉且日报照常定稿（偏小无声）。现在按证据分派：

        1. **满量程翻转**：表配了 `max_reading_kwh` 且两端读数都落在 [0, 量程] 内，
           按 `(量程 − 旧值) + 新值` 补回真实用量。机械表走到 99999.9 归零是硬件
           行为，不是数据异常，丢掉这一小时等于每月白送一段电量。
        2. **换表**：旧表被拆、新表底数更小。旧表在换表那一刻的读数不可知，但
           「本小时窗口内新表第一条读数 → 收口读数」是可测的，用它当基准，本小时
           记新表这一段的用量，而不是记 0。
        3. **无法判定**：在窗口里找不到可用的新基准，或算出来仍是负数。维持旧行为
           记 0，但事件里带上旧/新两个原始读数，方便现场核对底数。

        返回值恒 ≥ 0：任何分支算出的负数都退化成 0，绝不把负电量写进 hourly。
        """
        old_reading: float = float(base["value"])
        new_reading: float = float(end["value"])

        meter = db.query_one("SELECT max_reading_kwh FROM meter WHERE id=?", (meter_id,))
        rollover_max: float = _rollover_max(meter["max_reading_kwh"] if meter else None)
        if rollover_max > 0 and 0 <= old_reading <= rollover_max and 0 <= new_reading <= rollover_max:
            wrapped: float = (rollover_max - old_reading) + new_reading
            if 0 <= wrapped < rollover_max:
                db.log_event(
                    "warn", "collector", "METER_ROLLOVER",
                    f"表 {meter_id} 满量程翻转 {old_reading:.3f} → {new_reading:.3f}"
                    f"（量程 {rollover_max:g} kWh），本小时按 {wrapped:.6f} kWh 计",
                    meter_id,
                )
                return wrapped

        # 换表：用窗口内新表首条读数当基准。要求它严格早于收口读数、且不大于它，
        # 否则说明读数在一小时内还在往下掉（异常/回零中），交给下面的「无法判定」。
        first_in_window = db.query_one(
            "SELECT value, ts FROM reading WHERE point_id=? AND ts>? AND ts<=? "
            "AND quality IN (0,3) ORDER BY ts ASC LIMIT 1",
            (point_id, hour_start, hour_end),
        )
        if (
            first_in_window is not None
            and int(first_in_window["ts"]) < int(end["ts"])
            and int(first_in_window["ts"]) > int(base["ts"])
        ):
            first_value: float = float(first_in_window["value"])
            swapped: float = new_reading - first_value
            # 有量程时用它兜底：超过一个量程的「换表增量」只能是脏数据
            sane_upper: float = rollover_max if rollover_max > 0 else math.inf
            if 0 <= swapped < sane_upper:
                db.log_event(
                    "warn", "collector", "METER_SWAP",
                    f"表 {meter_id} 累计值回退，疑似换表：旧表基准 {old_reading:.3f}，"
                    f"新表首条 {first_value:.3f} → 收口 {new_reading:.3f}，"
                    f"本小时按新表 {swapped:.6f} kWh 计（旧表末段不可知，未计入）",
                    meter_id,
                )
                return swapped

        db.log_event(
            "warn", "collector", "METER_RESET",
            f"表 {meter_id} 累计值回退 {delta_kwh:.6f} kWh（{old_reading:.3f} → "
            f"{new_reading:.3f}），无法判定翻转或换表，本小时按 0 计；"
            "若为满量程翻转请在表上填 max_reading_kwh，若为换表请核对新表底数",
            meter_id,
        )
        return 0.0

    def _rollup_one_meter(self, meter_id: int, hour_start: int, hour_end: int) -> bool:
        """单表小时汇总：返回是否回退了「窗口内首条」作为基准。

        拆成独立方法是为了异常隔离：某块表出现脏数据（超大整数、字段缺失）时
        只跳过它，不会让排在它后面的所有表整轮停止更新。
        """
        reading_point = db.query_one(
            "SELECT p.id FROM point p JOIN meter_point mp ON mp.point_id=p.id "
            "WHERE mp.meter_id=? AND mp.role='reading' ORDER BY p.id LIMIT 1",
            (meter_id,),
        )
        if reading_point is None:
            return False  # 未配置累计电量点，无法计算小时电量
        point_id: int = int(reading_point["id"])
        used_fallback: bool = False

        # 收口：本小时窗口内最后一条；窗口为空即本小时无样本，不产出行
        end = db.query_one(
            "SELECT value, ts FROM reading WHERE point_id=? AND ts>? AND ts<=? "
            "AND quality IN (0,3) ORDER BY ts DESC LIMIT 1",
            (point_id, hour_start, hour_end),
        )
        if end is None:
            return False

        # 基准：hour_start 及之前的最后一条；缺失则回退到窗口内首条（首个小时/换表）
        base = db.query_one(
            "SELECT value, ts FROM reading WHERE point_id=? AND ts<=? "
            "AND quality IN (0,3) ORDER BY ts DESC LIMIT 1",
            (point_id, hour_start),
        )
        if base is None:
            base = db.query_one(
                "SELECT value, ts FROM reading WHERE point_id=? AND ts>? AND ts<=? "
                "AND quality IN (0,3) ORDER BY ts ASC LIMIT 1",
                (point_id, hour_start, hour_end),
            )
            used_fallback = True
        if base is None:
            return False

        delta_kwh: float = float(end["value"]) - float(base["value"])
        if delta_kwh < 0:
            delta_kwh = self._resolve_backward_reading(
                meter_id, point_id, base, end, hour_start, hour_end, delta_kwh
            )
        energy_wh: int = int(round(delta_kwh * MILLI_PER_KWH))

        # 瞬时功率：同表所有 role='power' 的点的平均/最大值（仅 quality=0）。
        # 功率用 [hour_start, hour_end) 左闭右开——它是「本小时内的采样」，
        # 与电量的右闭窗口不同源：累计读数 v(hour_end) 是上一段区间的收口，
        # 而瞬时功率 p(hour_end) 属于下一小时的第一个采样点。
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
        return used_fallback

    def settle_day(
        self, day_start: int, is_final: int = 1, enforce_coverage: bool = True
    ) -> None:
        """把某日 24 个 UTC 小时汇总成 daily 并计电费；is_final 可指定结算状态。

        定稿门槛分三层：
          · 表覆盖——有数据的表数须 ≥ 应结算表数×90%，否则整日都不定时稿；
          · 单表小时覆盖——该表当日「有生产数据的小时数」须 ≥ 应覆盖小时数×90%，
            不足则该表单独保留 is_final=0。旧实现用「所有表的小时并集」判定，
            拦不住「99 块表满 24 小时、1 块只有 1 小时」也被一并定稿的情况；
          · 应覆盖小时数 = 24 − 停机小时（含缺口推断），分子是「有数据且非停机」
            的小时数。两者同源是关键：分母剔了停机小时、分子却没剔，比值会被
            凭空放大（实测 12 小时待机 + 12 小时生产全缺 = 12/12 也能定稿）。
            夜间停产、周末停线这类小时本就不该有生产电量，把它们算进分母等于
            要求「停产也得报数」，0 度停产日就永远定不了稿；但「没采到数」也
            不能自己判成停机，否则门槛形同虚设——折中见 _assumed_shutdown_hours。

        模拟模式（is_final=0）与人工回填（enforce_coverage=False）不受门槛限制：
        前者本就只是当天实时视图，后者是操作员对历史数据的明确修正。
        """
        day_start = int(day_start)
        final_flag: int = 1 if int(is_final) else 0
        day_end: int = day_start + SECONDS_PER_DAY
        # 应结算表集合：启用 + 计入，且接入真表后排除演示表（P0）。
        # 覆盖率分母必须与「谁应该报数」一致，否则演示数据会把分母撑爆。
        target_rows = db.query(
            "SELECT id, shutdown_power_w FROM meter WHERE is_enabled=1 AND is_counted=1"
            + db.demo_guard("meter")
        )
        target_ids: list[int] = [int(row["id"]) for row in target_rows]
        target_set: set[int] = set(target_ids)
        # 停机阈值逐表解析：同一厂区里注塑机待机 3 kW、照明回路待机 20 W，
        # 一个全局阈值不可能同时正确，所以表级配置优先、全局只当兜底。
        thresholds: dict[int, float] = {
            int(row["id"]): _shutdown_threshold(row["shutdown_power_w"])
            for row in target_rows
        }
        rows = db.query(
            "SELECT meter_id, hour_start, energy_wh, avg_power, max_power, sample_cnt "
            "FROM hourly WHERE hour_start>=? AND hour_start<?",
            (day_start, day_end),
        )
        buckets: dict[int, dict[str, Any]] = {}
        hours_covered: set[int] = set()
        for row in rows:
            if target_set and int(row["meter_id"]) not in target_set:
                continue  # 已停用/演示表的残留小时数据不参与结算
            hours_covered.add(int(row["hour_start"]))
            bucket: dict[str, Any] = buckets.setdefault(
                int(row["meter_id"]),
                {
                    "energy_wh": 0, "weighted_sum": 0.0, "samples": 0,
                    "peak": None, "hours": [], "power_by_hour": {},
                },
            )
            bucket["energy_wh"] += int(row["energy_wh"])
            bucket["hours"].append((int(row["hour_start"]), int(row["energy_wh"])))
            bucket["power_by_hour"][int(row["hour_start"])] = (
                None if row["avg_power"] is None else float(row["avg_power"])
            )
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

        day_final: int = final_flag
        if final_flag and enforce_coverage:
            total_meters: int = len(target_ids)
            meter_ratio: float = len(buckets) / total_meters if total_meters else 0.0
            if not buckets or meter_ratio < SETTLE_MIN_COVERAGE:
                day_final = 0
                db.log_event(
                    "warn", "collector", "SETTLE_INCOMPLETE",
                    f"day_start={day_start} 数据不完整，按未结算保留："
                    f"表覆盖 {len(buckets)}/{total_meters}，小时并集 {len(hours_covered)}/24",
                )

        short_meters: list[str] = []
        settled_meters: int = 0
        shutdown_meters: list[str] = []
        # 正式结算时，应结算但整日无数据的表也要落一行零值（P2-4）：
        # 旧行为是「没数据就不产行」，报表按行求和会静默少计，而页面仍显示「已结算」。
        # 补零行 + covered_hours=0 + is_final=0，缺哪块表一眼能看出来。
        meter_ids: list[int] = list(buckets.keys())
        if enforce_coverage:
            meter_ids += [mid for mid in target_ids if mid not in buckets]
        for meter_id in meter_ids:
            bucket: dict[str, Any] | None = buckets.get(meter_id)
            hours: list[tuple[int, int]] = bucket["hours"] if bucket else []
            energy_wh: int = int(bucket["energy_wh"]) if bucket else 0
            avg_power: float | None = (
                bucket["weighted_sum"] / bucket["samples"]
                if bucket and bucket["samples"] > 0 else None
            )
            # 分时电价：按当天各小时电量逐段计价，并把「按什么价算的」存成快照
            cost: tariff.CostResult = tariff.cost_for_hourly(hours)
            if bucket:
                self._warn_if_reprice(meter_id, day_start, cost)
            covered_hours: int = len(hours)
            # 停机小时：该小时功率点均值低于阈值 → 现场没在生产，不该要求「有数据」。
            # 只认有功率读数的小时：没有功率点、或该小时没采到功率，一律不当停机，
            # 否则「采不到数据」会被自己判成「停机」，覆盖率门槛就形同虚设。
            threshold: float = thresholds.get(meter_id, 0.0)
            power_by_hour: dict[int, float | None] = bucket["power_by_hour"] if bucket else {}
            observed: set[int] = {
                (hour_start - day_start) // SECONDS_PER_HOUR for hour_start, _ in hours
            }
            shutdown_idx: set[int] = set()
            if threshold > 0:
                shutdown_idx = {
                    (hour_start - day_start) // SECONDS_PER_HOUR
                    for hour_start, power in power_by_hour.items()
                    if power is not None and power < threshold
                }
            assumed: int = _assumed_shutdown_hours(
                observed, shutdown_idx, SHUTDOWN_GAP_MAX_HOURS
            )
            shutdown_observed: int = len(shutdown_idx)
            shutdown_hours: int = shutdown_observed + assumed
            if shutdown_hours:
                shutdown_meters.append(
                    f"{meter_id}({shutdown_hours}h"
                    + (f",含缺口{assumed}" if assumed else "")
                    + ")"
                )
            # 覆盖率门槛：分子与分母同源。分母是「应有生产小时」，分子是
            # 「有数据且非停机的小时」——停机小时两边都不算。
            # 整日停机（含缺口推断）时没有应有生产小时，直接放行：
            # 这正是「全天停产、当天 0 度」能定稿的来源。
            expected_hours: int = 24 - shutdown_hours
            meter_final: int = day_final
            if meter_final and enforce_coverage and expected_hours > 0:
                production_covered: int = max(covered_hours - shutdown_observed, 0)
                if production_covered / expected_hours < SETTLE_MIN_COVERAGE:
                    meter_final = 0
                    short_meters.append(
                        f"{meter_id}({production_covered}/{expected_hours}"
                        + (f",停机{shutdown_observed}" if shutdown_observed else "")
                        + ")"
                    )
            db.execute(
                "INSERT OR REPLACE INTO daily "
                "(meter_id, day_start, energy_wh, peak_power, avg_power, cost_cent, "
                "is_final, covered_hours, shutdown_hours, price_cent, tariff_snapshot) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    meter_id, day_start, energy_wh,
                    bucket["peak"] if bucket else None, avg_power,
                    cost.cost_cent, meter_final, covered_hours, shutdown_hours,
                    cost.price_cent,
                    tariff.dumps(cost, covered_hours),
                ),
            )
            settled_meters += 1

        if short_meters:
            db.log_event(
                "warn", "collector", "SETTLE_METER_INCOMPLETE",
                f"day_start={day_start} 有 {len(short_meters)} 块表小时覆盖不足，"
                f"单独保留未结算: {', '.join(short_meters[:10])}"
                + (" ..." if len(short_meters) > 10 else ""),
            )

        if shutdown_meters:
            # 留一条痕：日报"定稿了但看起来只有几个小时数据"时，读表的人要靠它解释。
            db.log_event(
                "info", "collector", "SETTLE_SHUTDOWN",
                f"day_start={day_start} 有 {len(shutdown_meters)} 块表存在停机小时，"
                f"已从覆盖率分母剔除: {', '.join(shutdown_meters[:10])}"
                + (" ..." if len(shutdown_meters) > 10 else ""),
            )

        db.log_event(
            "info", "collector", "SETTLE_DONE",
            f"日结算完成 day_start={day_start}，is_final={day_final}，"
            f"应结算表数={len(target_ids)}，有数据表数={len(buckets)}，"
            f"写入行数={settled_meters}，含停机时段表数={len(shutdown_meters)}",
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
            "WHERE d.day_start=?" + db.demo_guard("m") + " GROUP BY m.purpose, m.node_id "
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
        guard: str = db.demo_guard("m")
        if guard:
            conditions.append(guard.removeprefix(" AND "))
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
