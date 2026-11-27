"""collector.py 验证：合并读取、写库幂等、试读、失败日志与热生效。"""
from __future__ import annotations

import asyncio

import db
from collector import Collector, collector
from helpers import (
    FakeGateway, add_reading, create_device, create_point, run, wait_until,
)


def _point(point_id, slave_id=1, func_code=3, offset=0, count=2):
    return {
        "id": point_id, "slave_id": slave_id, "func_code": func_code,
        "reg_offset": offset, "reg_count": count,
    }


def test_merge_reads_joins_adjacent_and_overlapping_ranges(isolated_db):
    service = Collector()
    points = [
        _point(1, offset=0, count=2),
        _point(2, offset=2, count=2),   # 真正相邻
        _point(3, offset=6, count=2),   # 与上一组有空洞
        _point(4, offset=7, count=1),   # 与 id=3 重叠
    ]
    groups = service.merge_reads({"id": 1}, points)
    assert len(groups) == 2
    assert (groups[0]["offset"], groups[0]["count"]) == (0, 4)
    assert [p["id"] for p in groups[0]["points"]] == [1, 2]
    assert (groups[1]["offset"], groups[1]["count"]) == (6, 2)
    assert [p["id"] for p in groups[1]["points"]] == [3, 4]


def test_merge_reads_separates_slave_and_func_and_splits_over_125(isolated_db):
    service = Collector()
    points = [
        _point(1, slave_id=1, func_code=3, offset=0, count=2),
        _point(2, slave_id=2, func_code=3, offset=0, count=2),
        _point(3, slave_id=1, func_code=4, offset=0, count=2),
    ]
    groups = service.merge_reads({"id": 1}, points)
    assert {(g["slave_id"], g["func_code"]) for g in groups} == {
        (1, 3), (2, 3), (1, 4),
    }

    big_points = [
        _point(4, offset=0, count=100),
        _point(5, offset=50, count=100),
    ]
    groups = service.merge_reads({"id": 1}, big_points)
    assert [(g["offset"], g["count"]) for g in groups] == [(0, 100), (50, 100)]
    assert all(g["count"] <= 125 for g in groups)


def test_write_batch_is_idempotent_and_never_overwrites_latest_with_old_value(isolated_db):
    device_id = create_device()
    point_id = create_point(device_id)
    service = Collector()

    service.write_batch([(point_id, 1000, 1.0, 0)])
    service.write_batch([(point_id, 1000, 2.0, 0)])
    rows = db.query("SELECT value FROM reading WHERE point_id=? AND ts=1000", (point_id,))
    assert len(rows) == 1
    assert rows[0]["value"] == 2.0
    latest = db.query_one("SELECT ts, value FROM reading_latest WHERE point_id=?", (point_id,))
    assert (latest["ts"], latest["value"]) == (1000, 2.0)

    service.write_batch([(point_id, 900, 3.0, 0)])
    latest = db.query_one("SELECT ts, value FROM reading_latest WHERE point_id=?", (point_id,))
    assert (latest["ts"], latest["value"]) == (1000, 2.0)

    service.write_batch([(point_id, 1100, 4.0, 2)])
    latest = db.query_one(
        "SELECT ts, value, quality FROM reading_latest WHERE point_id=?", (point_id,)
    )
    assert (latest["ts"], latest["value"], latest["quality"]) == (1100, 4.0, 2)


def test_write_batch_logs_failure(isolated_db):
    service = Collector()
    service.write_batch([(1, 1000, 1.0)])  # 故意列数不匹配
    rows = db.query("SELECT code FROM event_log WHERE code='WRITE_FAIL'")
    assert len(rows) == 1


def test_probe_point_returns_raw_hex_and_all_byte_order_values(isolated_db):
    async def scenario():
        async with FakeGateway(values=[0x0000, 0x4148]) as gateway:
            device_id = create_device(port=gateway.port)
            point_id = create_point(
                device_id, reg_offset=0, reg_count=2,
                data_type="f32", byte_order="CDAB",
            )
            result = await collector.probe_point(point_id)
            assert result["point_id"] == point_id
            assert result["raw_hex"] == "0000 4148"
            assert result["raw_regs"] == [0x0000, 0x4148]
            assert result["parsed_value"] == 12.5
            assert result["quality"] == 0
            assert result["parsed_orders"]["CDAB"] == 12.5
            assert result["parsed_orders"]["ABCD"] != 12.5
            assert result["elapsed_ms"] >= 0
            assert gateway.connects == 1

    run(scenario())


