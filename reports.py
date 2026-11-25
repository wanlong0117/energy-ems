"""报表组装与导出：日/周/月共用一套数据口径，Excel(.xlsx) 与 PDF(.pdf) 导出。

Excel 用 openpyxl；PDF 用 reportlab 内置 STSong-Light CID 中文字体，A4 横版，
页眉“能耗管理系统 / Energy EMS”，页脚生成时间 + 页码。空区间返回空报表不报错。
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from aggregation import aggregator
from api_data import data_service

HEADERS: tuple[str, ...] = (
    "期间", "计量表", "用途", "节点", "电量(毫度)", "电费(分)", "峰值功率", "已结算",
)
KEYS: tuple[str, ...] = (
    "period", "meter_name", "purpose", "node_name",
    "energy_wh", "cost_cent", "peak_power", "final_text",
)
COLUMN_WIDTHS_MM: tuple[float, ...] = (32.0, 46.0, 28.0, 32.0, 30.0, 26.0, 30.0, 22.0)

KIND_TITLES: dict[str, str] = {
    "daily": "日能耗报表 / Daily Energy Report",
    "weekly": "周能耗报表 / Weekly Energy Report",
    "monthly": "月能耗报表 / Monthly Energy Report",
}


class ReportService:
    """报表服务：build() 组装真实数据，to_xlsx()/to_pdf() 生成文件字节。"""

    def build(self, kind: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """按 kind 读取日/周/月汇总（数据全部来自真实 API 服务层）。"""
        options: dict[str, Any] = dict(filters or {})
        if kind == "daily":
            raw: list[dict[str, Any]] = data_service.daily(
                meter_id=options.get("meter_id"),
                from_ts=options.get("from_ts"),
                to_ts=options.get("to_ts"),
                purpose=options.get("purpose"),
                node_id=options.get("node_id"),
                is_final=options.get("is_final"),
            )
            return self._normalize(raw, "daily")
        if kind == "weekly":
            raw = aggregator.weekly(
                meter_id=options.get("meter_id"),
                from_ts=options.get("from_ts"),
                to_ts=options.get("to_ts"),
                purpose=options.get("purpose"),
                node_id=options.get("node_id"),
                is_final=options.get("is_final"),
            )
            return self._normalize(raw, "weekly")
        if kind == "monthly":
            raw = aggregator.monthly(
                meter_id=options.get("meter_id"),
                from_ts=options.get("from_ts"),
                to_ts=options.get("to_ts"),
                purpose=options.get("purpose"),
                node_id=options.get("node_id"),
                is_final=options.get("is_final"),
            )
            return self._normalize(raw, "monthly")
        raise ValueError(f"不支持的报表类型: {kind}")

    @staticmethod
    def _normalize(raw: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        """把 API 行映射成导出用行；数值字段保持与 API 完全一致。"""
        rows: list[dict[str, Any]] = []
        for item in raw:
            if kind == "daily":
                ts: int = int(item.get("day_start", 0))
            else:
                ts = int(item.get("period_start", 0))
            period: str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if kind == "weekly":
                period = f"{period} 起 / ISO Week"
            elif kind == "monthly":
                period = period[:7]
            peak: Any = item.get("peak_power")
            rows.append(
                {
                    "period": period,
                    "meter_name": str(item.get("meter_name") or ""),
                    "purpose": str(item.get("purpose") or ""),
                    "node_name": str(item.get("node_name") or ""),
                    "energy_wh": int(item.get("energy_wh") or 0),
                    "cost_cent": int(item.get("cost_cent") or 0),
                    "peak_power": "" if peak is None else float(peak),
                    "final_text": "是" if int(item.get("is_final") or 0) == 1 else "否",
                }
            )
        return rows

    @staticmethod
    def _totals(rows: list[dict[str, Any]]) -> tuple[int, int]:
        return (
            sum(int(row.get("energy_wh") or 0) for row in rows),
            sum(int(row.get("cost_cent") or 0) for row in rows),
        )

    def to_xlsx(self, rows: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> bytes:
        """openpyxl 生成 .xlsx：中文表头 + 合计行；空区间生成仅表头的空报表。"""
        info: dict[str, Any] = dict(meta or {})
        title: str = str(info.get("title", KIND_TITLES.get(str(info.get("kind", "daily")), "能耗报表")))
        generated: str = str(info.get("generated_at", self._generated_at()))

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "能耗报表"
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
        sheet.cell(row=1, column=1, value=title).font = Font(bold=True, size=14)
        sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(HEADERS))
        sheet.cell(row=2, column=1, value=f"生成时间: {generated}")
        sheet.append([])

        header_row: int = sheet.max_row + 1
        for column, header in enumerate(HEADERS, start=1):
            cell = sheet.cell(row=header_row, column=column, value=header)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(fill_type="solid", fgColor="3367D6")
            cell.alignment = Alignment(horizontal="center")

        for row in rows:
            sheet.append([row.get(key, "") for key in KEYS])

        energy_total, cost_total = self._totals(rows)
        total_row: list[Any] = ["合计", "", "", "", energy_total, cost_total, "", ""]
        # 必须先固定行号：sheet.max_row 会随循环内写入单元格递增，
        # 逐格取 max_row+1 会把 8 个字段散落到 8 行，导致合计值与标签分离。
        total_idx: int = sheet.max_row + 1
        for column, value in enumerate(total_row, start=1):
            cell = sheet.cell(row=total_idx, column=column, value=value)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(fill_type="solid", fgColor="E8F0FE")

        for index, width in enumerate((22, 28, 14, 18, 16, 14, 14, 12), start=1):
            sheet.column_dimensions[chr(64 + index)].width = width

        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    @staticmethod
    def _stsong_font() -> str:
        """注册并返回 reportlab 内置 CID 中文字体名（无需外部字体文件）。"""
        font_name: str = "STSong-Light"
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        return font_name

    @staticmethod
    def _generated_at() -> str:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def to_pdf(self, rows: list[dict[str, Any]], meta: dict[str, Any] | None = None) -> bytes:
        """reportlab 生成 A4 横版 PDF：页眉/页脚 + 中文表格 + 合计行。"""
        info: dict[str, Any] = dict(meta or {})
        title: str = str(info.get("title", KIND_TITLES.get(str(info.get("kind", "daily")), "能耗报表")))
        generated: str = str(info.get("generated_at", self._generated_at()))
        font_name: str = self._stsong_font()

        buffer = io.BytesIO()
        page = SimpleDocTemplate(
            buffer,
            pagesize=landscape(A4),
            leftMargin=12 * mm,
            rightMargin=12 * mm,
            topMargin=20 * mm,
            bottomMargin=16 * mm,
            title=title,
        )
        style = ParagraphStyle(
            "ems-cell", fontName=font_name, fontSize=8, leading=10, wordWrap="CJK"
        )
        header_style = ParagraphStyle(
            "ems-header", fontName=font_name, fontSize=8, leading=10,
            wordWrap="CJK", textColor=colors.white,
        )
        title_style = ParagraphStyle(
            "ems-title", fontName=font_name, fontSize=14, leading=18, alignment=1
        )

        def draw_page(canvas: Any, document: Any) -> None:
            canvas.saveState()
            canvas.setFont(font_name, 9)
            canvas.drawString(12 * mm, page.pagesize[1] - 12 * mm, "能耗管理系统 / Energy EMS")
            canvas.drawRightString(
                page.pagesize[0] - 12 * mm, 8 * mm,
                f"生成时间: {generated}    第 {document.page} 页",
            )
            canvas.setStrokeColor(colors.HexColor("#3367D6"))
            canvas.line(
                12 * mm, page.pagesize[1] - 14 * mm,
                page.pagesize[0] - 12 * mm, page.pagesize[1] - 14 * mm,
            )
            canvas.restoreState()

        def cell(text: Any) -> Paragraph:
            return Paragraph(str(text), style)

        def head_cell(text: Any) -> Paragraph:
            return Paragraph(str(text), header_style)

        data: list[list[Paragraph]] = [[head_cell(header) for header in HEADERS]]
        if rows:
            for row in rows:
                data.append([cell(row.get(key, "")) for key in KEYS])
        else:
            empty: list[Paragraph] = [cell("无数据 / No data")]
            empty.extend(cell("") for _ in range(len(HEADERS) - 1))
            data.append(empty)
        energy_total, cost_total = self._totals(rows)
        data.append(
            [cell(value) for value in ("合计", "", "", "", energy_total, cost_total, "", "")]
        )

        table = Table(data, colWidths=[width * mm for width in COLUMN_WIDTHS_MM], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3367D6")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA5B1")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8F0FE")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F6F8FB")]),
                ]
            )
        )
        # 表头中文需要用注册字体渲染，Paragraph 已使用 style；此处仅设置段落样式
        document_items: list[Any] = [
            Paragraph(title, title_style), Spacer(1, 4 * mm), table,
        ]
        page.build(document_items, onFirstPage=draw_page, onLaterPages=draw_page)
        return buffer.getvalue()


report_service: ReportService = ReportService()
