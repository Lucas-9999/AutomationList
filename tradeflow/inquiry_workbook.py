"""Read-only XLSX inquiry transcription; no technical interpretation or OCR."""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import PurePath
from zipfile import BadZipFile, ZipFile

from .profiles import read_values
from .templates import MAIN, _coordinate, _xml, inspect_template

MAX_INPUT_BYTES = 15 * 1024 * 1024
MAX_PARTS = 2000
MAX_PART_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_CONTENT_CELLS = 10000
MAX_BODY_CHARACTERS = 180000


def _preflight(content: bytes) -> None:
    if not isinstance(content, bytes) or not content:
        raise ValueError("询价工作簿必须为非空 XLSX bytes")
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("询价工作簿超过 15 MiB 输入上限")
    try:
        with ZipFile(BytesIO(content)) as package:
            infos = package.infolist()
            if len(infos) > MAX_PARTS:
                raise ValueError("询价工作簿 ZIP 部件数量超限")
            if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("询价工作簿 ZIP 解压总大小超限")
            if any(info.file_size > MAX_PART_BYTES for info in infos):
                raise ValueError("询价工作簿 ZIP 单部件大小超限")
            if any(info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 500 for info in infos):
                raise ValueError("询价工作簿 ZIP 压缩比超限")
    except (BadZipFile, RuntimeError, NotImplementedError):
        raise ValueError("询价文件不是可读取的 XLSX ZIP") from None


def _style_formats(package: ZipFile) -> list[dict]:
    if "xl/styles.xml" not in package.namelist():
        return []
    root = _xml(package.read("xl/styles.xml"))
    formats = {}
    for item in root.findall(f"{{{MAIN}}}numFmts/{{{MAIN}}}numFmt"):
        identity = item.get("numFmtId")
        if identity is None or identity in formats:
            raise ValueError("询价工作簿数字格式 ID 缺失或重复")
        formats[identity] = item.get("formatCode", "")
    return [{"number_format_id": item.get("numFmtId"),
             "number_format": formats.get(item.get("numFmtId"))}
            for item in root.findall(f"{{{MAIN}}}cellXfs/{{{MAIN}}}xf")]


