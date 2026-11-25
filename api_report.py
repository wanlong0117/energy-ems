"""报表导出 / 手动录入 / 模式切换 API：均需登录，统一 {ok,data,error} 响应。

GET  /api/report/export?kind=daily|weekly|monthly&fmt=xlsx|pdf  -> 文件流
POST /api/manual / GET /api/manual                                -> 人工录入与列表
POST /api/mode/switch                                             -> 写 config.data_mode
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError

import auth
from auth import require_auth
from manual import manual_service
from reports import KIND_TITLES, report_service
from schemas import ManualIn, ModeIn, fail, ok

router = APIRouter(prefix="/api", tags=["report"], dependencies=[Depends(require_auth)])

XLSX_MEDIA_TYPE: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MEDIA_TYPE: str = "application/pdf"


@router.get("/report/export")
def export_report(
    kind: str = "daily",
    fmt: str = "xlsx",
    meter_id: int | None = None,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    purpose: str | None = None,
    node_id: int | None = None,
    is_final: int | None = None,
) -> Any:
    """GET /api/report/export：按过滤项生成 xlsx/pdf 文件流（空区间返回空报表）。"""
    try:
        if kind not in ("daily", "weekly", "monthly"):
            return fail("kind 仅支持 daily/weekly/monthly", 400)
        if fmt not in ("xlsx", "pdf"):
            return fail("fmt 仅支持 xlsx/pdf", 400)
        filters: dict[str, Any] = {
            "meter_id": meter_id,
            "from_ts": from_,
            "to_ts": to,
            "purpose": purpose,
            "node_id": node_id,
            "is_final": is_final,
        }
        rows: list[dict[str, Any]] = report_service.build(kind, filters)
        meta: dict[str, Any] = {
            "kind": kind,
            "title": KIND_TITLES[kind],
            "filters": filters,
        }
        if fmt == "xlsx":
            content: bytes = report_service.to_xlsx(rows, meta)
            media_type: str = XLSX_MEDIA_TYPE
            extension: str = "xlsx"
        else:
            content = report_service.to_pdf(rows, meta)
            media_type = PDF_MEDIA_TYPE
            extension = "pdf"
        filename: str = quote(f"energy-{kind}.{extension}")
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )
    except ValueError as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return fail(f"导出报表失败: {exc}", 500)


@router.post("/manual")
def create_manual(body: ManualIn) -> Any:
    """POST /api/manual：录入人工读数（事务三表一致 + 回填重算）。"""
    try:
        return ok(manual_service.record(body.model_dump()))
    except LookupError as exc:
        return fail(str(exc), 404)
    except (ValueError, TypeError) as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return fail(f"手动录入失败: {exc}", 500)


@router.get("/manual")
def list_manual(point_id: int | None = None, limit: int = 100) -> Any:
    """GET /api/manual：最近录入列表。"""
    try:
        return ok(manual_service.list({"point_id": point_id, "limit": limit}))
    except (ValueError, TypeError) as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return fail(f"查询手动录入失败: {exc}", 500)


@router.post("/mode/switch")
def switch_mode(body: ModeIn) -> Any:
    """POST /api/mode/switch：写 config.data_mode，重启后生效。"""
    try:
        mode: str = auth.set_data_mode(body.data_mode)
        return ok({"data_mode": mode, "restart_required": True})
    except (ValueError, ValidationError) as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return fail(f"切换数据模式失败: {exc}", 500)
