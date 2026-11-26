"""计量完整性回归：整点丢电量、覆盖率按表判定、掉线记 0、批量写半提交。

这些都是「数字悄悄错掉但系统照样标记已结算」的缺陷——代码能跑、页面正常、
测试全绿，只有把真实时序摊开算一遍才会暴露。每个用例对应一条实测复现路径。
"""
from __future__ import annotations

from datetime import datetime, timezone

import db
import settings
from aggregation import Aggregator, month_start, week_start
from collector import collector
from helpers import (
    add_reading, create_device, create_meter, create_node, create_point, fill_hourly,
    link_point, run,
)

DAY = int(datetime(2026, 11, 25, tzinfo=timezone.utc).timestamp())  # UTC 00:00


def _meter_with_reading_point(name: str = "1号表") -> tuple[int, int]:
    node_id = create_node(name="A栋")
    device_id = create_device()
    point_id = create_point(device_id, reg_offset=0, reg_count=1, data_type="u32")
    meter_id = create_meter(node_id, name=name)
    link_point(meter_id, point_id, "reading")
    return meter_id, point_id


# ---------- 1. 整点读数不再丢一个采样间隔 ----------


def test_hourly_energy_closes_across_boundary_readings(isolated_db):
    """300s 轮询、恒定 1 kWh/h，且读数正好落在整点：4 小时必须是 4.000 kWh。

    旧实现收口用 ts<hour_end，整点那条只被下一小时当基准，跨整点的一个间隔
    两边都不算（实测丢 8.3%）。改成右闭区间 (hour_start, hour_end] 后逐小时闭合。
    """
    meter_id, point_id = _meter_with_reading_point()
    value: float = 1000.0
    for index in range(4 * 3600 // 300 + 1):  # 00:00, 00:05 ... 04:00 共 49 条
        add_reading(point_id, DAY + index * 300, value)
        value += 1.0 / 12  # 每 300s 走 1/12 kWh

    aggregator = Aggregator()
    for hour in range(4):
        aggregator.rollup_hour(DAY + hour * 3600)

    rows = db.query(
        "SELECT hour_start, energy_wh FROM hourly WHERE meter_id=? ORDER BY hour_start",
        (meter_id,),
    )
    assert [row["energy_wh"] for row in rows] == [1_000_000] * 4
    assert sum(row["energy_wh"] for row in rows) == 4_000_000


def test_hourly_energy_matches_first_last_reading_delta(isolated_db):
    """整日 24 小时的汇总应等于首末读数差，不留系统性缺口。"""
    meter_id, point_id = _meter_with_reading_point()
    add_reading(point_id, DAY, 500.0)
    for hour in range(1, 25):
        add_reading(point_id, DAY + hour * 3600, 500.0 + hour)
    aggregator = Aggregator()
    for hour in range(24):
        aggregator.rollup_hour(DAY + hour * 3600)
    total = db.query_one(
        "SELECT SUM(energy_wh) AS total FROM hourly WHERE meter_id=?", (meter_id,)
    )["total"]
    assert total == 24_000_000  # 首末差 24 kWh，一小时不多一小时不少


# ---------- 2. 覆盖率按表判定 ----------


def test_settle_finalizes_per_meter_not_by_global_hour_union(isolated_db):
    """表1 满 24 小时可定稿；表2 只有 1 小时必须单独留在未结算。"""
    full_meter, _ = _meter_with_reading_point("满数据表")
    partial_meter, _ = _meter_with_reading_point("缺数据表")

    fill_hourly(full_meter, DAY, energy_wh=1_000_000)
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,?)",
        (partial_meter, DAY, 1_000_000, 10),
    )

    Aggregator().settle_day(DAY)
    rows = {
        int(row["meter_id"]): row
        for row in db.query(
            "SELECT meter_id, energy_wh, is_final FROM daily WHERE day_start=?", (DAY,)
        )
    }
    assert rows[full_meter]["is_final"] == 1
    assert rows[partial_meter]["is_final"] == 0
    # 数字本身仍照实写：不定稿只是标记「别拿它对账」，不是把数据丢掉
    assert rows[partial_meter]["energy_wh"] == 1_000_000
    assert db.query_one(
        "SELECT 1 FROM event_log WHERE code='SETTLE_METER_INCOMPLETE'"
    ) is not None


# ---------- 3. 掉线不再记成「用电 0」 ----------