def parse_inquiry_workbook(content: bytes, filename: str) -> dict:
    """Transcribe nonempty/formula cells with coordinates and exact source text.

    Formula cached values remain explicitly unverified. Limits reject the whole
    request, never silently remove later rows. Images and drawings are metadata
    only, and no external relationship is dereferenced.
    """
    if not isinstance(filename, str) or not filename.lower().endswith(".xlsx"):
        raise ValueError("询价工作簿解析仅支持 .xlsx")
    filename = PurePath(filename.replace("\\", "/")).name
    _preflight(content)
    inspection = inspect_template(content)
    values = read_values(content, inspection)
    warnings = list(inspection["warnings"])
    cells, sheets = [], []
    body_parts = ["XLSX 询价原始单元格转录。保留原文和单位，未做技术字段推断或公式重算。"]
    body_length = len(body_parts[0])

    def append_body(text: str):
        nonlocal body_length
        body_length += len(text) + 1
        if body_length > MAX_BODY_CHARACTERS:
            raise ValueError(f"询价正文超过 {MAX_BODY_CHARACTERS} 字符上限；请拆分完整业务范围，不会截断规格")
        body_parts.append(text)

    with ZipFile(BytesIO(content)) as package:
        formats = _style_formats(package)
        for sheet in inspection["sheets"]:
            formula_map = {item["cell"]: item for item in sheet["formulas"]}
            sheet_cells = []
            for coordinate in sorted(values[sheet["name"]], key=lambda ref: (_coordinate(ref)[1], _coordinate(ref)[0])):
                raw = values[sheet["name"]][coordinate]
                formula = formula_map.get(coordinate)
                if raw == "" and formula is None:
                    continue
                if len(cells) >= MAX_CONTENT_CELLS:
                    raise ValueError(f"询价有内容单元格超过 {MAX_CONTENT_CELLS} 格上限；不会截断明细")
                metadata = sheet["cells"][coordinate]
                cell = {"sheet": sheet["name"], "cell": coordinate, "raw": raw,
                        "data_type": metadata["type"], "style": metadata["style"],
                        "formula": formula["formula"] if formula else None,
                        "formula_attributes": formula["attributes"] if formula else None,
                        "cached_value": formula["cached_value"] if formula else None,
                        "cache_verified": False}
                if metadata["style"] is not None:
                    try:
                        index = int(metadata["style"])
                        if index < 0:
                            raise IndexError
                        cell.update(formats[index])
                    except (ValueError, IndexError):
                        raise ValueError("询价单元格样式索引无效：" + sheet["name"] + "!" + coordinate) from None
                cells.append(cell)
                sheet_cells.append(cell)
                location = sheet["name"] + "!" + coordinate
                if formula:
                    append_body(f"[{location}] 公式（未重算）：={formula['formula']}\n原文件缓存（未验证，不作为已重算事实）：\n{raw}")
                    if formula["cached_value"] is None:
                        warnings.append(location + ": 公式没有缓存，尚无计算结果")
                elif metadata["type"] == "e":
                    append_body(f"[{location}] Excel 错误值（不能作为技术参数）：\n{raw}")
                    warnings.append(location + ": 单元格为 Excel 错误值，需核对来源")
                else:
                    append_body(f"[{location}] 原值：\n{raw}")
                if cell.get("number_format"):
                    append_body(f"[{location}] 原数字格式（保留其中单位，未应用显示换算）：{cell['number_format']}")
            rows = sorted({_coordinate(item["cell"])[1] for item in sheet_cells})
            sheets.append({"name": sheet["name"], "state": sheet["state"],
                           "dimension": sheet["dimension"], "content_cell_count": len(sheet_cells),
                           "content_rows": rows, "formula_count": len(sheet["formulas"]),
                           "drawing_count": sheet["drawings"],
                           "legacy_drawing_count": sheet["legacy_drawings"],
                           "merged_ranges": sheet["merged_ranges"]})
            if sheet["state"] != "visible":
                warnings.append(sheet["name"] + ": 隐藏工作表内容也已保留，未自动排除")
        media = inspection["media_parts"]
        attachments = [{"filename": name.rsplit("/", 1)[-1], "part": name,
                        "sha256": hashlib.sha256(package.read(name)).hexdigest(),
                        "analysis_status": "not_analyzed", "source_type": "embedded_media"}
                       for name in media]
    has_drawings = any(sheet["drawing_count"] or sheet["legacy_drawing_count"] for sheet in sheets)
    other_objects = [name for name in inspection["parts"] if name.startswith(("xl/embeddings/", "xl/charts/", "xl/comments")) and not name.endswith("/")]
    if attachments or has_drawings or other_objects:
        warning = "not-analyzed：图片、技术图纸、图表、绘图文本及嵌入对象仅登记元信息，尚未进行 OCR 或视觉理解，不能视为已读懂"
        warnings.append(warning)
        append_body(warning)
    if not cells:
        warnings.append("没有可分析的单元格正文；不能用图片或文件名推断技术要求")
        append_body("没有可分析的单元格正文。")
    warnings.append("这里只转录单元格原始值；未应用内置日期/数字显示格式、批注或图形文字，不推断缺失字段")
    return {"subject": filename, "sender": "", "message_id": "", "body": "\n".join(body_parts),
            "sha256": inspection["sha256"], "source_type": "inquiry_workbook",
            "source": {"filename": filename, "source_type": "inquiry_workbook", "sheets": sheets,
                       "text_analysis_available": bool(cells), "other_object_parts": other_objects,
                       "external_relationships": inspection["external_relationships"]},
            "cells": cells, "attachments": attachments, "warnings": list(dict.fromkeys(warnings))}
