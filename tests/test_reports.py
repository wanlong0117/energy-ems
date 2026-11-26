"""v2 报表导出验证：日/周/月 × xlsx/pdf、中文表头/合计、PDF 字节头与中文字体、空区间。"""
from __future__ import annotations

import io
from datetime import datetime, timezone

import db
import httpx
import pytest
from openpyxl import load_workbook
from reportlab.pdfbase import pdfmetrics

import main
from reports import HEADERS, report_service

from helpers import create_meter, create_node, run

DAY_START = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp())
DAY = 86400


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _login(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/login", json={"password": "ems2026"})
    assert resp.status_code == 200


def _seed_daily() -> None:
    """构造可被 daily/weekly/monthly 共用的真实汇总数据。"""
    node = create_node(name="A栋")
    meter = create_meter(node, name="1号表", purpose="production")
    for offset, energy, cost in ((0, 1_000_000, 80), (DAY, 2_000_000, 160)):
        db.execute(
            "INSERT OR REPLACE INTO daily "
            "(meter_id, day_start, energy_wh, peak_power, avg_power, cost_cent, is_final) "
            "VALUES (?,?,?,?,?,?,1)",
            (meter, DAY_START + offset, energy, 320.0, 150.0, cost),
        )


@pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
@pytest.mark.parametrize("fmt", ["xlsx", "pdf"])
def test_export_returns_nonempty_file_with_correct_headers(ready_db, kind, fmt):
    """Arrange: 真实 daily 数据 + 登录。Act: 导出 6 种组合。Assert: 200、非空、文件头/Content-Disposition 正确。"""
    _seed_daily()

    async def scenario():
        async with _client() as client:
            await _login(client)
            resp = await client.get(f"/api/report/export?kind={kind}&fmt={fmt}")
            assert resp.status_code == 200, resp.text
            assert len(resp.content) > 500, "导出文件不能为空"
            disposition = resp.headers.get("content-disposition", "")
            assert "attachment" in disposition
            assert f"energy-{kind}.{fmt}" in disposition
            if fmt == "xlsx":
                assert resp.headers["content-type"].startswith(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                assert resp.content[:2] == b"PK"  # xlsx = zip 容器
                wb = load_workbook(io.BytesIO(resp.content))
                assert wb.active.title == "能耗报表"
            else:
                assert resp.headers["content-type"].startswith("application/pdf")
                assert resp.content[:4] == b"%PDF"
                assert b"STSong-Light" in resp.content, "PDF 必须嵌入内置中文字体"
                assert "STSong-Light" in pdfmetrics.getRegisteredFontNames()

    run(scenario())


def test_xlsx_has_chinese_headers_and_total_row_matching_api(ready_db):
    """Arrange: 两天真实数据。Act: 导出 xlsx 并重开。Assert: 中文表头齐全、合计行数值等于 API 汇总。"""
    _seed_daily()
    captured: dict[str, object] = {}

    async def scenario():
        async with _client() as client:
            await _login(client)
            captured["xlsx"] = (await client.get(
                f"/api/report/export?kind=daily&fmt=xlsx&from={DAY_START}&to={DAY_START + DAY}"
            )).content
            captured["api_rows"] = (await client.get("/api/daily")).json()["data"]

    run(scenario())
    xlsx = captured["xlsx"]
    api_rows = captured["api_rows"]
    sheet = load_workbook(io.BytesIO(xlsx)).active
    grid = [list(row) for row in sheet.iter_rows(values_only=True)]

    header_idx = next(i for i, row in enumerate(grid) if "期间" in row)
    for header in HEADERS:
        assert header in grid[header_idx], f"缺中文表头: {header}"

    total_idx = next(i for i, row in enumerate(grid) if row and row[0] == "合计")
    # 报表电量折算 kWh、电费折算 元（两位小数），合计按原始毫度/分累加后一次性折算
    total_energy = round(sum(int(r["energy_wh"]) for r in api_rows) / 1_000_000, 2)
    total_cost = round(sum(int(r["cost_cent"]) for r in api_rows) / 100.0, 2)
    assert (total_energy, total_cost) == (3.0, 2.4)
    assert grid[total_idx][4] == total_energy, "合计行电量应等于 API 汇总折算后的 kWh"
    assert grid[total_idx][5] == total_cost, "合计行电费应等于 API 汇总折算后的元"


def test_empty_range_returns_valid_empty_report(ready_db):
    """Arrange: 无数据的未来区间。Act: xlsx/pdf 导出。Assert: 200 且为有效空报表，不报错。"""
    _seed_daily()
    future = DAY_START + 365 * DAY

    async def scenario():
        async with _client() as client:
            await _login(client)
            xlsx = await client.get(
                f"/api/report/export?kind=weekly&fmt=xlsx&from={future}&to={future + DAY}")
            assert xlsx.status_code == 200, xlsx.text
            wb = load_workbook(io.BytesIO(xlsx.content))
            assert "期间" in [c.value for row in wb.active.iter_rows() for c in row]
            pdf = await client.get(
                f"/api/report/export?kind=monthly&fmt=pdf&from={future}&to={future + DAY}")
            assert pdf.status_code == 200, pdf.text
            assert pdf.content[:4] == b"%PDF"
            assert len(pdf.content) > 500

    run(scenario())


def test_export_rejects_invalid_kind_and_fmt(ready_db):
    """Arrange/Act: 非法 kind/fmt。Assert: 400 且统一错误格式。"""
    async def scenario():
        async with _client() as client:
            await _login(client)
            for query in ("kind=yearly&fmt=xlsx", "kind=daily&fmt=csv"):
                resp = await client.get(f"/api/report/export?{query}")
                assert resp.status_code == 400, resp.text
                body = resp.json()
                assert body["ok"] is False and "error" in body

    run(scenario())


def test_report_service_build_uses_real_data_service(ready_db):
    """Arrange: 真实 daily。Act: ReportService.build 三类型。Assert: 数值与 API 一致、无 mock。"""
    _seed_daily()
    for kind in ("daily", "weekly", "monthly"):
        rows = report_service.build(kind)
        assert rows, f"{kind} 报表应有真实数据"
        assert sum(r["energy_wh"] for r in rows) == 3_000_000
        assert all(r["meter_name"] == "1号表" for r in rows)