def test_poll_device_writes_reading_and_latest(isolated_db):
    async def scenario():
        async with FakeGateway(values=[0x0064]) as gateway:
            device_id = create_device(port=gateway.port, poll_interval=60)
            point_id = create_point(device_id, reg_offset=0, reg_count=1, data_type="u16")
            device = dict(db.query_one("SELECT * FROM device WHERE id=?", (device_id,)))
            task = asyncio.create_task(collector.poll_device(device))
            try:
                assert await wait_until(lambda: db.query_one(
                    "SELECT 1 FROM reading WHERE point_id=?", (point_id,)
                ) is not None)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            row = db.query_one("SELECT value, quality FROM reading WHERE point_id=?", (point_id,))
            assert (row["value"], row["quality"]) == (100.0, 0)
            latest = db.query_one("SELECT value FROM reading_latest WHERE point_id=?", (point_id,))
            assert latest["value"] == 100.0
            updated = db.query_one("SELECT last_ok_at FROM device WHERE id=?", (device_id,))
            assert updated["last_ok_at"] is not None

    run(scenario())


def test_poll_device_logs_failure_and_out_of_range(isolated_db):
    async def scenario():
        device_id = create_device(host="127.0.0.1", port=1, timeout_ms=50, retry=0)
        point_id = create_point(device_id, reg_offset=0, reg_count=1, data_type="u16")
        device = dict(db.query_one("SELECT * FROM device WHERE id=?", (device_id,)))
        task = asyncio.create_task(collector.poll_device(device))
        try:
            # 连接被拒绝在不同平台会呈现为 READ_FAIL 或 READ_TIMEOUT，两者都算「失败留痕」。
            # 等待条件必须是「事件 + 设备状态都写完」：这两个写入是两次独立的线程池调用，
            # 只等其中一个就取消任务，取消点正好可能落在另一个之前（Windows 上调度更慢），
            # 断言随之偶发失败。
            def failed_round_done() -> bool:
                row = db.query_one("SELECT last_error FROM device WHERE id=?", (device_id,))
                event = db.query_one(
                    "SELECT 1 FROM event_log WHERE code IN ('READ_FAIL','READ_TIMEOUT')"
                )
                return bool(row and row["last_error"]) and event is not None

            assert await wait_until(failed_round_done)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert db.query_one("SELECT last_error FROM device WHERE id=?", (device_id,))["last_error"]

        async with FakeGateway(values=[0x0064]) as gateway:
            db.execute("UPDATE device SET port=? WHERE id=?", (gateway.port, device_id))
            db.execute("UPDATE point SET max_val=10, reg_offset=1 WHERE id=?", (point_id,))
            device = dict(db.query_one("SELECT * FROM device WHERE id=?", (device_id,)))
            task = asyncio.create_task(collector.poll_device(device))
            try:
                # 同理：越界日志先写、读数（quality=2）后写，只等日志会丢掉读数
                def out_of_range_stored() -> bool:
                    row = db.query_one(
                        "SELECT quality FROM reading WHERE point_id=? ORDER BY ts DESC LIMIT 1",
                        (point_id,),
                    )
                    return row is not None and int(row["quality"]) == 2

                assert await wait_until(out_of_range_stored)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            row = db.query_one(
                "SELECT quality FROM reading WHERE point_id=? ORDER BY ts DESC LIMIT 1",
                (point_id,),
            )
            assert row["quality"] == 2

    run(scenario())


def test_reload_sets_event_and_logs(isolated_db):
    collector.reload_event.clear()
    collector.reload()
    assert collector.reload_event.is_set()
    assert db.query_one("SELECT 1 FROM event_log WHERE code='COLLECTOR_RELOAD'") is not None
    collector.reload_event.clear()


def test_add_reading_helper_respects_primary_key(isolated_db):
    device_id = create_device()
    point_id = create_point(device_id)
    add_reading(point_id, 500, 1.0)
    add_reading(point_id, 500, 9.0)
    rows = db.query("SELECT value FROM reading WHERE point_id=?", (point_id,))
    assert len(rows) == 1
    assert rows[0]["value"] == 9.0