def test_offline_meter_produces_no_hourly_rows_and_stays_unfinalized(isolated_db):
    """只剩 carry-forward（quality=1）的小时不得产出 0 电量行。

    旧实现取到几小时前的旧值相减得到 0，日结算再把 0 当最终值定稿——
    「没采到数据」变成「没用电」，而且是已结算状态。
    """
    meter_id, point_id = _meter_with_reading_point()
    add_reading(point_id, DAY, 1000.0, quality=0)
    add_reading(point_id, DAY + 3600, 1001.0, quality=0)  # 01:00 收口
    for hour in range(2, 24):  # 02:00 起掉线，采集器写保持值 quality=1
        add_reading(point_id, DAY + hour * 3600, 1001.0, quality=1)

    aggregator = Aggregator()
    for hour in range(24):
        aggregator.rollup_hour(DAY + hour * 3600)

    rows = db.query(
        "SELECT hour_start, energy_wh FROM hourly WHERE meter_id=? ORDER BY hour_start",
        (meter_id,),
    )
    assert len(rows) == 1, "掉线的小时不应产出汇总行（否则等于记了 0 电量）"
    assert rows[0]["energy_wh"] == 1_000_000

    aggregator.settle_day(DAY)
    daily = db.query_one(
        "SELECT energy_wh, is_final FROM daily WHERE meter_id=? AND day_start=?",
        (meter_id, DAY),
    )
    assert daily["is_final"] == 0


# ---------- 4. 批量写失败必须整批回滚 ----------


def test_write_batch_rolls_back_whole_batch_on_failure(isolated_db):
    """reading 与 reading_latest 必须同生共死，否则最新值与历史读数长期不一致。"""
    _, point_id = _meter_with_reading_point()
    other_point = create_point(
        db.query_one("SELECT device_id FROM point WHERE id=?", (point_id,))["device_id"],
        reg_offset=4, reg_count=1, data_type="u32", name="备用点位",
    )
    collector.write_batch([(point_id, 1000, 1.0, 0)])
    assert db.query_one("SELECT value FROM reading_latest WHERE point_id=?", (point_id,))[
        "value"
    ] == 1.0

    # 让 reading_latest 对备用点位写失败（真实场景是磁盘/锁/触发器级别的错误）
    db.execute(
        "CREATE TRIGGER block_latest BEFORE INSERT ON reading_latest "
        f"WHEN NEW.point_id = {int(other_point)} "
        "BEGIN SELECT RAISE(ABORT, '模拟写失败'); END"
    )
    collector.write_batch([(point_id, 2000, 5.0, 0), (other_point, 2000, 7.0, 0)])

    assert db.query_one("SELECT COUNT(*) AS cnt FROM reading WHERE ts=2000")["cnt"] == 0
    assert db.query_one("SELECT value FROM reading_latest WHERE point_id=?", (point_id,))[
        "value"
    ] == 1.0


# ---------- 5. 查询默认窗 ----------


def test_hourly_query_without_window_does_not_return_full_history(isolated_db):
    """不传 from/to 时只回最近几天，而不是整表。"""
    from api_data import data_service

    meter_id, _ = _meter_with_reading_point()
    now = int(datetime.now(tz=timezone.utc).timestamp())
    old_hour = now - 400 * 86400
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,?)",
        (meter_id, old_hour, 1_000_000, 1),
    )
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,?)",
        (meter_id, now - 3600, 2_000_000, 1),
    )
    rows = data_service.hourly()
    assert [row["energy_wh"] for row in rows] == [2_000_000]


# ---------- 6. 设备连接测试按协议分派 ----------


def test_device_test_dispatches_by_protocol(isolated_db, monkeypatch):
    """串口设备必须走串口客户端：旧实现固定用 TCP 去连 COM3:4800。"""
    node_id = create_node(name="A栋")
    device_id = db.execute(
        "INSERT INTO device (name, protocol, host, port, node_id, timeout_ms, retry) "
        "VALUES ('串口表', 'modbus_rtu_serial', 'COM3', 4800, ?, 300, 0)",
        (node_id,),
    )
    seen: dict[str, object] = {}

    class _FakeSerial:
        async def connect(self) -> None:
            seen["connected"] = True

        async def read_registers(self, *args: object) -> list[int]:
            return [1]

        async def close(self) -> None:
            seen["closed"] = True

    def _fake_client_for(device: dict) -> _FakeSerial:
        seen["protocol"] = device.get("protocol")
        seen["port"] = device.get("port")
        return _FakeSerial()

    monkeypatch.setattr(collector, "_client_for", _fake_client_for)

    from api_config import config_service

    result = run(config_service.test_device(int(device_id)))
    assert seen["protocol"] == "modbus_rtu_serial"
    assert seen["port"] == 4800
    assert seen["connected"] is True
    assert "串口已打开" in result["message"]


# ---------- 7. 周 / 月起点跟随可配置日切点 ----------


