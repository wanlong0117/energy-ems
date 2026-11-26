"""手动录入（机械排水量表 / 未来离线表补录）：单事务三表一致 + 回填重算。

record() 在单事务内写 manual_reading + reading(quality=3) + reading_latest
（latest 仅当新 ts >= 旧 ts 覆盖）；重复 (point_id, ts) 覆盖并写审计日志。
提交后 recompute(ts) -> rollup_hour(ts 所在小时) + settle_day(ts 所在日，保留原 is_final)。
weekly/monthly 为按需聚合，无需重算。
"""
from __future__ import annotations

import math
import time
from typing import Any

import db
from aggregation import aggregator
from settings import (
    MANUAL_MAX_ABS_VALUE,
    QUALITY_MANUAL,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    day_floor,
)


class ManualService:
    """人工读数服务：事务写入 + 最近列表 + 受影响小时/日回填。"""

    # 补录窗口：只允许最近 90 天至当前时间后 5 分钟，避免凭空生成 1970 或未来日期的汇总
    MAX_LOOKBACK_DAYS: int = 90
    MAX_FUTURE_SEC: int = 300
    # 单条读数的绝对上限：超过 1e9（kWh 口径下是十亿度）不可能是真实抄见值，
    # 而这类值会在 ×1e6 转毫度时超出 SQLite INTEGER 范围，把整轮汇总打断。
    MAX_ABS_VALUE: float = MANUAL_MAX_ABS_VALUE

    def _check_ts(self, ts: int) -> int:
        """校验时间戳落在允许区间内，返回规范化后的整数秒。"""
        if ts <= 0:
            raise ValueError("ts 必须为正整数时间戳")
        now: int = int(time.time())
        earliest: int = now - self.MAX_LOOKBACK_DAYS * SECONDS_PER_DAY
        if ts < earliest:
            raise ValueError(f"ts 超出可补录范围（仅支持最近 {self.MAX_LOOKBACK_DAYS} 天）")
        if ts > now + self.MAX_FUTURE_SEC:
            raise ValueError("ts 不得晚于当前时间 5 分钟以上")
        return ts

    def _check_value(self, value: Any, point: dict[str, Any]) -> float:
        """校验读数：必须有限、在绝对上限内、并落在点位配置的量程里。

        校验放在服务层而不是只放在请求模型上：脚本、运维钩子也会直接调用
        record()，模型层的约束到不了这些入口。旧实现放行 1e20 后，
        rollup_hour 在 int(value*1e6) 处抛 OverflowError，异常冒到轮询循环外，
        排在它后面的表整轮停止更新，而小时任务每 5 分钟重试一次，等于每 5 分钟
        重复失败一次。
        """
        try:
            number: float = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("value 必须为数值") from exc
        if not math.isfinite(number):
            raise ValueError("value 必须为有限数值（不接受 inf/nan）")
        if abs(number) > self.MAX_ABS_VALUE:
            raise ValueError(
                f"value 超出合理范围（|value| ≤ {self.MAX_ABS_VALUE:g}），请核对单位与小数点"
            )
        min_val: Any = point.get("min_val")
        max_val: Any = point.get("max_val")
        if min_val is not None and number < float(min_val):
            raise ValueError(f"value={number} 低于点位下限 {float(min_val):g}")
        if max_val is not None and number > float(max_val):
            raise ValueError(f"value={number} 高于点位上限 {float(max_val):g}")
        return number

    def record(self, body: dict[str, Any]) -> dict[str, Any]:
        """录入一条人工读数，返回写入结果与是否覆盖。"""
        point_id: int = int(body.get("point_id", 0))
        ts: int = self._check_ts(int(body.get("ts", 0)))
        operator: str = str(body.get("operator") or "").strip()
        remark: str | None = body.get("remark")

        point_row = db.query_one(
            "SELECT p.id, p.name, p.unit, p.min_val, p.max_val, d.protocol AS protocol "
            "FROM point p JOIN device d ON d.id=p.device_id WHERE p.id=?",
            (point_id,),
        )
        if point_row is None:
            raise LookupError(f"点位 {point_id} 不存在")
        point: dict[str, Any] = dict(point_row)
        value: float = self._check_value(body.get("value", 0.0), point)

        row: dict[str, Any] = {
            "point_id": point_id,
            "ts": ts,
            "value": value,
            "operator": operator,
            "remark": remark,
            "point_name": str(point["name"]),
        }
        overwritten: bool = self._write_in_txn(row)
        self.recompute(ts)
        return {
            "point_id": point_id,
            "ts": ts,
            "value": value,
            "operator": operator,
            "remark": remark,
            "point_name": row["point_name"],
            "overwritten": overwritten,
        }

    def _write_in_txn(self, row: dict[str, Any]) -> bool:
        """单事务写三表；返回是否存在被覆盖的原记录。

        事务跑在独立连接上（db.transaction），期间全局连接被锁挡住，
        不会出现「别的线程提交时把本次未提交写入一并提交」的串味。
        """
        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM manual_reading WHERE point_id=? AND ts=?",
                (row["point_id"], row["ts"]),
            ).fetchone()
            overwritten: bool = existing is not None
            # 是否覆盖了「真实采集值」：reading 里同 (point_id, ts) 已有非人工记录时，
            # 人工录入会把现场实测值改掉。旧实现只在覆盖 manual_reading 时留痕，
            # 于是真实计量数据可被无痕改写。
            raw_prev = conn.execute(
                "SELECT value, quality FROM reading WHERE point_id=? AND ts=?",
                (row["point_id"], row["ts"]),
            ).fetchone()
            clobbered_raw: bool = raw_prev is not None and int(raw_prev["quality"]) != QUALITY_MANUAL

            conn.execute(
                "INSERT OR REPLACE INTO manual_reading "
                "(point_id, ts, value, operator, remark) VALUES (?,?,?,?,?)",
                (
                    row["point_id"], row["ts"], row["value"],
                    row["operator"], row.get("remark"),
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO reading (point_id, ts, value, quality) VALUES (?,?,?,?)",
                (row["point_id"], row["ts"], row["value"], QUALITY_MANUAL),
            )
            conn.execute(
                "INSERT INTO reading_latest (point_id, ts, value, quality) VALUES (?,?,?,?) "
                "ON CONFLICT(point_id) DO UPDATE SET "
                "ts=excluded.ts, value=excluded.value, quality=excluded.quality "
                "WHERE excluded.ts >= reading_latest.ts",
                (row["point_id"], row["ts"], row["value"], QUALITY_MANUAL),
            )
            if clobbered_raw:
                conn.execute(
                    "INSERT INTO event_log (ts, level, source, code, message, ref_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        int(time.time()), "warn", "user", "MANUAL_OVERWRITE_RAW",
                        f"人工录入覆盖现场采集值 point_id={row['point_id']} ts={row['ts']} "
                        f"原值={raw_prev['value']}(quality={raw_prev['quality']}) "
                        f"新值={row['value']} operator={row['operator']}",
                        row["point_id"],
                    ),
                )
            if overwritten:
                # 审计与三表同事务，避免"覆盖成功但无日志"
                conn.execute(
                    "INSERT INTO event_log (ts, level, source, code, message, ref_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        int(time.time()), "warn", "user", "MANUAL_OVERWRITE",
                        f"人工读数覆盖 point_id={row['point_id']} ts={row['ts']} "
                        f"value={row['value']} operator={row['operator']}",
                        row["point_id"],
                    ),
                )
            return overwritten

    def recompute(self, ts: int) -> None:
        """回填受影响小时与日汇总；当日原 is_final 状态保留。

        小时窗口是**左开右闭** `(hour_start, hour_end]`，所以「受影响的小时」不止
        ts 所在的那个：

          · ts 正好落在整点 —— 这条值属于**上一小时**的收口（上一小时 delta 变了）；
          · ts 落在某小时内 —— 本小时 delta 变了；若该值是本小时最后一条，
            下一小时的基准也跟着变，所以要连下一小时一起重算。

        旧实现只重算当前小时、只结算当前日，操作员用 00:00/08:00 这类整点补录时
        上一小时与上一日会静默错账（实测应 10 kWh 仍显示 2 kWh），因此这里
        按小时集合与所属日集合分别重算，并把未来的小时排除在外。

        操作员对历史数据的明确修正不再受自动结算的覆盖率门槛约束，
        因此传 enforce_coverage=False，保持原 is_final 语义。
        """
        ts = int(ts)
        now: int = int(time.time())
        hour_start: int = (ts // SECONDS_PER_HOUR) * SECONDS_PER_HOUR
        hours: set[int] = {hour_start}
        if hour_start == ts:
            hours.add(hour_start - SECONDS_PER_HOUR)  # 整点补录改写的是上一小时收口
        next_hour: int = hour_start + SECONDS_PER_HOUR
        if next_hour <= now:
            hours.add(next_hour)  # 下一小时的基准可能变成这条值
        ordered: list[int] = sorted(hours)
        for hour in ordered:
            aggregator.rollup_hour(hour)
        for day_start in sorted({day_floor(hour) for hour in ordered}):
            aggregator.settle_day(
                day_start, is_final=self._day_final(day_start), enforce_coverage=False
            )

    @staticmethod
    def _day_final(day_start: int) -> int:
        """沿用该日原有结算状态；没有日报时，历史日按已结算、当日按未结算。"""
        current = db.query_one(
            "SELECT is_final FROM daily WHERE day_start=? ORDER BY is_final DESC LIMIT 1",
            (day_start,),
        )
        if current is not None:
            return int(current["is_final"])
        return 0 if day_start == day_floor(time.time()) else 1

    def list(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """最近录入列表（point_id / limit 过滤）。"""
        options: dict[str, Any] = dict(filters or {})
        conditions: list[str] = []
        params: list[Any] = []
        if options.get("point_id") is not None:
            conditions.append("mr.point_id=?")
            params.append(int(options["point_id"]))
        sql: str = (
            "SELECT mr.id, mr.point_id, mr.ts, mr.value, mr.operator, mr.remark, "
            "p.name AS point_name, p.unit FROM manual_reading mr "
            "JOIN point p ON p.id=mr.point_id"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY mr.ts DESC, mr.id DESC LIMIT ?"
        try:
            limit: int = max(1, min(int(options.get("limit", 100)), 500))
        except (TypeError, ValueError):
            limit = 100
        params.append(limit)
        return [dict(row) for row in db.query(sql, tuple(params))]


manual_service: ManualService = ManualService()
