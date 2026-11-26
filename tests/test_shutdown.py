"""停机判定：功率低于阈值的小时不计入日结算覆盖率分母。

现场口径：车间夜间停产时电表仍在线，功率掉到待机值、电量≈0。把这类小时算进
「应有数据的小时」等于要求「停产也得报数」，于是停机占大半的一天永远定不了稿，
报表上永远显示「未结算」，谁也不敢拿它去对账。

判定只认「有功率读数且低于阈值」的小时：
  · 没有功率点 / 该小时没采到功率 → 不当停机（否则「采不到」会被自己判成「停机」）；
  · 阈值为 0 → 规则关闭，行为与 v0.1.4 完全一致；
  · 阈值逐表可覆盖全局，避免注塑机（待机 3 kW）和照明回路（待机 20 W）共用一个数。

v0.1.6 修正了覆盖率门槛的分子分母不同源：分母剔了停机小时、分子却把停机小时
算成「有生产数据」，比值被凭空放大，导致「12 小时待机有数 + 12 小时生产全缺」
也能算出 12/12=1.0 并定稿。现在分子只算「有数据且非停机」的小时，缺失小时
默认按生产小时计；只有夹在两段已观测停机之间、且不超过
settings.SHUTDOWN_GAP_MAX_HOURS（默认 3 小时）的短缺口才按停机推断。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aggregation
import db
import httpx
import main
import pytest
import settings
from aggregation import aggregator
from helpers import create_meter, create_node, fill_hourly, run

DAY_START = int(datetime(2026, 3, 10, tzinfo=timezone.utc).timestamp())


def _hour(meter_id: int, day_start: int, hour: int, *, energy_wh: int, power: float | None) -> None:
    """写入一条小时汇总，power=None 表示该小时没有功率读数。"""
    db.execute(
        "INSERT OR REPLACE INTO hourly "
        "(meter_id, hour_start, energy_wh, avg_power, max_power, sample_cnt) "
        "VALUES (?,?,?,?,?,?)",
        (meter_id, day_start + hour * 3600, energy_wh, power, power, 0 if power is None else 12),
    )


def _daily(meter_id: int) -> Any:
    return db.query_one(
        "SELECT energy_wh, is_final, covered_hours, shutdown_hours FROM daily WHERE meter_id=?",
        (meter_id,),
    )


def _shift_day(meter_id: int) -> None:
    """典型夜班日：3 小时生产 + 18 小时停机 + 中间 3 小时采集缺口（夹在停机之间）。

    覆盖 21/24 = 87.5%，按纯 24 小时口径永远差一点定不了稿；而真正有数据的
    18 个非生产小时全是待机，缺口两侧也都是待机，可以放心按停机推断。
    """
    for hour in (0, 1, 2):
        _hour(meter_id, DAY_START, hour, energy_wh=100_000, power=860.0)
    for hour in list(range(3, 11)) + list(range(14, 24)):
        _hour(meter_id, DAY_START, hour, energy_wh=0, power=6.5)
    # 11/12/13 三个小时完全没有数据：左右两侧（10 点、14 点）都是待机


def test_shutdown_hours_are_excluded_from_coverage(ready_db, monkeypatch):
    """停机（含可推断的短缺口）被剔除后，分母只剩 3 小时生产 → 达标定稿。"""
    node = create_node("注塑车间")
    meter = create_meter(node, name="1号产线")
    _shift_day(meter)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["covered_hours"]) == 21
    # 18 个观测停机 + 3 个夹在停机之间的缺口 = 21
    assert int(row["shutdown_hours"]) == 21
    assert int(row["is_final"]) == 1, "停机时段不该拖住定稿"
    # 停机判定只改覆盖率口径，绝不改电量：0 度的小时仍然是 0 度
    assert int(row["energy_wh"]) == 300_000
    event = db.query_one("SELECT message FROM event_log WHERE code='SETTLE_SHUTDOWN'")
    assert event is not None and "缺口" in str(event["message"])


def test_same_day_stays_unsettled_without_threshold(ready_db, monkeypatch):
    """阈值关闭（0）时保持 v0.1.4 行为：覆盖 21/24 < 90%，照旧不定稿。

    这条是回归锚点——新规则不能悄悄放宽所有历史日的结算状态。
    """
    node = create_node("注塑车间")
    meter = create_meter(node, name="1号产线")
    _shift_day(meter)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 0.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 0
    assert int(row["is_final"]) == 0
    assert db.query_one("SELECT 1 FROM event_log WHERE code='SETTLE_SHUTDOWN'") is None


def test_full_day_shutdown_finalizes_with_zero_energy(ready_db, monkeypatch):
    """整日停产（24 小时都在待机）：没有应有生产小时，0 度日也能定稿。"""
    node = create_node("包装车间")
    meter = create_meter(node, name="包装线总表")
    for hour in range(24):
        _hour(meter, DAY_START, hour, energy_wh=0, power=4.0)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 24
    assert int(row["covered_hours"]) == 24
    assert int(row["energy_wh"]) == 0
    assert int(row["is_final"]) == 1


def test_zero_energy_day_with_short_gap_still_finalizes(ready_db, monkeypatch):
    """0 度停产日中间漏采 2 小时（两侧都是待机）仍能定稿——本功能要解决的原始场景。"""
    node = create_node("配料车间")
    meter = create_meter(node, name="配料线总表")
    for hour in list(range(0, 12)) + list(range(14, 24)):
        _hour(meter, DAY_START, hour, energy_wh=0, power=3.2)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 24, "22 个观测停机 + 2 个缺口推断"
    assert int(row["covered_hours"]) == 22
    assert int(row["energy_wh"]) == 0
    assert int(row["is_final"]) == 1


def test_meter_threshold_overrides_global(ready_db, monkeypatch):
    """表级阈值优先：同一天两块表，同一功率在一条线上算停机、在另一条上算生产。"""
    node = create_node("混合车间")
    light = create_meter(node, name="照明回路")
    machine = create_meter(node, name="注塑机")
    db.execute("UPDATE meter SET shutdown_power_w=3000 WHERE id=?", (machine,))
    for hour in range(24):
        # 两条回路都是 500 W：照明（阈值 50）算生产，注塑机（阈值 3000）算待机
        _hour(light, DAY_START, hour, energy_wh=500_000, power=500.0)
        _hour(machine, DAY_START, hour, energy_wh=500_000, power=500.0)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    assert int(_daily(light)["shutdown_hours"]) == 0
    assert int(_daily(machine)["shutdown_hours"]) == 24
    # 两块表都满覆盖，各自定稿；表级阈值不影响电量与电费
    assert int(_daily(light)["is_final"]) == 1
    assert int(_daily(machine)["is_final"]) == 1
    assert int(_daily(machine)["energy_wh"]) == int(_daily(light)["energy_wh"])


def test_hours_without_power_reading_are_not_shutdown(ready_db, monkeypatch):
    """没有功率读数的小时不能判停机：否则采集中断会被伪装成「停机」。"""
    node = create_node("原料仓")
    meter = create_meter(node, name="仓区表")
    fill_hourly(meter, DAY_START, energy_wh=1000)  # avg_power 全为 NULL
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 0
    assert int(row["covered_hours"]) == 24
    assert int(row["is_final"]) == 1


def test_edge_gap_is_not_assumed_shutdown(ready_db, monkeypatch):
    """生产时段有数、日界处缺 3 小时：缺口不被推断成停机，仍判未结算。

    v0.1.5 的实现会算出 22/(24-21)=7.33 并定稿——而缺的正是生产时段，
    等于给一次采集中断发了「已结算」证书（v0.1.6 修正的核心）。
    """
    node = create_node("返工区")
    meter = create_meter(node, name="返工线")
    _hour(meter, DAY_START, 0, energy_wh=100_000, power=860.0)  # 生产，1~2 点缺数
    for hour in range(3, 24):
        _hour(meter, DAY_START, hour, energy_wh=0, power=6.5)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 21
    assert int(row["is_final"]) == 0, "缺口贴着生产时段，不能当作停机"
    assert db.query_one("SELECT 1 FROM event_log WHERE code='SETTLE_METER_INCOMPLETE'") is not None


def test_long_gap_is_not_assumed_shutdown(ready_db, monkeypatch):
    """半天采集空白（12 小时）不推断成停机：那是有问题的一天，不是停产的一天。"""
    node = create_node("夜班车间")
    meter = create_meter(node, name="夜班表")
    for hour in list(range(0, 8)) + list(range(20, 24)):
        _hour(meter, DAY_START, hour, energy_wh=0, power=6.5)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 12, "长缺口不计入停机小时"
    assert int(row["covered_hours"]) == 12
    assert int(row["is_final"]) == 0


def test_gap_heuristic_can_be_disabled(ready_db, monkeypatch):
    """把上限调到 0 即关闭缺口推断：短缺口也回到「未结算」，行为最保守。"""
    node = create_node("包装车间")
    meter = create_meter(node, name="包装线")
    _shift_day(meter)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)
    monkeypatch.setattr(aggregation, "SHUTDOWN_GAP_MAX_HOURS", 0)

    aggregator.settle_day(DAY_START)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 18
    assert int(row["is_final"]) == 0


def test_shutdown_hours_survive_recompute_without_coverage_enforcement(ready_db, monkeypatch):
    """人工回填走 enforce_coverage=False：停机小时照样要记，只是不再卡定稿。"""
    node = create_node("返工区")
    meter = create_meter(node, name="返工线")
    _shift_day(meter)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START, is_final=1, enforce_coverage=False)
    row = _daily(meter)
    assert int(row["shutdown_hours"]) == 21
    assert int(row["is_final"]) == 1


@pytest.mark.parametrize("value", [-1, -0.5])
def test_api_rejects_negative_shutdown_threshold(ready_db, value):
    """负阈值无意义（等价于「永不判定」），入口直接拒，别让它静默变成 0。"""
    node = create_node("化验室")

    async def scenario() -> int:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/login", json={"password": "ems2026"})
            response = await client.post(
                "/api/config/meter",
                json={
                    "name": "化验室表",
                    "node_id": node,
                    "category_id": 1,
                    "shutdown_power_w": value,
                },
            )
            return response.status_code

    assert run(scenario()) >= 400


def test_api_stores_and_returns_shutdown_threshold(ready_db):
    """阈值能存能读，且更新时不会被默认值悄悄覆盖。"""
    node = create_node("装配车间")

    async def scenario() -> tuple[int, Any, Any]:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/login", json={"password": "ems2026"})
            created = await client.post(
                "/api/config/meter",
                json={
                    "name": "装配线表",
                    "node_id": node,
                    "category_id": 1,
                    "shutdown_power_w": 250.5,
                },
            )
            assert created.status_code == 200, created.text
            meter_id = created.json()["data"]["id"]
            updated = await client.put(
                f"/api/config/meter/{meter_id}", json={"purpose": "production"}
            )
            assert updated.status_code == 200, updated.text
            return meter_id, created.json()["data"], updated.json()["data"]

    _, created, updated = run(scenario())
    assert float(created["shutdown_power_w"]) == pytest.approx(250.5)
    assert float(updated["shutdown_power_w"]) == pytest.approx(250.5)


def test_report_carries_shutdown_hours_column(ready_db, monkeypatch):
    """报表要能解释「为什么只有 21 小时数据也算已结算」，否则对账的人只能猜。"""
    from reports import HEADERS, report_service

    node = create_node("注塑车间")
    meter = create_meter(node, name="1号产线")
    _shift_day(meter)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)
    aggregator.settle_day(DAY_START)

    rows = report_service.build("daily", {"from_ts": DAY_START, "to_ts": DAY_START})
    assert "停机小时" in HEADERS
    assert rows and rows[0]["shutdown_hours"] == 21
    assert rows[0]["final_text"] == "是"
