"""分时电价（峰平谷）：把 tariff 表的 start_hour/end_hour 真正接进日结算。

计价口径：
- tariff 行若 start_hour 与 end_hour 都不为空，视为一个「本地小时区间」[start, end) 的时段电价；
- is_default=1 且无区间的行，作为全时段单一电价（峰平谷时段未覆盖时的小时兜底）；
- 两者都没有 -> 电费为 0，并在快照里标记 none，避免把缺失配置算成免费电。

日结算按「该表当天各小时的毫度电量 × 对应时段单价」逐小时累加，
而不是把日总量乘单一电价——否则峰平谷形同虚设。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

import db
from settings import MILLI_PER_KWH, local_hour


@dataclass
class CostResult:
    """一次日结算的电费计算结果（含可直接落库的价格快照）。"""

    cost_cent: int = 0
    price_cent: float | None = None
    mode: str = "none"
    uncovered_hours: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self, hours: int) -> dict[str, Any]:
        """价格快照：落 daily.tariff_snapshot，改价后仍能追溯当初按什么价计费。"""
        return {
            "mode": self.mode,
            "price_cent": None if self.price_cent is None else round(self.price_cent, 4),
            "segments": self.segments,
            "hours": int(hours),
            "uncovered_hours": int(self.uncovered_hours),
        }


def load_tariffs() -> list[dict[str, Any]]:
    """读取全部电价行（按 id 排序，先定义者优先匹配）。"""
    return [
        dict(row)
        for row in db.query(
            "SELECT id, name, price_cent, start_hour, end_hour, is_default "
            "FROM tariff ORDER BY id"
        )
    ]


def _segments(tariffs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出真正的分时时段（start/end 均非空且合法）。"""
    result: list[dict[str, Any]] = []
    for row in tariffs:
        start: Any = row.get("start_hour")
        end: Any = row.get("end_hour")
        if start is None or end is None:
            continue
        try:
            start_hour: int = int(start)
            end_hour: int = int(end)
        except (TypeError, ValueError):
            continue
        if not (0 <= start_hour < end_hour <= 24):
            continue
        result.append(
            {
                "name": str(row.get("name") or "时段"),
                "price_cent": float(row.get("price_cent") or 0),
                "start_hour": start_hour,
                "end_hour": end_hour,
            }
        )
    return result


def _default_price(tariffs: Iterable[dict[str, Any]]) -> float | None:
    """兜底单价：优先 is_default=1 的行，其次第一条无区间的行。"""
    fallback: float | None = None
    for row in tariffs:
        if row.get("start_hour") is not None and row.get("end_hour") is not None:
            continue
        price: Any = row.get("price_cent")
        if price is None:
            continue
        if int(row.get("is_default") or 0) == 1:
            return float(price)
        if fallback is None:
            fallback = float(price)
    return fallback


def price_at(hour: int, segments: list[dict[str, Any]], fallback: float | None) -> float | None:
    """某本地小时适用的单价（分/kWh）；无覆盖且无兜底时返回 None。"""
    for segment in segments:
        if segment["start_hour"] <= hour < segment["end_hour"]:
            return float(segment["price_cent"])
    return fallback


def cost_for_hourly(hourly: Iterable[tuple[int, int]]) -> CostResult:
    """按小时电量计费：hourly 为 (hour_start, energy_wh) 序列。"""
    tariffs: list[dict[str, Any]] = load_tariffs()
    segments: list[dict[str, Any]] = _segments(tariffs)
    fallback: float | None = _default_price(tariffs)
    result = CostResult(
        mode="tou" if segments else ("flat" if fallback is not None else "none"),
        segments=segments,
    )

    total_cost: float = 0.0
    total_energy_wh: int = 0
    covered_energy_wh: int = 0
    hours: int = 0
    for hour_start, energy_wh in hourly:
        energy: int = int(energy_wh or 0)
        hours += 1
        total_energy_wh += energy
        if energy == 0:
            continue
        price: float | None = price_at(local_hour(int(hour_start)), segments, fallback)
        if price is None:
            result.uncovered_hours += 1
            continue
        total_cost += energy / MILLI_PER_KWH * price
        covered_energy_wh += energy

    result.cost_cent = int(round(total_cost))
    if covered_energy_wh:
        result.price_cent = total_cost / (covered_energy_wh / MILLI_PER_KWH)
    elif total_energy_wh == 0 and fallback is not None and not segments:
        result.price_cent = fallback
    return result


def flat_cost(energy_wh: int) -> CostResult:
    """单一电价计费（无小时明细时的回退路径，保持 v2 行为）。"""
    tariffs: list[dict[str, Any]] = load_tariffs()
    fallback: float | None = _default_price(tariffs)
    if fallback is None:
        return CostResult(mode="none")
    energy: int = int(energy_wh or 0)
    return CostResult(
        cost_cent=int(round(energy / MILLI_PER_KWH * fallback)),
        price_cent=fallback,
        mode="flat",
    )


def dumps(result: CostResult, hours: int) -> str:
    """序列化快照（紧凑 JSON，便于直接存 TEXT 列）。"""
    return json.dumps(result.snapshot(hours), ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "CostResult",
    "cost_for_hourly",
    "dumps",
    "flat_cost",
    "load_tariffs",
    "price_at",
]