def test_week_and_month_start_follow_configured_day_boundary(isolated_db, monkeypatch):
    """改日切点后，周/月起点必须仍落在合法日切点上，否则周报比日报少一天。"""
    default_week = week_start(DAY)
    default_month = month_start(DAY)
    assert default_week % 86400 == 0
    assert default_month % 86400 == 0

    # 改成「本地 06:00 换日」：日切点平移到 UTC 22:00
    monkeypatch.setattr(settings, "TZ_OFFSET_HOURS", 8)
    monkeypatch.setattr(settings, "DAY_START_LOCAL_HOUR", 6)
    monkeypatch.setattr(settings, "DAY_BOUNDARY_UTC_HOUR", 22)

    shifted_week = week_start(DAY)
    shifted_month = month_start(DAY)
    # 起点必须正好落在新的日切点上（22:00 UTC = 本地 06:00）
    assert shifted_week % 86400 == 22 * 3600
    assert shifted_month % 86400 == 22 * 3600
    # 且仍然是同一周/同一月内的合法起点，不会跑到下一天
    assert shifted_week <= DAY < shifted_week + 7 * 86400
    assert shifted_month <= DAY < shifted_month + 32 * 86400


# ---------- 8. 人工录入覆盖真实读数要留痕 ----------


def test_manual_overwrite_of_raw_reading_is_audited(isolated_db):
    """覆盖现场采集值必须单独记一条审计，否则真实计量数据可被无痕改写。"""
    from manual import manual_service

    _, point_id = _meter_with_reading_point()
    ts = int(datetime.now(tz=timezone.utc).timestamp()) - 600
    add_reading(point_id, ts, 1000.0, quality=0)  # 现场采集值
    add_reading(point_id, ts, 1000.0, quality=0)

    manual_service.record({"point_id": point_id, "ts": ts, "value": 999.0, "operator": "张工"})
    assert db.query_one(
        "SELECT 1 FROM event_log WHERE code='MANUAL_OVERWRITE_RAW' AND ref_id=?", (point_id,)
    ) is not None


# ---------- 9. 事件日志节流 ----------


def test_log_event_throttled_folds_repeated_events(isolated_db):
    """故障状态下同类事件每秒一条会把 2 万条审计上限冲干净。"""
    for _ in range(5):
        db.log_event_throttled("warn", "collector", "READ_FAIL", "读取失败", 7)
    rows = db.query("SELECT message FROM event_log WHERE code='READ_FAIL'")
    assert len(rows) == 1

    # 窗口过后再写一条，并把期间折叠的次数写进消息
    db._log_throttle_at.clear()
    db.log_event_throttled("warn", "collector", "READ_FAIL", "读取失败", 7)
    messages = [row["message"] for row in db.query("SELECT message FROM event_log")]
    assert any("已折叠" in message for message in messages)


# ---------- 10. 索引与约束文案 ----------


def test_reading_ts_index_exists(isolated_db):
    """90 天清理按 ts 扫，主键前缀是 point_id，没索引就是全表扫。"""
    row = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_reading_ts'"
    )
    assert row is not None


def test_duplicate_meter_point_reports_duplicate_not_reference(isolated_db):
    """重复关联要被说成「已存在」，不能说成「仍被其它数据引用」。"""
    from fastapi.testclient import TestClient

    import auth
    import main

    meter_id, point_id = _meter_with_reading_point()
    config = auth.load_config()
    config["password"] = auth.hash_password("TestPass123")
    config["password_changed"] = True
    auth.save_config(config)

    with TestClient(main.app) as client:
        client.post("/api/login", json={"password": "TestPass123"})
        response = client.post(
            "/api/config/meter_point",
            json={"meter_id": meter_id, "point_id": point_id, "role": "reading"},
        )
    assert response.status_code == 409
    assert "已存在" in response.json()["error"]


# ---------- 11. 历史重算脚本 ----------


def test_recompute_script_rebuilds_hourly_and_drops_fake_zero_rows(isolated_db):
    """重算脚本要同时做两件事：按新口径补足电量、清掉旧口径留下的假 0 行。"""
    from scripts.recompute import main as recompute_main

    meter_id, point_id = _meter_with_reading_point()
    value: float = 800.0
    for index in range(2 * 3600 // 300 + 1):  # 两小时、300s 一条、每小时 1 kWh
        add_reading(point_id, DAY + index * 300, value)
        value += 1.0 / 12

    # 旧口径产物：第一小时少一个间隔；再插一行「掉线记 0」
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,?)",
        (meter_id, DAY, 917_000, 12),
    )
    db.execute(
        "INSERT INTO hourly (meter_id, hour_start, energy_wh, sample_cnt) VALUES (?,?,?,?)",
        (meter_id, DAY + 2 * 3600, 0, 0),
    )

    assert recompute_main(["--db", str(db.DB_PATH)]) == 0

    rows = db.query(
        "SELECT hour_start, energy_wh FROM hourly WHERE meter_id=? ORDER BY hour_start",
        (meter_id,),
    )
    assert [row["hour_start"] for row in rows] == [DAY, DAY + 3600]
    assert [row["energy_wh"] for row in rows] == [1_000_000, 1_000_000]
