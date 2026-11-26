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
from settings import MILLI_PER_KWH

HEADERS: tuple[str, ...] = (
    "期间", "计量表", "用途", "节点", "电量(kWh)", "电费(元)", "峰值功率", "覆盖小时",
    "停机小时", "已结算",
)
KEYS: tuple[str, ...] = (
    "period", "meter_name", "purpose", "node_name",
    "energy_kwh", "cost_yuan", "peak_power", "covered_hours", "shutdown_hours",
    "final_text",
)
COLUMN_WIDTHS_MM: tuple[float, ...] = (
    30.0, 44.0, 26.0, 30.0, 28.0, 24.0, 26.0, 22.0, 22.0, 20.0,
)

def footnote() -> str:
    """报表页脚说明：日切点可配，文案必须跟着配置走，不能写死 08:00。

    先让人判断这张表能不能用，再判断数字对不对——所以「已结算=否」的含义
    必须写在报表上，否则拿报表对账的人会把它当最终数。
    """
    from settings import DAY_START_LOCAL_HOUR

    start: int = int(DAY_START_LOCAL_HOUR)
    end: int = (start + 24) % 24
    return (
        f"说明：日汇总口径为本地 {start:02d}:00 至次日 {end:02d}:00（期间列为区间起点）；"
        "电量单位 kWh、电费单位 元，均已按两位小数折算；"
        "“覆盖小时”为该表当期有数据的小时数（满值 24），"
        "“停机小时”为功率低于停机阈值的时段，不计入完整度分母；"
        "“已结算=否”表示该期数据未达完整度门槛，仅供参考。"
    )


# 兼容旧引用；运行期请调用 footnote()，它会读取当前配置的日切点
FOOTNOTE: str = footnote()

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
                # 报表服务不套默认窗：时间范围由调用方（api_report）算好并显式传入，
                # 再收敛一次会把「不传范围查全量」的场景裁成最近 90 天
                default_days=None,
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
        """把 API 行映射成导出用行；电量折算 kWh、电费折算 元，保留两位小数。

        原始整数字段 energy_wh / cost_cent 一并保留，供合计行按毫度/分累加，
        避免逐行四舍五入后合计与明细对不上。
        """
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
            energy_wh: int = int(item.get("energy_wh") or 0)
            cost_cent: int = int(item.get("cost_cent") or 0)
            rows.append(
                {
                    "period": period,
                    "meter_name": str(item.get("meter_name") or ""),
                    "purpose": str(item.get("purpose") or ""),
                    "node_name": str(item.get("node_name") or ""),
                    "energy_wh": energy_wh,
                    "cost_cent": cost_cent,
                    "energy_kwh": round(energy_wh / MILLI_PER_KWH, 2),
                    "cost_yuan": round(cost_cent / 100.0, 2),
                    "peak_power": "" if peak is None else round(float(peak), 2),
                    # 周/月聚合没有小时覆盖概念，留空而不是补 24，避免看起来"齐全"
                    "covered_hours": (
                        int(item["covered_hours"])
                        if kind == "daily" and item.get("covered_hours") is not None
                        else ""
                    ),
                    # 停机小时：结算时从覆盖率分母里剔除的小时数，用来解释
                    # 「这天只有 2 小时数据为什么已结算」
                    "shutdown_hours": (
                        int(item["shutdown_hours"])
                        if kind == "daily" and item.get("shutdown_hours") is not None
                        else ""
                    ),
                    "final_text": "是" if int(item.get("is_final") or 0) == 1 else "否",
                }
            )
        return rows

    @staticmethod
    def _totals(rows: list[dict[str, Any]]) -> tuple[float, float]:
        """合计按原始毫度/分累加后一次性折算，保证与明细求和一致。"""
        energy_wh: int = sum(int(row.get("energy_wh") or 0) for row in rows)
        cost_cent: int = sum(int(row.get("cost_cent") or 0) for row in rows)
        return (
            round(energy_wh / MILLI_PER_KWH, 2),
            round(cost_cent / 100.0, 2),
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
        total_row: list[Any] = ["合计", "", "", "", energy_total, cost_total, "", "", "", ""]
        # 必须先固定行号：sheet.max_row 会随循环内写入单元格递增，
        # 逐格取 max_row+1 会把 8 个字段散落到 8 行，导致合计值与标签分离。
        total_idx: int = sheet.max_row + 1
        for column, value in enumerate(total_row, start=1):
            cell = sheet.cell(row=total_idx, column=column, value=value)
            cell.font = Font(bold=True)
            cell.fill = PatternFill(fill_type="solid", fgColor="E8F0FE")

        note_idx: int = sheet.max_row + 2
        sheet.merge_cells(
            start_row=note_idx, start_column=1, end_row=note_idx, end_column=len(HEADERS)
        )
        note_cell = sheet.cell(row=note_idx, column=1, value=footnote())
        note_cell.font = Font(size=9, color="666666")
        note_cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.row_dimensions[note_idx].height = 32

        # 列宽数量必须与 HEADERS 一致：少一个值，最后一列（已结算）会退回默认宽度，
        # 而前面各列的宽度看起来仍然正常，肉眼极难发现（v0.1.6 修）。
        for index, width in enumerate((22, 28, 14, 18, 16, 14, 14, 12, 10, 10), start=1):
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
            [cell(value) for value in ("合计", "", "", "", energy_total, cost_total, "", "", "", "")]
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
        note_style = ParagraphStyle(
            "ems-note", fontName=font_name, fontSize=7, leading=9,
            wordWrap="CJK", textColor=colors.HexColor("#666666"),
        )
        document_items: list[Any] = [
            Paragraph(title, title_style), Spacer(1, 4 * mm), table,
            Spacer(1, 3 * mm), Paragraph(footnote(), note_style),
        ]
        page.build(document_items, onFirstPage=draw_page, onLaterPages=draw_page)
        return buffer.getvalue()


report_service: ReportService = ReportService()
