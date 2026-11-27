"""v0.1.10 四项现场口径：断电=断线、负功率=异常、结算查询合并、报表预览补覆盖/停机小时。

对应强哥 2026-11-27 的四条决定：
  ① 设备彻底断电「直接显示断线」——不再当成停机豁免，日结该拖住就拖住；
  ② 功率为负「显示为异常」——原始值照常入库，但不进功率统计；
  ③ `_warn_if_reprice` 每块表一次查询要合并——预加载之后不再补查；
  ④ 看板报表预览补「覆盖小时 / 停机小时」两列。

这里每一条都钉在「失效会静默」的位置上：断线被当成停机 -> 带着缺口定稿；
负功率被当成 0 也好、被当成正常值也好，都不会报错；多查 6000 次库只是慢；
报表少两列则要等到对账时才发现解释不了「为什么 3 小时数据也算已结算」。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import db
import link
import settings
import tariff
from aggregation import _assumed_shutdown_hours, aggregator
from collector import collector
from helpers import (
    FakeGateway,
    add_reading,
    create_device,
    create_meter,
    create_node,
    create_point,
    link_point,
    run,
    wait_until,
)

DAY_START = int(datetime(2026, 4, 8, tzinfo=timezone.utc).timestamp())


# ---------- ① 断线判定 ----------


def test_stale_window_is_clamped_both_ends():
    """轮询 10s 不该因为一次抖动就报断线；轮询 4 小时也不该等 12 小时才报。"""
    assert link.stale_after_sec(10) == link.LINK_STALE_MIN_SEC == 120
    assert link.stale_after_sec(60) == 180
    assert link.stale_after_sec(7200) == link.LINK_STALE_MAX_SEC == 3600
    # 空值/脏值退回 60s 轮询口径，而不是抛错或返回 0（返回 0 会让所有设备恒断线）
    assert link.stale_after_sec(None) == 180
    assert link.stale_after_sec("abc") == 180


def test_link_state_boundaries():
    now = 1_800_000_000
    base: dict[str, Any] = {
        "id": 7, "name": "E90", "protocol": "modbus_tcp",
        "is_enabled": 1, "poll_interval": 60,
    }
    assert link.classify({**base, "last_ok_at": now - 180}, now)["state"] == "online"
    offline = link.classify({**base, "last_ok_at": now - 181}, now)
    assert offline["state"] == "offline"
    assert offline["label"] == "断线"
    assert offline["stale_sec"] == 181

    never = link.classify({**base, "last_ok_at": None}, now)
    assert never["state"] == "never"
    # stale_sec 必须是 None：给 0 会被前端渲染成「刚刚采到」，把配置错误显示成正常
    assert never["stale_sec"] is None

    assert link.classify({**base, "is_enabled": 0}, now)["state"] == "disabled"
    assert link.classify({**base, "protocol": "manual"}, now)["state"] == "manual"
    # 演示实例：模拟器造的网关 protocol 也是 manual，但没有链路概念，
    # 不加这一档会让演示界面整片显示成「人工录入」
    demo = link.classify({**base, "is_demo": 1, "protocol": "manual"}, now)
    assert demo["state"] == "demo"


def test_link_state_falls_back_to_latest_reading_time():
    """模拟模式不写 device.last_ok_at，只靠它会把演示实例整片显示成「从未连通」。"""
    now = 1_800_000_000
    device: dict[str, Any] = {
        "id": 1, "name": "模拟网关", "protocol": "modbus_tcp",
        "is_enabled": 1, "poll_interval": 60, "last_ok_at": None,
    }
    assert link.classify({**device, "last_data_at": now - 30}, now)["state"] == "online"
    # 两个信号取较晚者：采集成功但点位刚加、还没有读数时不能判成断线
    assert link.classify(
        {**device, "last_ok_at": now - 10, "last_data_at": now - 900}, now
    )["state"] == "online"
    assert link.classify({**device, "last_data_at": now - 900}, now)["state"] == "offline"


# ---------- ① 断电缺口不算停机 ----------


def test_assumed_shutdown_skips_silent_hours():
    """夹在停机之间的缺口默认按停机推断，但整台设备静默的小时要扣掉。"""
    observed: set[int] = set(range(0, 11)) | set(range(14, 24))
    shutdown: set[int] = {hour for hour in observed if hour not in (0, 1, 2)}
    assert _assumed_shutdown_hours(observed, shutdown, 3) == 3
    assert _assumed_shutdown_hours(observed, shutdown, 3, {11, 12, 13}) == 0
    # 部分静默（11、12 断电、13 只是没采到）只扣掉断电那两个小时
    assert _assumed_shutdown_hours(observed, shutdown, 3, {11, 12}) == 1


def _hour(meter_id: int, hour: int, *, energy_wh: int, power: float | None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO hourly "
        "(meter_id, hour_start, energy_wh, avg_power, max_power, sample_cnt) "
        "VALUES (?,?,?,?,?,?)",
        (meter_id, DAY_START + hour * 3600, energy_wh, power, power, 0 if power is None else 12),
    )


def _build_shift_day(*, device_silent_in_gap: bool) -> int:
    """3 小时生产 + 18 小时待机 + 中间 3 小时缺口；缺口里设备是否有读数可切换。

    与 test_shutdown.py 的典型夜班日同构，唯一区别是多了一个真实设备：
    缺口小时里只要设备还有任何点位在上报，就说明「设备活着、只是这台表没数据」；
    一个读数都没有，才是断电/链路断。
    """
    node = create_node("注塑车间")
    device_id = create_device(poll_interval=60)
    meter_id = create_meter(node, name="1号产线")
    reading_point = create_point(device_id, name="累计电量", reg_offset=0, unit="kWh")
    power_point = create_point(device_id, name="瞬时功率", reg_offset=8, unit="kW")
    link_point(meter_id, reading_point, "reading")
    link_point(meter_id, power_point, "power")

    for hour in (0, 1, 2):
        _hour(meter_id, hour, energy_wh=100_000, power=860.0)
    alive_hours = list(range(3, 11)) + list(range(14, 24))
    for hour in alive_hours:
        _hour(meter_id, hour, energy_wh=0, power=6.5)
    if not device_silent_in_gap:
        alive_hours += [11, 12, 13]
    for hour in alive_hours:
        add_reading(power_point, DAY_START + hour * 3600 + 60, 6.5, 0)
    return meter_id


def _settled(meter_id: int) -> Any:
    return db.query_one(
        "SELECT is_final, covered_hours, shutdown_hours FROM daily WHERE meter_id=?",
        (meter_id,),
    )


def test_power_cut_gap_blocks_settlement(ready_db, monkeypatch):
    """断电 3 小时：缺口按缺数据算，覆盖率 3/6 不够 → 不许定稿。

    这正是「断电直接显示断线」的实质：断线不是停机，不能拿停产豁免把缺口抹平。
    """
    meter_id = _build_shift_day(device_silent_in_gap=True)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _settled(meter_id)
    assert int(row["covered_hours"]) == 21
    assert int(row["shutdown_hours"]) == 18, "断电的 3 小时不该被推断成停机"
    assert int(row["is_final"]) == 0, "带断电缺口的日不能定稿"


def test_device_alive_gap_still_settles(ready_db, monkeypatch):
    """同样 3 小时缺口，但设备还在（有读数）→ 仍按停机推断，照常定稿。"""
    meter_id = _build_shift_day(device_silent_in_gap=False)
    monkeypatch.setattr(settings, "SHUTDOWN_POWER_W", 50.0)

    aggregator.settle_day(DAY_START)
    row = _settled(meter_id)
    assert int(row["shutdown_hours"]) == 21, "18 观测停机 + 3 缺口推断"
    assert int(row["is_final"]) == 1


# ---------- ② 负功率记异常 ----------


def test_negative_power_is_stored_with_invalid_quality(isolated_db):
    """-1 kW 照原样入库（现场要看见），但 quality=2，不进功率统计。"""
    async def scenario() -> None:
        # 0xFFFF 按 s16 解出来就是 -1：现场最常见的「CT 反接」形态
        async with FakeGateway(values=[0xFFFF]) as gateway:
            device_id = create_device(port=gateway.port, poll_interval=60)
            node = create_node("A栋")
            meter_id = create_meter(node)
            point_id = create_point(
                device_id, name="瞬时功率", reg_offset=0, reg_count=1,
                data_type="s16", unit="kW",
            )
            link_point(meter_id, point_id, "power")
            device = dict(db.query_one("SELECT * FROM device WHERE id=?", (device_id,)))
            task = asyncio.create_task(collector.poll_device(device))
            try:
                def stored() -> bool:
                    row = db.query_one(
                        "SELECT 1 FROM reading WHERE point_id=?", (point_id,)
                    )
                    event = db.query_one(
                        "SELECT 1 FROM event_log WHERE code='POWER_NEGATIVE'"
                    )
                    return row is not None and event is not None

                assert await wait_until(stored)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            row = db.query_one(
                "SELECT value, quality FROM reading WHERE point_id=?", (point_id,)
            )
            assert row["value"] == -1.0, "原始负值不能被抹掉，否则现场查不出是反接"
            assert row["quality"] == 2, "负功率必须标成越界/异常"
            event = db.query_one("SELECT message FROM event_log WHERE code='POWER_NEGATIVE'")
            assert "按异常处理" in str(event["message"])

    run(scenario())


def test_negative_energy_reading_is_not_treated_as_invalid(isolated_db):
    """累计电量点回退是「翻转/换表」，不能顺手按负值拦成异常（另有 METER_* 处置）。"""
    async def scenario() -> None:
        async with FakeGateway(values=[0xFFFF]) as gateway:
            device_id = create_device(port=gateway.port, poll_interval=60)
            node = create_node("A栋")
            meter_id = create_meter(node)
            point_id = create_point(
                device_id, name="累计电量", reg_offset=0, reg_count=1,
                data_type="s16", unit="kWh",
            )
            link_point(meter_id, point_id, "reading")
            device = dict(db.query_one("SELECT * FROM device WHERE id=?", (device_id,)))
            task = asyncio.create_task(collector.poll_device(device))
            try:
                assert await wait_until(lambda: db.query_one(
                    "SELECT 1 FROM reading WHERE point_id=?", (point_id,)
                ) is not None)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            row = db.query_one(
                "SELECT value, quality FROM reading WHERE point_id=?", (point_id,)
            )
            assert (row["value"], row["quality"]) == (-1.0, 0)
            assert db.query_one("SELECT 1 FROM event_log WHERE code='POWER_NEGATIVE'") is None

    run(scenario())


# ---------- ③ 结算查询合并 ----------


def test_warn_if_reprice_does_not_requery_after_preload(ready_db, monkeypatch):
    """预加载后「确认这天没有日报」必须靠 None 短路，不能再补一次库。"""
    calls: list[str] = []
    real_query_one = db.query_one

    def counted(sql: str, params: Any = ()) -> Any:
        calls.append(sql.split()[1])
        return real_query_one(sql, params)

    monkeypatch.setattr(db, "query_one", counted)
    cost = tariff.CostResult(cost_cent=100, price_cent=80.0)

    # 预加载过、且确认不存在 -> 一次都不该查
    aggregator._warn_if_reprice(1, DAY_START, cost, None)
    assert calls == [], "100 表×30 天补算会因此变成 6000 次重复查询"

    # 没预加载（默认哨兵）-> 允许现查一次
    aggregator._warn_if_reprice(1, DAY_START, cost)
    assert len(calls) == 1 and calls[0] == "cost_cent,", "只有真正没预加载时才回查"


# ---------- ④ 报表预览补两列 ----------


def test_report_preview_carries_covered_and_shutdown_columns(isolated_db):
    """看板报表预览必须能显示覆盖/停机小时，且周/月明确留「—」。"""
    from pathlib import Path

    import main

    text = (Path(main.__file__).resolve().parent / "dashboard" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 't("rep_covered")' in text and 't("rep_shutdown")' in text
    assert 'cell(row, "covered_hours")' in text
    assert 'cell(row, "shutdown_hours")' in text
    # 周/月没有小时概念：留「—」而不是补 24，否则看起来像数据齐全
    assert 'isDaily && row[key] !== null' in text
    assert "rep_coverage_hint" in text


def test_api_devices_link_reports_offline(ready_db):
    """GET /api/devices/link 要能把「断电」直接说成断线，而不是让人盯时间戳猜。"""
    import time

    import httpx
    import main

    alive_id = create_device(name="在线网关", poll_interval=60)
    dead_id = create_device(name="断电网关", poll_interval=60)
    idle_id = create_device(name="未跑通网关", poll_interval=60)
    never_call_id = create_device(name="手动表", poll_interval=60)
    moment = int(time.time())
    db.execute("UPDATE device SET last_ok_at=? WHERE id=?", (moment - 30, alive_id))
    db.execute("UPDATE device SET last_ok_at=? WHERE id=?", (moment - 7200, dead_id))
    db.execute("UPDATE device SET protocol='manual', is_enabled=0 WHERE id=?", (never_call_id,))

    async def scenario() -> dict[str, Any]:
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/login", json={"password": "ems2026"})
            response = await client.get("/api/devices/link")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["ok"] is True
            return {item["device_id"]: item for item in payload["data"]}

    states = run(scenario())
    assert states[alive_id]["state"] == "online"
    assert states[dead_id]["state"] == "offline"
    assert states[dead_id]["label"] == "断线"
    assert states[idle_id]["state"] == "never"
    assert states[never_call_id]["state"] == "disabled"
