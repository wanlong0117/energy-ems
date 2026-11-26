"""手动录入（机械排水量表 / 未来离线表补录）：单事务三表一致 + 回填重算。

record() 在单事务内写 manual_reading + reading(quality=3) + reading_latest
（latest 仅当新 ts >= 旧 ts 覆盖）；重复 (point_id, ts) 覆盖并写审计日志。
提交后 recompute(ts) -> rollup_hour(ts 所在小时) + settle_day(ts 所在日，保留原 is_final)。
weekly/monthly 为按需聚合，无需重算。
"""
from __future__ import annotations

import time
from typing import Any

import db
from aggregation import aggregator
from settings import QUALITY_MANUAL, SECONDS_PER_DAY, SECONDS_PER_HOUR, day_floor


class ManualService:
    """人工读数服务：事务写入 + 最近列表 + 受影响小时/日回填。"""

    # 补录窗口：只允许最近 90 天至当前时间后 5 分钟，避免凭空生成 1970 或未来日期的汇总
    MAX_LOOKBACK_DAYS: int = 90
    MAX_FUTURE_SEC: int = 300

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

    def record(self, body: dict[str, Any]) -> dict[str, Any]:
        """录入一条人工读数，返回写入结果与是否覆盖。"""
        point_id: int = int(body.get("point_id", 0))
        ts: int = self._check_ts(int(body.get("ts", 0)))
        value: float = float(body.get("value", 0.0))
        operator: str = str(body.get("operator") or "").strip()
        remark: str | None = body.get("remark")

        point = db.query_one(
            "SELECT p.id, p.name, p.unit, d.protocol AS protocol "
            "FROM point p JOIN device d ON d.id=p.device_id WHERE p.id=?",
            (point_id,),
        )
        if point is None:
            raise LookupError(f"点位 {point_id} 不存在")

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
        """回填受影响小时与当日汇总；当日原 is_final 状态保留。

        操作员对历史数据的明确修正不再受自动结算的覆盖率门槛约束，
        因此传 enforce_coverage=False，保持原 is_final 语义。
        """
        ts = int(ts)
        hour_start: int = (ts // SECONDS_PER_HOUR) * SECONDS_PER_HOUR
        day_start: int = day_floor(ts)
        aggregator.rollup_hour(hour_start)

        current = db.query_one(
            "SELECT is_final FROM daily WHERE day_start=? ORDER BY is_final DESC LIMIT 1",
            (day_start,),
        )
        if current is not None:
            is_final: int = int(current["is_final"])
        else:
            today_start: int = day_floor(time.time())
            is_final = 0 if day_start == today_start else 1
        aggregator.settle_day(day_start, is_final=is_final, enforce_coverage=False)

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
