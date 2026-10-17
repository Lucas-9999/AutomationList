"""Estimate XLSX text clipping without modifying the file or claiming print QA."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import math
import re
import unicodedata

from .profiles import read_values
from .templates import MAIN, _package, _sheet_parts, _xml, _cells, _coordinate, _merges

MAX_REVIEW_CELLS = 20000
MAX_REVIEW_CHARACTERS = 1000000
BUILTIN_FORMATS = {0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 49: "@"}


def _true(value):
    return str(value).lower() in {"1", "true", "on"}


def _number(value, fallback):
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 <= result <= 1000000 else fallback
    except (ValueError, TypeError, OverflowError):
        return fallback


def _styles(parts):
    if "xl/styles.xml" not in parts:
        return [], [], {}, []
    root = _xml(parts["xl/styles.xml"])
    fonts = []
    for font in root.findall(f"{{{MAIN}}}fonts/{{{MAIN}}}font"):
        name, size = font.find(f"{{{MAIN}}}name"), font.find(f"{{{MAIN}}}sz")
        bold, italic = font.find(f"{{{MAIN}}}b"), font.find(f"{{{MAIN}}}i")
        fonts.append({"name": name.get("val") if name is not None else None,
                      "size_pt": _number(size.get("val"), None) if size is not None else None,
                      "bold": bold is not None and _true(bold.get("val", "1")),
                      "italic": italic is not None and _true(italic.get("val", "1")),
                      "availability": "not_checked"})
    custom = {}
    for item in root.findall(f"{{{MAIN}}}numFmts/{{{MAIN}}}numFmt"):
        identity = item.get("numFmtId")
        if identity is None or identity in custom:
            raise ValueError("布局检查：数字格式 ID 缺失或重复")
        custom[identity] = item.get("formatCode", "")
    return (fonts, root.findall(f"{{{MAIN}}}cellXfs/{{{MAIN}}}xf"), custom,
            root.findall(f"{{{MAIN}}}cellStyleXfs/{{{MAIN}}}xf"))


def _style(index, fonts, xfs, custom, bases):
    reasons = []
    xf, base = None, None
    try:
        number = int(index)
        if number < 0:
            raise IndexError
        xf = xfs[number]
    except (ValueError, IndexError, TypeError):
        reasons.append("缺少或无法解析有效单元格样式")
    if xf is not None and xf.get("xfId") is not None:
        try:
            base_index = int(xf.get("xfId"))
            if base_index < 0:
                raise IndexError
            base = bases[base_index]
        except (ValueError, IndexError):
            reasons.append("基础样式索引不可解析")

    def attribute(name, apply):
        if xf is not None and xf.get(apply) != "0" and xf.get(name) is not None:
            return xf.get(name)
        return base.get(name, "0") if base is not None else "0"

    try:
        font_index = int(attribute("fontId", "applyFont"))
        if font_index < 0:
            raise IndexError
        font = dict(fonts[font_index])
    except (ValueError, IndexError):
        font = {"name": None, "size_pt": None, "bold": False, "italic": False, "availability": "unknown"}
    if not font["name"] or not font["size_pt"]:
        reasons.append("字体名称或字号声明缺失；无法可靠测量，使用 11pt 保守回退值")
    alignment = xf.find(f"{{{MAIN}}}alignment") if xf is not None else None
    if (alignment is None or (xf is not None and xf.get("applyAlignment") == "0")) and base is not None:
        alignment = base.find(f"{{{MAIN}}}alignment")
    alignment = dict(alignment.attrib) if alignment is not None else {}
    format_id = attribute("numFmtId", "applyNumberFormat")
    try:
        format_code = custom.get(format_id, BUILTIN_FORMATS.get(int(format_id)))
    except ValueError:
        format_code = None
    return font, alignment, format_id, format_code, reasons


def _display(raw, cell, format_code):
    kind = cell.get("t")
    if kind in {"s", "inlineStr", "str", "e"}:
        return raw, "text" if kind != "e" else "error", []
    if kind == "b":
        return {"0": "FALSE", "1": "TRUE"}.get(raw, raw), "boolean", []
    if not raw:
        return "", "numeric", ["公式没有可显示缓存，未测量原公式表达式"]
    if format_code in {"General", "@"}:
        return raw, "numeric", []
    if not format_code:
        return raw, "numeric", ["内置或自定义数字显示格式未支持，显示文本未知"]
    # Deliberately small display-format subset; sections/conditions/dates and
    # Excel's fill/alignment directives are not guessed.
    if any(char in format_code for char in ";[]_*?\\") or re.search(r"[a-zA-Z]", re.sub(r'"[^"]*"', "", format_code)):
        return raw, "numeric", ["复杂数字/日期格式未解析，实际显示文本未知"]
    simplified = re.sub(r'"([^"]*)"', r"\1", format_code)
    pattern = re.fullmatch(r"([^0#]*)([#,]*0)(?:\.(0+))?(%?)([^0#]*)", simplified)
    if not pattern:
        return raw, "numeric", ["数字显示格式未解析，实际显示文本未知"]
    try:
        number = Decimal(raw)
        if not number.is_finite() or abs(number.as_tuple().exponent) > 100 or len(number.as_tuple().digits) > 256:
            raise InvalidOperation
        decimals = len(pattern[3] or "")
        with localcontext() as context:
            context.prec = 1024
            if pattern[4]:
                number *= 100
            number = number.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
            result = format(number, ("," if "," in pattern[2] else "") + "." + str(decimals) + "f")
        return pattern[1] + result + pattern[4] + pattern[5], "numeric", []
    except (InvalidOperation, ValueError, OverflowError):
        return raw, "numeric", ["数值或数字显示格式无法安全估算"]


def _width(text, size_pt, bold=False, italic=False):
    """Conservative character classes in em, not native glyph metrics."""
    units = 0.0
    for char in text:
        if unicodedata.combining(char) or char in {"\u200d", "\ufe0f"}:
            continue
        if char == "\t":
            units += 2.4
        elif char.isspace():
            units += 0.38
        elif unicodedata.east_asian_width(char) in {"W", "F", "A"}:
            units += 1.05
        elif char in "MW@%":
            units += 0.94
        elif char in "ilI.,:;'!|":
            units += 0.34
        elif char.isupper():
            units += 0.74
        elif char.isdigit():
            units += 0.64
        else:
            units += 0.64
    return units * size_pt * 4 / 3 * (1.07 if bold else 1) * (1.03 if italic else 1)


def _wrapped_lines(text, available, size, bold, italic):
    hard_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if available <= 0:
        return len(hard_lines)
    count = 0
    for hard in hard_lines:
        line_width, lines = 0.0, 1
        for token in re.findall(r"\S+\s*|\s+", hard):
            token_width = _width(token, size, bold, italic)
            if token_width <= available:
                if line_width and line_width + token_width > available:
                    lines += 1
                    line_width = 0
                line_width += token_width
            else:
                for char in token:
                    width = _width(char, size, bold, italic)
                    if line_width and line_width + width > available:
                        lines += 1
                        line_width = 0
                    line_width += width
        count += lines
    return count


def inspect_text_fit(content: bytes) -> dict:
    """Return estimates/unknowns for existing displayed cells; never change XLSX."""
    _, parts, _ = _package(content)
    sheets = _sheet_parts(parts)
    values = read_values(content, {"sheets": sheets})
    fonts, xfs, custom, bases = _styles(parts)
    shared_rich = set()
    if "xl/sharedStrings.xml" in parts:
        for index, entry in enumerate(_xml(parts["xl/sharedStrings.xml"]).findall(f"{{{MAIN}}}si")):
            if entry.find(f"{{{MAIN}}}r") is not None:
                shared_rich.add(str(index))
    report = {"sha256": hashlib.sha256(content).hexdigest(),
              "measurement_method": "conservative_character_estimate",
              "native_print_verified": False, "fonts_installed_verified": False,
              "cells": [], "risks": [], "unknown": [], "sheets": [],
              "warnings": ["字符宽度与列宽换算为保守估算；未使用 Excel 原生排版或打印引擎，也未验证字体安装。",
                           "检查不修改模板、字号或单元格；未发现风险不能视为打印验收通过。"]}
    characters = 0
    for sheet in sheets:
        root = _xml(parts[sheet["part"]])
        cell_nodes = _cells(root)
        rows = {int(row.get("r")): row for row in root.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row") if row.get("r", "").isdigit()}
        format_node = root.find(f"{{{MAIN}}}sheetFormatPr")
        defaults = dict(format_node.attrib) if format_node is not None else {}
        normal_size = fonts[0].get("size_pt") if fonts else None
        digit_width = 7 * (normal_size or 11) / 11
        default_width = _number(defaults.get("defaultColWidth"), 8.43)
        default_height = _number(defaults.get("defaultRowHeight"), 15) * 4 / 3
        columns = []
        for col in root.findall(f"{{{MAIN}}}cols/{{{MAIN}}}col"):
            try:
                first, last = int(col.get("min")), int(col.get("max"))
            except (ValueError, TypeError):
                raise ValueError("布局检查：列范围无效") from None
            if first < 1 or last > 16384 or first > last:
                raise ValueError("布局检查：列范围越界")
            columns.append((first, last, dict(col.attrib)))
        columns.sort(key=lambda item: item[0])
        if any(left[1] >= right[0] for left, right in zip(columns, columns[1:])):
            raise ValueError("布局检查：列宽定义重叠，无法可靠确认")
        report["sheets"].append({"name": sheet["name"], "default_column_width_chars": default_width,
                                 "default_row_height_pt": default_height * 3 / 4,
                                 "column_definitions": [data for _, _, data in columns]})
        occupied = {_coordinate(reference) for reference, value in values[sheet["name"]].items()
                    if value or cell_nodes[reference].find(f"{{{MAIN}}}f") is not None}

        def column_info(index):
            return next((data for first, last, data in columns if first <= index <= last), {})

        def pixels(data):
            if _true(data.get("hidden")):
                return 0
            width = _number(data.get("width"), default_width)
            return math.floor(width * digit_width + 5) if width >= 1 else math.floor(width * (digit_width + 5) + 0.5)

        def width_between(first, last):
            total = pixels({}) * (last - first + 1)
            for left, right, data in columns:
                overlap = max(0, min(last, right) - max(first, left) + 1)
                total += overlap * (pixels(data) - pixels({}))
            return total

        def height_between(first, last):
            total = default_height * (last - first + 1)
            for index, row in rows.items():
                if first <= index <= last:
                    actual = 0 if _true(row.get("hidden")) else _number(row.get("ht"), default_height * 3 / 4) * 4 / 3
                    total += actual - default_height
            return total

        merged = []
        for area in _merges(root):
            bounds = area.split(":")
            if len(bounds) != 2:
                raise ValueError("布局检查：合并区域无效")
            left, top = _coordinate(bounds[0])
            right, bottom = _coordinate(bounds[1])
            if left > right or top > bottom:
                raise ValueError("布局检查：合并区域顺序无效")
            merged.append((left, top, right, bottom, area))
        conditional = root.find(f"{{{MAIN}}}conditionalFormatting") is not None
        for reference, raw in values[sheet["name"]].items():
            cell = cell_nodes[reference]
            formula = cell.find(f"{{{MAIN}}}f")
            if not raw and formula is None:
                continue
            if len(report["cells"]) >= MAX_REVIEW_CELLS:
                raise ValueError("布局检查内容格数量超限")
            characters += len(raw)
            if characters > MAX_REVIEW_CHARACTERS:
                raise ValueError("布局检查文本总长度超限")
            col_index, row_index = _coordinate(reference)
            merge = next((m for m in merged if m[0] <= col_index <= m[2] and m[1] <= row_index <= m[3]), None)
            row = rows.get(row_index)
            row_attributes = dict(row.attrib) if row is not None else {}
            col_attributes = column_info(col_index)
            style_index = cell.get("s", row_attributes.get("s", col_attributes.get("style", "0")))
            font, alignment, fmt_id, fmt_code, reasons = _style(style_index, fonts, xfs, custom, bases)
            display, kind, display_reasons = _display(raw, cell, fmt_code)
            reasons.extend(display_reasons)
            if formula is not None:
                reasons.append("公式只使用当前存储的显示缓存；本检查未重算或确认缓存正确性")
            first, top, last, bottom = (merge[:4] if merge else (col_index, row_index, col_index, row_index))
            if merge and (col_index, row_index) != (first, top):
                reasons.append("合并区域非左上角存在内容，原生显示行为需确认")
            width = max(0, width_between(first, last) - 5)
            size = font["size_pt"] or 11
            indent = _number(alignment.get("indent"), 0)
            width = max(0, width - indent * _width("   ", size))
            height = max(0, height_between(top, bottom) - 2)
            wrap, shrink = _true(alignment.get("wrapText")), _true(alignment.get("shrinkToFit"))
            lines = display.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            estimated_width = max((_width(line, size, font["bold"], font["italic"]) for line in lines), default=0)
            line_count = _wrapped_lines(display, width, size, font["bold"], font["italic"]) if wrap else len(lines)
            estimated_height = line_count * size * 4 / 3 * 1.15
            horizontal_risk = estimated_width > width + max(1, width * 0.03) and not wrap and not shrink
            vertical_risk = estimated_height > height + max(2, size * 4 / 3 * 0.10)
            spill = ("merged_range" if merge else "numeric_cannot_spill" if kind != "text"
                     else "blocked_by_neighbor" if (last + 1, row_index) in occupied
                     else "unverified_blank_neighbor")
            unknown_reasons = [reason for reason in reasons if not reason.startswith("公式只使用")]
            if alignment.get("textRotation", "0") != "0":
                unknown_reasons.append("旋转/竖排文本未测量")
            if alignment.get("horizontal") in {"fill", "justify", "distributed", "centerContinuous"}:
                unknown_reasons.append("填充/分散等特殊对齐未模拟")
            if conditional:
                unknown_reasons.append("工作表含条件格式，未解析其字体和显示效果")
            stored_value = cell.find(f"{{{MAIN}}}v")
            if (cell.find(f"{{{MAIN}}}is/{{{MAIN}}}r") is not None
                    or (cell.get("t") == "s" and stored_value is not None and stored_value.text in shared_rich)):
                unknown_reasons.append("富文本分段字体未单独测量")
            hidden = width <= 0 or height <= 0 or _true(row_attributes.get("hidden"))
            risk = "within_estimate"
            if hidden:
                risk = "hidden_content"
            elif horizontal_risk or vertical_risk:
                risk = "possible_spill" if horizontal_risk and not vertical_risk and spill == "unverified_blank_neighbor" else "possible_clipping"
            elif shrink and estimated_width > width + 0.5:
                risk = "shrink_to_fit_unverified"
            if unknown_reasons:
                risk = "unknown"
            item = {"sheet": sheet["name"], "sheet_state": sheet["state"],
                    "cell": reference, "text": display, "text_length": len(display),
                    "raw": raw, "value_kind": kind, "formula": formula.text if formula is not None else None,
                    "formula_cache_verified": False if formula is not None else None,
                    "font": font, "style_index": style_index, "number_format_id": fmt_id, "number_format": fmt_code,
                    "alignment": alignment, "wrap_text": wrap, "shrink_to_fit": shrink, "merged_range": merge[4] if merge else None,
                    "available_width_px": round(width, 2), "estimated_text_width_px": round(estimated_width, 2),
                    "column_width_chars": _number(col_attributes.get("width"), default_width),
                    "available_height_px": round(height, 2), "estimated_text_height_px": round(estimated_height, 2),
                    "estimated_line_count": line_count, "row_height_pt": row_attributes.get("ht"),
                    "row_height_explicit": "ht" in row_attributes, "horizontal_risk": horizontal_risk,
                    "vertical_risk": vertical_risk, "risk": risk,
                    "spill_behavior": spill,
                    "clipping_priority": ("unknown" if unknown_reasons else "high" if
                                          (horizontal_risk and spill != "unverified_blank_neighbor") or
                                          (vertical_risk and wrap and "ht" in row_attributes) else "review"),
                    "measurement_status": "unknown" if unknown_reasons else "estimate",
                    "reasons": list(dict.fromkeys(reasons + unknown_reasons))}
            if horizontal_risk:
                item["reasons"].append("不换行文字估算超过格内宽度；相邻空格的原生溢出显示未模拟，需结合 spill_behavior 核对")
            if vertical_risk:
                item["reasons"].append("估算行数超过当前行高；自动行高及合并格原生行为未验证，可能被裁剪")
            if shrink and estimated_width > width:
                item["estimated_shrunk_font_pt"] = round(size * width / estimated_width, 2) if estimated_width else size
                item["reasons"].append("启用 shrinkToFit；未验证实际缩小字号与可读性")
            report["cells"].append(item)
            if risk != "within_estimate":
                report["risks"].append(item)
            if unknown_reasons:
                report["unknown"].append(item)
    report["summary"] = {"checked_cells": len(report["cells"]), "risk_cells": len(report["risks"]),
                         "unknown_cells": len(report["unknown"]),
                         "possible_clipping_cells": sum(item["horizontal_risk"] or item["vertical_risk"] for item in report["cells"])}
    report["status"] = "review_required" if report["risks"] else "no_risk_found_in_estimate"
    return report
