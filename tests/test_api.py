"""ASGI 级 API 验证：认证、7 类资源 CRUD、试读/测试/重载、事件与数据查询。"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

import db
import httpx
import main
from helpers import (
    FakeGateway, add_reading, create_device, create_meter, create_node,
    create_point, create_tariff, fill_hourly, link_point, run,
)

DAY_START = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


async def _login(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post("/api/login", json={"password": "ems2026"})
    assert response.status_code == 200
    return response


def _envelope(response: httpx.Response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body, dict) and isinstance(body.get("ok"), bool)
    return body


def test_login_session_protection_and_logout_flow(ready_db):
    async def scenario():
        async with _client() as client:
            unauth = _envelope(await client.get("/api/config/node"), 401)
            assert unauth["ok"] is False and "error" in unauth

            session = _envelope(await client.get("/api/session"))
            assert session["data"]["authenticated"] is False

            bad = _envelope(await client.post(
                "/api/login", json={"password": "wrong"}
            ), 401)
            assert bad["ok"] is False and bad["error"] == "密码错误"

            logged_in = _envelope(await _login(client))
            assert "ok" in logged_in and "data" not in logged_in
            assert "ems_session" in client.cookies
            assert _envelope(await client.get("/api/session"))["data"]["authenticated"] is True
            assert _envelope(await client.get("/api/config/node"))["data"] == []

            assert _envelope(await client.post("/api/logout"))["ok"] is True
            assert _envelope(await client.get("/api/session"))["data"]["authenticated"] is False
            again = _envelope(await client.get("/api/config/node"), 401)
            assert again["ok"] is False

    run(scenario())


def test_logout_is_protected_endpoint_per_design(ready_db):
    """设计 §3.2 端点清单：POST /api/logout 需要认证。"""
    async def scenario():
        async with _client() as client:
            response = await client.post("/api/logout")
            assert response.status_code == 401, response.text
            assert response.json()["ok"] is False

    run(scenario())


def test_crud_for_all_seven_config_resources(ready_db):
    async def scenario():
        async with _client() as client:
            await _login(client)

            # node
            node = _envelope(await client.post("/api/config/node", json={"name": "A栋"}))["data"]
            assert node["name"] == "A栋"
            assert len(_envelope(await client.get("/api/config/node"))["data"]) == 1
            node = _envelope(await client.put(
                f"/api/config/node/{node['id']}", json={"name": "A栋-改"}
            ))["data"]
            assert node["name"] == "A栋-改"
            assert _envelope(await client.get(f"/api/config/node/{node['id']}"))["data"]["name"] == "A栋-改"

            # device
            device = _envelope(await client.post("/api/config/device", json={
                "name": "E90", "protocol": "modbus_tcp", "host": "127.0.0.1",
                "port": 502, "timeout_ms": 200, "retry": 0, "poll_interval": 60,
                "node_id": node["id"],
            }))["data"]
            assert device["port"] == 502
            device = _envelope(await client.put(
                f"/api/config/device/{device['id']}", json={"poll_interval": 30}
            ))["data"]
            assert device["poll_interval"] == 30
            assert len(_envelope(await client.get("/api/config/device"))["data"]) == 1

            # point
            point = _envelope(await client.post("/api/config/point", json={
                "name": "总电量", "device_id": device["id"], "slave_id": 1,
                "func_code": 3, "reg_offset": 0, "reg_count": 2,
                "data_type": "u32", "byte_order": "ABCD", "unit": "kWh",
            }))["data"]
            assert point["byte_order"] == "ABCD"
            point = _envelope(await client.put(
                f"/api/config/point/{point['id']}", json={"name": "总电量-改"}
            ))["data"]
            assert point["name"] == "总电量-改"
            assert len(_envelope(await client.get("/api/config/point"))["data"]) == 1

            # energy_category（初始化已有 3 条；v0.1.4 起只接受电 kWh）
            category = _envelope(await client.post("/api/config/energy_category", json={
                "name": "电-照明", "unit": "kWh",
            }))["data"]
            _envelope(await client.put(
                f"/api/config/energy_category/{category['id']}", json={"unit": "kWh"}
            ))
            assert len(_envelope(await client.get("/api/config/energy_category"))["data"]) == 4

            # meter
            meter = _envelope(await client.post("/api/config/meter", json={
                "name": "1号表", "node_id": node["id"], "category_id": 1,
                "purpose": "production",
            }))["data"]
            _envelope(await client.put(
                f"/api/config/meter/{meter['id']}", json={"purpose": "hvac"}
            ))
            assert _envelope(await client.get(f"/api/config/meter/{meter['id']}"))["data"]["purpose"] == "hvac"

            # meter_point（复合主键以 rowid 对外暴露）
            link = _envelope(await client.post("/api/config/meter_point", json={
                "meter_id": meter["id"], "point_id": point["id"], "role": "reading",
            }))["data"]
            assert link["role"] == "reading"
            link = _envelope(await client.put(
                f"/api/config/meter_point/{link['id']}", json={"role": "power"}
            ))["data"]
            assert link["role"] == "power"
            assert len(_envelope(await client.get("/api/config/meter_point"))["data"]) == 1

            # tariff
            tariff = _envelope(await client.post("/api/config/tariff", json={
                "name": "平价", "price_cent": 80, "is_default": 1,
            }))["data"]
            tariff = _envelope(await client.put(
                f"/api/config/tariff/{tariff['id']}", json={"price_cent": 90}
            ))["data"]
            assert tariff["price_cent"] == 90
            assert len(_envelope(await client.get("/api/config/tariff"))["data"]) == 1

            # delete：按外键依赖逆序删除，删除后再次查询应为 404
            for resource, item_id in [
                ("meter_point", link["id"]), ("meter", meter["id"]),
                ("point", point["id"]), ("device", device["id"]),
                ("energy_category", category["id"]), ("node", node["id"]),
                ("tariff", tariff["id"]),
            ]:
                assert _envelope(await client.delete(
                    f"/api/config/{resource}/{item_id}"
                ))["ok"] is True
                missing = _envelope(await client.get(
                    f"/api/config/{resource}/{item_id}"
                ), 404)
                assert missing["ok"] is False

    run(scenario())


def test_point_validation_via_api_rejects_all_invalid_cases(ready_db):
    async def scenario():
        async with _client() as client:
            await _login(client)
            device = _envelope(await client.post("/api/config/device", json={
                "name": "GW", "host": "127.0.0.1", "port": 502,
            }))["data"]
            base = {
                "name": "P", "device_id": device["id"], "slave_id": 1,
                "func_code": 3, "reg_offset": 100, "reg_count": 2,
                "data_type": "u32", "byte_order": "ABCD",
            }
            for override in [
                {"reg_offset": -1}, {"reg_offset": 65536},
                {"reg_count": 0}, {"reg_count": 126},
                {"reg_count": 4, "data_type": "u32"},
                {"func_code": 9}, {"byte_order": "AB"},
            ]:
                payload = {**base, **override}
                result = _envelope(await client.post("/api/config/point", json=payload), 400)
                assert result["ok"] is False and "error" in result

            good = _envelope(await client.post("/api/config/point", json=base))["data"]
            overlap = _envelope(await client.post("/api/config/point", json={
                **base, "name": "重叠", "reg_offset": 101,
            }), 400)
            assert overlap["ok"] is False
            assert "重叠" in overlap["error"]

            # 不同从站同区间合法
            _envelope(await client.post("/api/config/point", json={
                **base, "name": "另一从站", "slave_id": 2,
            }))

            # 更新时排除自身：保持原区间应成功
            self_update = _envelope(await client.put(
                f"/api/config/point/{good['id']}", json={"reg_offset": 100}
            ))
            assert self_update["ok"] is True

    run(scenario())


def test_point_read_device_test_reload_and_events(ready_db):
    async def scenario():
        async with FakeGateway(values=[0x0000, 0x4148]) as gateway:
            async with _client() as client:
                await _login(client)
                device = _envelope(await client.post("/api/config/device", json={
                    "name": "GW", "host": "127.0.0.1", "port": gateway.port,
                    "timeout_ms": 300, "retry": 0,
                }))["data"]
                point = _envelope(await client.post("/api/config/point", json={
                    "name": "试读点", "device_id": device["id"], "slave_id": 1,
                    "reg_offset": 0, "reg_count": 2, "data_type": "f32",
                    "byte_order": "CDAB",
                }))["data"]

                read = _envelope(await client.post(f"/api/point/{point['id']}/read"))["data"]
                assert read["raw_hex"] == "0000 4148"
                assert read["parsed_value"] == 12.5
                assert read["parsed_orders"]["CDAB"] == 12.5

                tested = _envelope(await client.post(f"/api/device/{device['id']}/test"))["data"]
                assert tested["ok"] is True
                assert tested["raw_regs"] == [0x0000, 0x4148]

                assert _envelope(await client.post("/api/collector/reload"))["data"] is True
                events = _envelope(await client.get(
                    "/api/events?source=api&limit=20"
                ))["data"]
                codes = {event["code"] for event in events}
                assert {"LOGIN", "COLLECTOR_RELOAD"}.issubset(codes)
                assert all(event["source"] == "api" for event in events)

                # 每次试读/测试使用全新短连接，事务 ID 从 0 开始
                assert gateway.last_request == struct.pack(
                    ">HHHBBHH", 0, 0, 6, 1, 3, 0, 2
                )

    run(scenario())


def test_latest_hourly_daily_and_settle_endpoints(ready_db):
    from aggregation import aggregator

    node_id = create_node(name="A栋")
    device_id = create_device()
    point_id = create_point(device_id, reg_offset=0, reg_count=1, data_type="u16")
    power_id = create_point(device_id, reg_offset=1, reg_count=1,
                            data_type="u16", point_role="power")
    meter_id = create_meter(node_id, purpose="production")
    link_point(meter_id, point_id, "reading")
    link_point(meter_id, power_id, "power")
    create_tariff(price_cent=80)
    add_reading(point_id, DAY_START, 100.0)
    add_reading(point_id, DAY_START + 1800, 101.234)
    add_reading(power_id, DAY_START + 600, 250.0)
    db.execute(
        "INSERT INTO reading_latest (point_id, ts, value, quality) VALUES (?,?,?,0)",
        (point_id, DAY_START + 1800, 101.234),
    )

    async def scenario():
        async with _client() as client:
            await _login(client)

            latest = _envelope(await client.get(f"/api/latest?point_id={point_id}"))["data"]
            assert len(latest) == 1
            assert latest[0]["value"] == 101.234
            assert latest[0]["name"] == "总电表电量"
            assert _envelope(await client.get(f"/api/latest?meter_id={meter_id}"))["data"]

            aggregator.rollup_hour(DAY_START)
            hourly = _envelope(await client.get(
                f"/api/hourly?meter_id={meter_id}&from={DAY_START}&to={DAY_START}&purpose=production"
            ))["data"]
            assert len(hourly) == 1
            assert hourly[0]["energy_wh"] == 1_234_000

            # 补齐其余 23 小时，构造「数据完整」的日，否则日结算不会定稿
            fill_hourly(meter_id, DAY_START, skip=(0,))
            settled = _envelope(await client.post(
                "/api/settle", json={"day_start": DAY_START}
            ))["data"]
            assert settled["settled"] is True
            assert settled["day_start"] == DAY_START
            assert settled["purposes"][0]["purpose"] == "production"

            daily = _envelope(await client.get(
                f"/api/daily?meter_id={meter_id}&is_final=1&node_id={node_id}"
            ))["data"]
            assert len(daily) == 1
            assert daily[0]["energy_wh"] == 1_234_000
            assert daily[0]["is_final"] == 1
            assert daily[0]["cost_cent"] == 99

    run(scenario())


def test_api_failure_status_codes_and_response_shape(ready_db):
    async def scenario():
        async with _client() as client:
            await _login(client)
            missing = _envelope(await client.get("/api/config/node/999999"), 404)
            assert missing["ok"] is False
            bad_resource = _envelope(await client.get("/api/config/unknown"), 400)
            assert bad_resource["ok"] is False
            # body 为通用 dict：模型级缺字段由 ConfigService 映射为 400
            invalid = _envelope(await client.post("/api/config/node", json={}), 400)
            assert invalid["ok"] is False and "error" in invalid
            # 路径参数类型错误由 FastAPI 请求校验处理器映射为 422
            bad_path = _envelope(await client.get("/api/config/node/not-an-int"), 422)
            assert bad_path["ok"] is False and "error" in bad_path
            async with _client() as anonymous:
                unauth_point_read = await anonymous.post("/api/point/1/read")
            assert unauth_point_read.status_code == 401
            assert unauth_point_read.json()["ok"] is False

    run(scenario())
