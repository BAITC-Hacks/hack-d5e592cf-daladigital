"""Readable, Unicode-safe protocol exports from approved data."""

from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any


LABELS = {"action": "Поручение", "decision": "Решение", "initiative": "Инициатива",
          "question": "Открытый вопрос", "risk": "Риск"}


def _time(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def _owner(item: dict[str, Any]) -> str:
    return item.get("owner") or "Требует уточнения"


def _due(item: dict[str, Any]) -> str:
    return item.get("due_text") or "Не указан"


def docx(meeting: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.shared import Cm, Pt

    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(2.3)
    section.bottom_margin = Cm(2.1)
    section.left_margin = section.right_margin = Cm(2.3)
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10)
    document.add_heading("Протокол совещания", 0)
    document.add_paragraph("АО «Самрук-Қазына»")
    document.add_paragraph(f"Тема: {meeting['title']}")
    document.add_paragraph(f"Дата: {meeting['meeting_date']} · Утверждён: {meeting['approved_at'] or '—'}")
    if meeting["participants"]:
        document.add_paragraph("Участники: " + ", ".join(meeting["participants"]))
    document.add_heading("Краткое содержание", level=1)
    document.add_paragraph(meeting["summary"] or "Саммари не сформировано")
    document.add_heading("Решения, инициативы и поручения", level=1)
    table = document.add_table(rows=1, cols=5)
    table.style = "Table Grid"
    for cell, label in zip(table.rows[0].cells, ("Тип", "Суть", "Ответственный", "Срок", "Источник")):
        cell.text = label
    for item in meeting["items"]:
        row = table.add_row().cells
        values = (LABELS.get(item["kind"], item["kind"]), item["title"], _owner(item), _due(item),
                  ", ".join(f"№{x}" for x in item["source_segment_ids"]))
        for cell, value in zip(row, values):
            cell.text = value
    document.add_heading("Транскрипт", level=1)
    for segment in meeting["segments"]:
        speaker = meeting["speakers"].get(segment["speaker_id"], segment["speaker_id"])
        document.add_paragraph(f"[{_time(segment['start'])}–{_time(segment['end'])}] №{segment['id']} · {speaker}: {segment['text']}")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _font_path() -> Path:
    import os

    candidates = [os.getenv("PROTOCOL_PDF_FONT", ""),
                  "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
                  "/Library/Fonts/Arial Unicode.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    raise RuntimeError("Не найден Unicode-шрифт для PDF. Задайте PROTOCOL_PDF_FONT")


def pdf(meeting: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether

    pdfmetrics.registerFont(TTFont("ProtocolUnicode", str(_font_path())))
    body = ParagraphStyle("body", fontName="ProtocolUnicode", fontSize=9, leading=13, spaceAfter=7)
    title = ParagraphStyle("title", parent=body, fontSize=17, leading=21, alignment=TA_CENTER, spaceAfter=16)
    heading = ParagraphStyle("heading", parent=body, fontSize=12, leading=16, spaceBefore=14, spaceAfter=8)
    tiny = ParagraphStyle("tiny", parent=body, fontSize=8, leading=11, spaceAfter=0)
    story: list[Any] = [Paragraph("Протокол совещания", title),
                        Paragraph("АО «Самрук-Қазына»", body),
                        Paragraph(f"Тема: {escape(meeting['title'])}", body),
                        Paragraph(f"Дата: {escape(meeting['meeting_date'])} · Утверждён: {escape(meeting['approved_at'] or '—')}", body)]
    if meeting["participants"]:
        story.append(Paragraph("Участники: " + escape(", ".join(meeting["participants"])), body))
    story.append(Paragraph("Краткое содержание", heading))
    for block in (meeting["summary"] or "Саммари не сформировано").split("\n\n"):
        story.append(Paragraph(escape(block).replace("\n", "<br/>"), body))
    story.append(Paragraph("Решения, инициативы и поручения", heading))
    cells = [[Paragraph(escape(label), tiny) for label in ("Тип", "Суть", "Ответственный", "Срок", "Источник")]]
    for item in meeting["items"]:
        values = (LABELS.get(item["kind"], item["kind"]), item["title"], _owner(item), _due(item),
                  ", ".join(f"№{x}" for x in item["source_segment_ids"]))
        cells.append([Paragraph(escape(value), tiny) for value in values])
    table = Table(cells, colWidths=[62, 183, 92, 91, 65], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF3")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C5CFD7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [table, Paragraph("Транскрипт", heading)]
    for segment in meeting["segments"]:
        speaker = meeting["speakers"].get(segment["speaker_id"], segment["speaker_id"])
        story.append(Paragraph(escape(
            f"[{_time(segment['start'])}–{_time(segment['end'])}] №{segment['id']} · {speaker}: {segment['text']}"
        ), body))
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=50, rightMargin=50,
                                 topMargin=45, bottomMargin=45, title="Протокол совещания")
    document.build(story)
    return buffer.getvalue()
