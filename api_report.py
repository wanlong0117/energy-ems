"""报表导出 / 手动录入 / 模式切换 API：均需登录，统一 {ok,data,error} 响应。

GET  /api/report/export?kind=daily|weekly|monthly&fmt=xlsx|pdf  -> 文件流
POST /api/manual / GET /api/manual                                -> 人工录入与列表
POST /api/mode/switch                                             -> 写 config.data_mode
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import ValidationError

import auth
import db
from auth import require_auth
from manual import manual_service
from reports import KIND_TITLES, report_service
from schemas import ManualIn, ModeIn, fail, ok
from settings import SECONDS_PER_DAY

router = APIRouter(prefix="/api", tags=["report"], dependencies=[Depends(require_auth)])

XLSX_MEDIA_TYPE: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MEDIA_TYPE: str = "application/pdf"

EXPORT_DEFAULT_DAYS: int = 31      # 不传时间范围时默认导出最近 31 天
EXPORT_MAX_SPAN_DAYS: int = 366    # 单次导出区间上限
EXPORT_MAX_ROWS: int = 20000       # 单次导出行数硬上限，避免整表读入内存


def _internal(code: str, action: str, exc: Exception) -> Any:
    """内部错误不回显原文：原文进事件日志，对外只给通用话术。"""
    db.log_event("error", "api", code, f"{action}失败: {type(exc).__name__}: {exc}")
    return fail(f"{action}失败，请查看事件日志", 500)


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
    """GET /api/report/export：按过滤项生成 xlsx/pdf 文件流（空区间返回空报表）。

    省略 from/to 时默认最近 31 天；区间跨度与命中行数均有硬上限，
    避免「不传参数就整表读入内存」把工作线程占死。
    """
    try:
        if kind not in ("daily", "weekly", "monthly"):
            return fail("kind 仅支持 daily/weekly/monthly", 400)
        if fmt not in ("xlsx", "pdf"):
            return fail("fmt 仅支持 xlsx/pdf", 400)
        to_ts: int = int(to) if to is not None else int(time.time())
        from_ts: int = (
            int(from_) if from_ is not None else to_ts - EXPORT_DEFAULT_DAYS * SECONDS_PER_DAY
        )
        if from_ts > to_ts:
            return fail("from 不能晚于 to", 400)
        if to_ts - from_ts > EXPORT_MAX_SPAN_DAYS * SECONDS_PER_DAY:
            return fail(f"导出区间不得超过 {EXPORT_MAX_SPAN_DAYS} 天，请缩小范围", 400)
        filters: dict[str, Any] = {
            "meter_id": meter_id,
            "from_ts": from_ts,
            "to_ts": to_ts,
            "purpose": purpose,
            "node_id": node_id,
            "is_final": is_final,
        }
        rows: list[dict[str, Any]] = report_service.build(kind, filters)
        if len(rows) > EXPORT_MAX_ROWS:
            return fail(
                f"命中 {len(rows)} 行，超过单次导出上限 {EXPORT_MAX_ROWS} 行，请缩小时间范围",
                400,
            )
        meta: dict[str, Any] = {
            "kind": kind,
            "title": KIND_TITLES[kind],
            "filters": filters,
            "from_ts": from_ts,
            "to_ts": to_ts,
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
        return _internal("REPORT_EXPORT_FAIL", "导出报表", exc)


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
        return _internal("MANUAL_CREATE_FAIL", "手动录入", exc)


@router.get("/manual")
def list_manual(point_id: int | None = None, limit: int = 100) -> Any:
    """GET /api/manual：最近录入列表。"""
    try:
        return ok(manual_service.list({"point_id": point_id, "limit": limit}))
    except (ValueError, TypeError) as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return _internal("MANUAL_LIST_FAIL", "查询手动录入", exc)


@router.post("/mode/switch")
def switch_mode(body: ModeIn) -> Any:
    """POST /api/mode/switch：写 config.data_mode，重启后生效。"""
    try:
        mode: str = auth.set_data_mode(body.data_mode)
        return ok({"data_mode": mode, "restart_required": True})
    except (ValueError, ValidationError) as exc:
        return fail(str(exc), 400)
    except Exception as exc:
        return _internal("MODE_SWITCH_FAIL", "切换数据模式", exc)
