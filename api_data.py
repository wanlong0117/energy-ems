"""数据查询 API：/api/latest、/api/hourly、/api/daily、/api/settle。

支持 meter_id/from/to/purpose/node_id/is_final 过滤；全部路由需登录。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

import auth
import db
from aggregation import aggregator
from auth import require_auth
from schemas import SettleIn, fail, ok
from settings import DATA_MODE_DEFAULT, SECONDS_PER_DAY

router = APIRouter(prefix="/api", tags=["data"], dependencies=[Depends(require_auth)])


class DataService:
    """数据服务层：组装过滤条件并读取最新值/小时/日汇总。"""

    @staticmethod
    def latest(point_id: int | None = None, meter_id: int | None = None) -> list[dict[str, Any]]:
        """最新读数：可按时段点位或所属计量表过滤。"""
        conditions: list[str] = []
        params: list[Any] = []
        if point_id is not None:
            conditions.append("rl.point_id=?")
            params.append(int(point_id))
        if meter_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM meter_point mp WHERE mp.point_id=rl.point_id "
                "AND mp.meter_id=?)"
            )
            params.append(int(meter_id))
        sql: str = (
            "SELECT rl.point_id, rl.ts, rl.value, rl.quality, p.name, p.unit, p.device_id "
            "FROM reading_latest rl JOIN point p ON p.id=rl.point_id"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY rl.ts DESC, rl.point_id"
        return [dict(row) for row in db.query(sql, tuple(params))]

    @staticmethod
    def hourly(
        meter_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        purpose: str | None = None,
        node_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """小时汇总：meter_id / 时间窗 / purpose / node_id 过滤。"""
        conditions: list[str] = []
        params: list[Any] = []
        if meter_id is not None:
            conditions.append("h.meter_id=?")
            params.append(int(meter_id))
        if from_ts is not None:
            conditions.append("h.hour_start>=?")
            params.append(int(from_ts))
        if to_ts is not None:
            conditions.append("h.hour_start<=?")
            params.append(int(to_ts))
        if purpose:
            conditions.append("m.purpose=?")
            params.append(purpose)
        if node_id is not None:
            conditions.append("m.node_id=?")
            params.append(int(node_id))
        sql: str = (
            "SELECT h.*, m.name AS meter_name, m.purpose, m.node_id, n.name AS node_name "
            "FROM hourly h JOIN meter m ON m.id=h.meter_id "
            "LEFT JOIN node n ON n.id=m.node_id"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY h.hour_start DESC, h.meter_id"
        return [dict(row) for row in db.query(sql, tuple(params))]

    @staticmethod
    def daily(
        meter_id: int | None = None,
        from_ts: int | None = None,
        to_ts: int | None = None,
        purpose: str | None = None,
        node_id: int | None = None,
        is_final: int | None = None,
    ) -> list[dict[str, Any]]:
        """日汇总：在小时过滤项基础上增加 is_final 过滤。"""
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
            "SELECT d.*, m.name AS meter_name, m.purpose, m.node_id, n.name AS node_name "
            "FROM daily d JOIN meter m ON m.id=d.meter_id "
            "LEFT JOIN node n ON n.id=m.node_id"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY d.day_start DESC, d.meter_id"
        return [dict(row) for row in db.query(sql, tuple(params))]


data_service: DataService = DataService()


def _internal(code: str, action: str, exc: Exception) -> Any:
    """内部错误不回显原文：原文进事件日志，对外只给通用话术。"""
    db.log_event("error", "api", code, f"{action}失败: {type(exc).__name__}: {exc}")
    return fail(f"{action}失败，请查看事件日志", 500)


@router.get("/latest")
def latest(
    point_id: int | None = None, meter_id: int | None = None
) -> Any:
    """GET /api/latest：各点最新读数。"""
    try:
        return ok(data_service.latest(point_id, meter_id))
    except Exception as exc:
        return _internal("LATEST_QUERY_FAIL", "查询最新读数", exc)


@router.get("/hourly")
def hourly(
    meter_id: int | None = None,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    purpose: str | None = None,
    node_id: int | None = None,
) -> Any:
    """GET /api/hourly：小时汇总（from/to 为 Unix 秒 UTC）。"""
    try:
        return ok(data_service.hourly(meter_id, from_, to, purpose, node_id))
    except Exception as exc:
        return _internal("HOURLY_QUERY_FAIL", "查询小时汇总", exc)


@router.get("/daily")
def daily(
    meter_id: int | None = None,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    purpose: str | None = None,
    node_id: int | None = None,
    is_final: int | None = None,
) -> Any:
    """GET /api/daily：日汇总（可按 is_final 过滤结算状态）。"""
    try:
        return ok(data_service.daily(meter_id, from_, to, purpose, node_id, is_final))
    except Exception as exc:
        return _internal("DAILY_QUERY_FAIL", "查询日汇总", exc)


@router.get("/weekly")
def weekly(
    meter_id: int | None = None,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    purpose: str | None = None,
    node_id: int | None = None,
    is_final: int | None = None,
) -> Any:
    """GET /api/weekly：ISO 周汇总（周一 UTC 00:00 为首），从 daily 按需聚合。"""
    try:
        return ok(aggregator.weekly(meter_id, from_, to, purpose, node_id, is_final))
    except Exception as exc:
        return _internal("WEEKLY_QUERY_FAIL", "查询周汇总", exc)


@router.get("/monthly")
def monthly(
    meter_id: int | None = None,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    purpose: str | None = None,
    node_id: int | None = None,
    is_final: int | None = None,
) -> Any:
    """GET /api/monthly：自然月汇总（1 日 UTC 00:00 为首），从 daily 按需聚合。"""
    try:
        return ok(aggregator.monthly(meter_id, from_, to, purpose, node_id, is_final))
    except Exception as exc:
        return _internal("MONTHLY_QUERY_FAIL", "查询月汇总", exc)


@router.get("/mode")
def mode() -> Any:
    """GET /api/mode：返回当前 data_mode（登录页只读展示；需登录）。"""
    try:
        config: dict[str, Any] = auth.load_config()
        return ok({
            "data_mode": str(config.get("data_mode", DATA_MODE_DEFAULT)),
            "simulate_interval_sec": int(config.get("simulate_interval_sec", 300)),
            "simulate_rollup_sec": int(config.get("simulate_rollup_sec", 600)),
        })
    except Exception as exc:
        return _internal("MODE_QUERY_FAIL", "查询数据模式", exc)


@router.post("/settle")
def settle(body: SettleIn | None = None) -> Any:
    """POST /api/settle：手动结算；省略 day_start 时结算最近一个完整日。

    结算是否定稿由覆盖率决定：数据不完整时保留 is_final=0，响应里如实回传。
    """
    try:
        today_start: int = (int(time.time()) // SECONDS_PER_DAY) * SECONDS_PER_DAY
        if body is not None and body.day_start is not None:
            day_start: int = int(body.day_start)
            if day_start > today_start:
                return fail("不允许结算未来日期", 400)
        else:
            day_start = today_start - SECONDS_PER_DAY
        aggregator.settle_day(day_start)
        final_row = db.query_one(
            "SELECT MIN(is_final) AS final_flag FROM daily WHERE day_start=?", (day_start,)
        )
        settled: bool = bool(final_row and int(final_row["final_flag"] or 0) == 1)
        return ok({
            "day_start": day_start,
            "settled": settled,
            "is_final": 1 if settled else 0,
            "purposes": aggregator.split_by_purpose(day_start),
        })
    except ValueError as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return _internal("SETTLE_FAIL", "手动结算", exc)
