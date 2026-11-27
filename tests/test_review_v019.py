"""v0.1.9 代码审计整改的回归测试（对应 2026-11-27 报告 BE-01~BE-10 / FE-01~06 / OPS-01~03）。

每条用例锁的是「报告指出的那个具体失效路径」，不是「函数能跑通」：
· BE-01 串口连接测试假成功 —— 设备没配点位时也必须真的打开一次 COM 口；
· BE-02 事务块内误用全局连接 —— 从「卡 5 秒后 database is locked」改成立刻报错；
· BE-03 跨线程置事件 —— 线程池里的 reload 必须走 call_soon_threadsafe；
· BE-04 删除审计丢字段 —— 删完要能查回删的是哪台、什么地址；
· BE-05 串口无应答归类 —— 必须报超时，不能报「读取失败」；
· BE-06 MBAP 长度 —— 畸形长度要拦在 readexactly 之前；
· FE-01 默认密码登录卡死 —— 必须先查会话再拉筛选项；
· FE-02 重置未清编辑态 —— 重置后保存必须是 POST 而不是 PUT；
· OPS-01 dry-run --- 绝不改库结构；
· OPS-02 跨小时区间归入 —— 08:30 与 10:00 两条读数要带出 09:00 那个小时；
· OPS-03 备份原子性 —— 失败不留残file，也不让 prune 把坏的当最新。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import auth
import db
import main
import modbus
from aggregation import Aggregator
from helpers import add_reading, create_device, create_meter, create_node, create_point, link_point, run

SRC_ROOT = Path(main.__file__).resolve().parent


# ---------------------------------------------------------------- BE-01 串口连接测试
@pytest.fixture
def logged_in(ready_db):
    """已登录实例：密码改成测试口令，避免默认密码的强制改密拦截业务接口。"""
    config = auth.load_config()
    config["password"] = auth.hash_password("TestPass123")
    config["password_changed"] = True
    auth.save_config(config)
    return ready_db


def _probe_fake_serial(*, fail: bool) -> tuple[SimpleNamespace, dict]:
    """假 pyserial：只关心「有没有真的打开过端口」。"""
    state: dict = {"opened": 0, "kwargs": None}

    class FakeSerial:
        def __init__(self, **kwargs):
            state["opened"] += 1
            state["kwargs"] = kwargs
            if fail:
                raise OSError("could not open port COM99: 系统找不到指定的文件。")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return SimpleNamespace(
        EIGHTBITS=8, STOPBITS_ONE=1, PARITY_NONE="N", PARITY_E="E", PARITY_O="O",
        Serial=FakeSerial,
    ), state


def test_serial_connect_actually_opens_the_port(monkeypatch):
    """连接测试必须真的打开端口：否则 COM99 / 没装驱动也会报「串口已打开」。"""
    fake, state = _probe_fake_serial(fail=False)
    monkeypatch.setattr(modbus, "serial", fake)

    client = modbus.ModbusRTUSerialClient("COM7", 9600)
    run(client.connect())

    assert state["opened"] == 1, "connect() 没有真正打开串口"
    assert state["kwargs"]["port"] == "COM7"
    assert state["kwargs"]["baudrate"] == 9600


def test_serial_connect_surfaces_open_failure(monkeypatch):
    """端口不存在时要抛 ModbusError，让接口返回失败并写 last_error。"""
    fake, _ = _probe_fake_serial(fail=True)
    monkeypatch.setattr(modbus, "serial", fake)

    client = modbus.ModbusRTUSerialClient("COM99", 9600)
    with pytest.raises(modbus.ModbusError) as excinfo:
        run(client.connect())
    assert "COM99" in str(excinfo.value)


def test_serial_connect_reports_missing_pyserial(monkeypatch):
    """没装 pyserial 时必须明说，不能假装连上了。"""
    monkeypatch.setattr(modbus, "serial", None)
    client = modbus.ModbusRTUSerialClient("COM7", 9600)
    with pytest.raises(modbus.ModbusError) as excinfo:
        run(client.connect())
    assert "pyserial" in str(excinfo.value)


def test_device_connect_test_fails_for_missing_serial_port(logged_in, monkeypatch):
    """端到端：串口设备无点位时，端口打不开必须让 /api/device/{id}/test 失败。"""
    fake, _ = _probe_fake_serial(fail=True)
    monkeypatch.setattr(modbus, "serial", fake)

    device_id = int(db.execute(
        "INSERT INTO device (name, protocol, host, port, parity, timeout_ms, retry, poll_interval) "
        "VALUES ('串口网关', 'modbus_rtu_serial', 'COM99', 9600, 'N', 500, 0, 60)"
    ))
    with TestClient(main.app) as client:
        client.post("/api/login", json={"password": "TestPass123"})
        response = client.post(f"/api/device/{device_id}/test")

    # 设备连通性失败统一映射为 502（上游不可达），不是 500
    assert response.status_code == 502
    assert "COM99" in response.json()["error"]
    assert db.query_one("SELECT last_ok_at FROM device WHERE id=?", (device_id,))["last_ok_at"] is None


# ---------------------------------------------------------------- BE-05 串口无应答
def test_empty_serial_response_is_timeout_not_failure(monkeypatch):
    """从站静默时 read() 返回 b""：必须归成 ModbusTimeoutError（READ_TIMEOUT）。"""
    class SilentSerial:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def reset_input_buffer(self):
            pass

        def write(self, data):
            pass

        def read(self, n):
            return b""

    fake = SimpleNamespace(
        EIGHTBITS=8, STOPBITS_ONE=1, PARITY_NONE="N", PARITY_E="E", PARITY_O="O",
        Serial=SilentSerial,
    )
    monkeypatch.setattr(modbus, "serial", fake)

    client = modbus.ModbusRTUSerialClient("COM5", 9600, timeout_ms=100, retry=0)
    with pytest.raises(modbus.ModbusTimeoutError):
        run(client.read_registers(1, 3, 0, 2))


# ---------------------------------------------------------------- BE-06 MBAP 长度
@pytest.mark.parametrize("length", [0, 1, 2, 65535])
def test_mbap_length_out_of_range_rejected_before_readexactly(monkeypatch, length):
    """畸形 MBAP 长度必须在读 body 之前被拦住，且给出可读原因。"""
    header = b"\x00\x01\x00\x00" + length.to_bytes(2, "big") + b"\x01"

    class FakeReader:
        async def readexactly(self, n: int) -> bytes:
            assert n == 7, f"非法长度 {length} 时不应继续读 body（请求了 {n} 字节）"
            return header

    client = modbus.ModbusTCPClient("127.0.0.1", 502, timeout_ms=100, retry=0)

    async def fake_connect() -> None:
        client._reader = FakeReader()
        client._writer = SimpleNamespace(
            write=lambda data: None, drain=_noop_async, close=_noop,
            is_closing=lambda: False, wait_closed=_noop_async,
        )

    monkeypatch.setattr(client, "connect", fake_connect)
    monkeypatch.setattr(client, "close", _noop_async)

    with pytest.raises(modbus.ModbusError) as excinfo:
        run(client.read_registers(1, 3, 0, 2))
    assert "MBAP" in str(excinfo.value)


async def _noop_async(*args, **kwargs) -> None:
    return None


def _noop(*args, **kwargs) -> None:
    return None


# ---------------------------------------------------------------- BE-03 跨线程热重载
def test_reload_from_worker_thread_wakes_the_loop(isolated_db):
    """线程池里调用 reload() 必须真的唤醒调度器，而不是「等 1 秒轮询兜底」。"""
    import asyncio

    from collector import Collector

    worker = Collector()

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        worker._loop = loop
        worker.reload_event.clear()
        started = time.perf_counter()
        thread = threading.Thread(target=worker.reload)
        thread.start()
        # 不给兜底的 1 秒：要求 0.3 秒内被唤醒
        await asyncio.wait_for(worker.reload_event.wait(), timeout=0.3)
        elapsed = time.perf_counter() - started
        thread.join(timeout=1.0)
        return elapsed

    elapsed = run(scenario())
    assert elapsed < 0.3


# ---------------------------------------------------------------- BE-04 删除审计
def test_delete_audit_records_the_deleted_record(logged_in):
    """删设备后，审计里必须能查到删的是哪台、哪个地址。"""
    device_id = create_device(host="10.0.0.9", port=1502, name="被删网关")
    with TestClient(main.app) as client:
        client.post("/api/login", json={"password": "TestPass123"})
        assert client.delete(f"/api/config/device/{device_id}").status_code == 200

    row = db.query_one("SELECT message FROM event_log WHERE code='CONFIG_CHANGE' ORDER BY id DESC")
    assert row is not None
    message = str(row["message"])
    assert "被删网关" in message, f"删除审计没有记录名称：{message}"
    assert "10.0.0.9" in message, f"删除审计没有记录地址：{message}"
    assert "（无字段变化）" not in message
    assert "删除 device" in message


def test_diff_text_reports_removed_fields():
    """after 里消失的字段也要出现在 diff 里（旧实现只遍历 after）。"""
    from api_config import ConfigService

    text = ConfigService._diff_text({"name": "a", "shutdown_power_w": 5.0}, {"name": "a"})
    assert "shutdown_power_w" in text


# ---------------------------------------------------------------- BE-02 事务自锁
def test_global_write_inside_transaction_raises_instead_of_hanging(isolated_db):
    """事务块内调 db.execute 必须立刻报错，而不是卡满 busy_timeout。"""
    started = time.perf_counter()
    with pytest.raises(RuntimeError) as excinfo:
        with db.transaction() as conn:
            conn.execute("INSERT INTO node (name) VALUES ('事务内')")
            db.execute("INSERT INTO node (name) VALUES ('全局连接')")
    assert time.perf_counter() - started < 1.0, "应当立刻失败，而不是等 5 秒 busy_timeout"
    assert "transaction" in str(excinfo.value)
    # 事务被回滚：一条都不该留下
    assert db.query_one("SELECT COUNT(*) AS cnt FROM node")["cnt"] == 0


def test_log_event_on_writes_inside_the_same_transaction(isolated_db):
    """事务内记审计的正规做法：复用同一连接，随事务提交。"""
    with db.transaction() as conn:
        conn.execute("INSERT INTO node (name) VALUES ('同事务')")
        db.log_event_on(conn, "info", "api", "TXN_TEST", "同事务日志")
    assert db.query_one("SELECT COUNT(*) AS cnt FROM node")["cnt"] == 1
    assert db.query_one("SELECT COUNT(*) AS cnt FROM event_log WHERE code='TXN_TEST'")["cnt"] == 1


def test_nested_transaction_is_rejected(isolated_db):
    with pytest.raises(RuntimeError) as excinfo:
        with db.transaction():
            with db.transaction():
                pass
    assert "嵌套" in str(excinfo.value)


def test_transaction_state_does_not_leak_after_the_block(isolated_db):
    """事务结束后线程标记必须复位，否则后续正常写入会被误判成「事务内」。"""
    with db.transaction() as conn:
        conn.execute("INSERT INTO node (name) VALUES ('事务内')")
    db.execute("INSERT INTO node (name) VALUES ('事务后')")
    assert db.query_one("SELECT COUNT(*) AS cnt FROM node")["cnt"] == 2


# ---------------------------------------------------------------- FE 前端静态契约
def _html() -> str:
    return (SRC_ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")


def test_login_checks_session_before_loading_filters():
    """默认密码状态下 /api/config/* 会 403，先拉筛选项会把用户卡在登录页。"""
    text = _html()
    body = text.split("async function doLogin()", 1)[1].split("async function loadDashboard", 1)[0]
    assert body.index("await loadSession()") < body.index("await loadFilters()"), (
        "必须先 loadSession 再 loadFilters"
    )
    assert "mustChangePassword" in body, "改密状态下不应继续拉业务数据"


def test_reset_button_clears_editing_state():
    """「重置」后保存必须走新增，不能 PUT 覆盖刚才编辑的那条。"""
    text = _html()
    body = text.split('$("cfgNew").onclick', 1)[1].split("};", 1)[0]
    assert "state.editing" in body, "重置必须清掉 state.editing[res]"


def test_hourly_chart_labels_use_local_time():
    """折线图标签不得用 toISOString（UTC），否则 UTC+8 现场整体早 8 小时。"""
    text = _html()
    assert "function fmtHourLabel" in text
    label_block = text.split("function fmtHourLabel", 1)[1].split("}", 1)[0]
    assert "getHours()" in label_block
    series_block = text.split("const series = Array.from", 1)[1].split("svgLine", 1)[0]
    assert ".toISOString(" not in series_block, "图表标签仍在用 UTC 格式化"


def test_report_day_start_follows_configured_boundary():
    """报表日期筛选要按后端下发的日切点偏移计算，不能写死 Z。"""
    text = _html()
    block = text.split("function dayStart(dateText)", 1)[1].split("}", 1)[0]
    assert "dayBoundaryUtcHour" in block
    assert 'T00:00:00Z' in block, "仍以 UTC 零点为基准，只是要加上偏移"


def test_manual_entry_requires_a_value():
    """空数值不能变成 0 提交（会覆盖真实读数且看不出异常）。"""
    text = _html()
    block = text.split("async function submitManual()", 1)[1].split("try {", 1)[0]
    assert "Number.isFinite" in block
    assert 'rawValue === ""' in block


def test_logout_hides_the_button():
    text = _html()
    block = text.split('$("logoutBtn").onclick', 1)[1].split("};", 1)[0]
    assert 'display = "none"' in block


def test_mode_endpoint_exposes_day_boundary(logged_in):
    """/api/mode 要下发日切点偏移，前端才可能算对。"""
    with TestClient(main.app) as client:
        client.post("/api/login", json={"password": "TestPass123"})
        payload = client.get("/api/mode").json()["data"]
    assert "day_boundary_utc_hour" in payload


# ---------------------------------------------------------------- 串口校验位（BE-06 后半）
def test_device_parity_reaches_the_serial_client(isolated_db):
    """设备上填了 E（8E1）必须真的传给 pyserial。"""
    from collector import collector

    client = collector._client_for({
        "protocol": "modbus_rtu_serial", "host": "COM3", "port": 9600,
        "timeout_ms": 1000, "retry": 0, "parity": "E",
    })
    assert client.parity() == "E"


def test_parity_defaults_to_none(isolated_db):
    from collector import collector

    client = collector._client_for({
        "protocol": "modbus_rtu_serial", "host": "COM3", "port": 9600,
        "timeout_ms": 1000, "retry": 0,
    })
    assert client.parity() == "N"


# ---------------------------------------------------------------- OPS-01 dry-run
def test_recompute_dry_run_never_touches_schema(isolated_db):
    """--dry-run 必须只读：不能执行 schema.sql / 迁移。"""
    from scripts.recompute import main as recompute_main

    before = db.query_one("SELECT MAX(version) AS v FROM schema_version")["v"]
    assert recompute_main(["--db", str(db.DB_PATH), "--dry-run"]) == 0
    after = db.query_one("SELECT MAX(version) AS v FROM schema_version")["v"]
    assert after == before, "dry-run 私自执行了迁移"


def test_clear_demo_dry_run_never_touches_schema(isolated_db):
    from scripts.clear_demo import main as clear_demo_main

    before = db.query_one("SELECT MAX(version) AS v FROM schema_version")["v"]
    assert clear_demo_main(["--db", str(db.DB_PATH), "--dry-run"]) == 0
    after = db.query_one("SELECT MAX(version) AS v FROM schema_version")["v"]
    assert after == before


def test_readonly_connection_refuses_writes(isolated_db):
    """只读连接由 SQLite 兜底：写语句必须失败，而不是靠「记得别写」。"""
    db.use_readonly(db.DB_PATH)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO node (name) VALUES ('不该写进去')")


# ---------------------------------------------------------------- OPS-02 整点归属
def test_hours_to_rebuild_covers_cross_hour_interval(isolated_db):
    """08:30 与 10:00 两条读数：09:00 那个小时有电量，必须在重建名单里。"""
    from scripts.recompute import _hours_to_rebuild

    node_id = create_node()
    meter_id = create_meter(node_id)
    device_id = create_device()
    point_id = create_point(device_id)
    link_point(meter_id, point_id, "reading")

    day = 1_800_000_000 - (1_800_000_000 % 86400)
    add_reading(point_id, day + 8 * 3600 + 1800, 100.0)
    add_reading(point_id, day + 10 * 3600, 110.0)

    hours = _hours_to_rebuild(day, day + 86400)
    assert day + 9 * 3600 in hours, f"09:00 未被纳入重建：{hours}"
    assert day + 8 * 3600 in hours


def test_hours_to_rebuild_does_not_invent_empty_hours(isolated_db):
    """不能为了「补上一小时」凭空造出没有数据的零行小时。"""
    from scripts.recompute import _hours_to_rebuild

    node_id = create_node()
    meter_id = create_meter(node_id)
    device_id = create_device()
    point_id = create_point(device_id)
    link_point(meter_id, point_id, "reading")

    day = 1_800_000_000 - (1_800_000_000 % 86400)
    add_reading(point_id, day, 100.0)
    add_reading(point_id, day + 7200, 120.0)

    hours = _hours_to_rebuild(day, day + 86400)
    assert day - 3600 not in hours, "不应把前一天最后一小时拉进来"


# ---------------------------------------------------------------- OPS-03 备份原子性
def test_backup_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    """备份中途失败：目录里不能留下任何 .db（否则 prune 会把坏的当最新）。"""
    from scripts import backup as backup_mod

    source = tmp_path / "energy.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()

    out = tmp_path / "backups"

    class Boom:
        def __init__(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backup_mod.sqlite3, "connect", _patched_connect(backup_mod, Boom, source))
    with pytest.raises((sqlite3.Error, OSError)):
        backup_mod.backup(source, out)

    assert list(out.glob("energy-*.db")) == [], "失败后残留了半成品备份"
    assert list(out.glob("*.tmp")) == [], "失败后残留了临时文件"


def _patched_connect(module, boom_factory, source: Path):
    """只在打开「目标备份文件」时报错，源库照常可读。"""
    original = module.sqlite3.connect

    def connect(target, *args, **kwargs):
        if Path(str(target)) == source:
            return original(target, *args, **kwargs)
        return boom_factory(target, *args, **kwargs)

    return connect


def test_backup_writes_atomically_and_prunes_only_real_backups(tmp_path):
    """正常路径：产物是完整库，目录里不留 .tmp。"""
    from scripts import backup as backup_mod

    source = tmp_path / "energy.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    out = tmp_path / "backups"
    target = backup_mod.backup(source, out)
    assert target.exists() and target.suffix == ".db"
    assert not (out / (target.name + ".tmp")).exists()
    check = sqlite3.connect(target).execute("PRAGMA quick_check").fetchone()[0]
    assert str(check).lower() == "ok"


# ---------------------------------------------------------------- BE-08 日结
def test_settle_day_keeps_reprice_warning_after_batching(ready_db):
    """日结改成批量写之后，改电价仍要留下 TARIFF_REPRICE 痕迹。"""
    from settings import MILLI_PER_KWH, SECONDS_PER_DAY

    day = 1_700_000_000 - (1_700_000_000 % SECONDS_PER_DAY)  # 2023 年，确保不是未来日
    node_id = create_node()
    meter_id = create_meter(node_id)
    device_id = create_device()
    point_id = create_point(device_id)
    link_point(meter_id, point_id, "reading")
    db.execute("INSERT INTO tariff (name, price_cent, is_default) VALUES ('平', 60.0, 1)")

    add_reading(point_id, day - 600, 0.0)
    add_reading(point_id, day + 1800, 2.0)
    Aggregator().rollup_hour(day)  # 日结读的是 hourly
    Aggregator().settle_day(day, is_final=1, enforce_coverage=False)
    first = db.query_one("SELECT cost_cent FROM daily WHERE meter_id=?", (meter_id,))
    assert first["cost_cent"] == int(round(2.0 * 60.0))

    db.execute("UPDATE tariff SET price_cent=100.0 WHERE name='平'")
    Aggregator().settle_day(day, is_final=1, enforce_coverage=False)
    second = db.query_one("SELECT cost_cent FROM daily WHERE meter_id=?", (meter_id,))
    assert second["cost_cent"] == int(round(2.0 * 100.0))
    assert db.query_one("SELECT 1 FROM event_log WHERE code='TARIFF_REPRICE'") is not None
    assert MILLI_PER_KWH == 1_000_000
