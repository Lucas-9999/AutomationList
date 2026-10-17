"""Derived, source-backed inquiry table rows; never mutate original text."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import re
import unicodedata

from .inquiry_document import WORD, _package
from .templates import _xml

VERSION = "inquiry-structure-v1"
MAX_INPUT_BYTES = 15 * 1024 * 1024
MAX_ROWS = 10000
MAX_TABLES = 1000


def _hash(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _normalized(value):
    return " ".join(value.split())


def _header(value):
    value = "".join(char for char in unicodedata.normalize("NFKD", value.casefold())
                    if not unicodedata.combining(char))
    value = re.sub(r"[._:/()\-]+", " ", value)
    return " ".join(value.split())


def _role(value):
    value = _header(value)
    if value in {"description", "complete description", "designation", "item description", "product description", "品名", "产品名称", "商品名称", "物料名称", "描述"}:
        return "description"
    if value in {"qty", "quantity", "quantite", "quantite consolidee", "数量", "询价数量"}:
        return "quantity"
    if value in {"supplier", "fournisseur", "供应商"}:
        return "supplier"
    if (value in {"reference", "item reference", "item ref", "item no", "part no", "part number", "pn", "customer reference", "supplier reference", "物料号", "料号", "客户料号", "供应商料号"}
            or value.endswith(" pn")
            or value == "ref" or value.startswith("ref ") or value.startswith("reference ")):
        return "reference"
    return "other"


class _Tables(HTMLParser):
    """Independent table stacks prevent outer cells absorbing nested rows."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack, self.hidden = [], [], 0

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "table":
            if len(self.tables) >= MAX_TABLES:
                raise ValueError("询价表格数量超限")
            if self.stack and self.stack[-1].get("cell") is not None:
                self.stack[-1]["cell"]["nested_table"] = True
            table = {"rows": [], "attributes": attrs, "table_index": len(self.tables) + 1,
                     "row": None, "cell": None, "malformed": False}
            self.tables.append(table)
            self.stack.append(table)
        elif self.stack:
            frame = self.stack[-1]
            if tag == "tr":
                if frame["row"] is not None:
                    frame["malformed"] = True
                frame["row"] = []
                frame["rows"].append(frame["row"])
                frame["cell"] = None
                if sum(len(table["rows"]) for table in self.tables) > MAX_ROWS:
                    raise ValueError("询价表格行数超限")
            elif tag in {"td", "th"}:
                if frame["row"] is None:
                    frame["malformed"] = True
                    frame["row"] = []
                    frame["rows"].append(frame["row"])
                frame["cell"] = {"raw_text": "", "attributes": attrs, "tag": tag}
                frame["row"].append(frame["cell"])
            elif tag in {"br", "p", "div", "li"} and frame["cell"] is not None:
                frame["cell"]["raw_text"] += "\n"

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.stack:
            return
        frame = self.stack[-1]
        if tag == "table":
            if frame["row"] is not None or frame["cell"] is not None:
                frame["malformed"] = True
            self.stack.pop()
        elif tag == "tr":
            frame["row"], frame["cell"] = None, None
        elif tag in {"td", "th"}:
            frame["cell"] = None
        elif tag in {"p", "div", "li"} and frame["cell"] is not None:
            frame["cell"]["raw_text"] += "\n"

    def handle_data(self, data):
        if not self.hidden and self.stack and self.stack[-1].get("cell") is not None:
            self.stack[-1]["cell"]["raw_text"] += data


def _html_tables(content):
    result = []
    message = BytesParser(policy=policy.default).parsebytes(content)
    for part_index, part in enumerate(message.walk()):
        if (part.get_content_type() != "text/html" or part.get_content_disposition() == "attachment"
                or part.get_filename()):
            continue
        raw = part.get_payload(decode=True) or b""
        text = raw.decode(part.get_content_charset() or "utf-8", errors="strict")
        parser = _Tables()
        parser.feed(text)
        parser.close()
        for unfinished in parser.stack:
            unfinished["malformed"] = True
        for table in parser.tables:
            table["id"] = f"mime-{part_index}-table-{table['table_index']}"
            table["source"] = {"type": "eml_html", "mime_part_index": part_index,
                               "table_index": table["table_index"], "part_sha256": _hash(raw)}
            result.append(table)
    return result


def _docx_tables(inquiry, content):
    by_table = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for block in inquiry.get("blocks", []):
        if "table_index" in block:
            by_table[block["table_index"]][block["row_index"]][block["cell_index"]].append(block)
    package = _package(content)
    document = _xml(package["word/document.xml"])
    xml_tables = list(document.iter(f"{{{WORD}}}tbl"))
    tables = []
    for index, rows in sorted(by_table.items()):
        records = []
        xml_table = xml_tables[index - 1] if index <= len(xml_tables) else None
        for row_index, columns in sorted(rows.items()):
            records.append([{"column_index": column_index, "raw_text": "\n".join(block["raw"] for block in blocks),
                             "blocks": blocks, "source": {"type": "docx", "part": "word/document.xml",
                                 "table_index": index, "row_index": row_index, "cell_index": column_index}}
                            for column_index, blocks in sorted(columns.items())])
        merged = bool(xml_table is not None and any(node.tag in {f"{{{WORD}}}gridSpan", f"{{{WORD}}}vMerge"} for node in xml_table.iter()))
        nested = xml_table is not None and sum(1 for _ in xml_table.iter(f"{{{WORD}}}tbl")) > 1
        tables.append({"id": f"docx-table-{index}", "rows": records, "merged": merged,
                       "nested": nested,
                       "source": {"type": "docx", "part": "word/document.xml", "table_index": index}})
    return tables


def _xlsx_tables(inquiry):
    sheets = defaultdict(lambda: defaultdict(list))
    for cell in inquiry.get("cells", []):
        match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", cell.get("cell", ""))
        if match is None:
            raise ValueError("询价单元格位置无效")
        column = 0
        for char in match.group(1):
            column = column * 26 + ord(char) - 64
        sheets[cell["sheet"]][int(match.group(2))].append({
            "column_index": column, "raw_text": cell["raw"], "cell": cell,
            "source": {"type": "xlsx", "sheet": cell["sheet"], "cell": cell["cell"],
                       "row_index": int(match.group(2)), "column_index": column}})
    info = {sheet["name"]: sheet for sheet in inquiry.get("source", {}).get("sheets", [])}
    return [{"id": f"xlsx-sheet-{name}", "rows": [sorted(cells, key=lambda cell: cell["column_index"])
                                                       for _, cells in sorted(rows.items())],
             "merged": bool(info.get(name, {}).get("merged_ranges")),
             "source": {"type": "xlsx", "sheet": name}}
            for name, rows in sheets.items()]


def _body_map(body):
    chars, starts, ends = [], [], []
    for match in re.finditer(r"\S+|\s+", body):
        if match.group().isspace():
            if chars:
                chars.append(" "); starts.append(match.start()); ends.append(match.end())
        else:
            for offset, char in enumerate(match.group()):
                chars.append(char); starts.append(match.start() + offset); ends.append(match.start() + offset + 1)
    if chars and chars[-1] == " ":
        chars.pop(); starts.pop(); ends.pop()
    return "".join(chars), starts, ends


def _quantity(raw):
    value = raw.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("数量必须是单个明确十进制原值")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("数量不是有效 Decimal") from None
    if not number.is_finite() or number <= 0:
        raise ValueError("数量必须是大于零的明确原值")
    return str(number)


def _column_letters(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _marked_span(body, cells):
    spans = []
    for cell in cells:
        if cell.get("source", {}).get("cell_missing_in_source"):
            continue
        if "blocks" in cell:
            for block in cell["blocks"]:
                marker = f"[word/document.xml:{block['path']}] 原文：\n{block['raw']}"
                start = body.find(marker)
                if start < 0 or body.find(marker, start + 1) >= 0:
                    return None
                spans.append((start, start + len(marker)))
        else:
            original = cell["cell"]
            marker = f"[{original['sheet']}!{original['cell']}] 原值：\n{original['raw']}"
            start = body.find(marker)
            if start < 0 or body.find(marker, start + 1) >= 0:
                return None
            spans.append((start, start + len(marker)))
    return (min(start for start, _ in spans), max(end for _, end in spans)) if spans else None


def enrich_inquiry_structure(inquiry: dict, content: bytes) -> dict:
    """Attach derived JSON-safe structure while preserving every input field."""
    result = deepcopy(inquiry)
    structure = {"version": VERSION, "rows": [], "tables": [], "warnings": [], "blockers": []}
    result["analysis_structure"] = structure
    body = inquiry.get("body", "")
    if not isinstance(content, bytes) or not content or len(content) > MAX_INPUT_BYTES:
        structure["blockers"].append("invalid_or_oversized_original")
        return result
    digest = _hash(content)
    if inquiry.get("sha256") != digest:
        structure["blockers"].append("original_sha256_mismatch")
        return result
    if not isinstance(body, str):
        structure["blockers"].append("invalid_original_body")
        return result
    try:
        if inquiry.get("source_type") == "inquiry_document":
            tables = _docx_tables(inquiry, content)
        elif inquiry.get("source_type") == "inquiry_workbook":
            tables = _xlsx_tables(inquiry)
        else:
            tables = _html_tables(content)
    except (ValueError, KeyError, IndexError, UnicodeError, LookupError, RecursionError):
        structure["blockers"].append("table_source_unreadable")
        return result
    normalized_body, body_starts, body_ends = _body_map(body)
    candidates = []
    for table in tables:
        metadata = {"id": table["id"], "source": {**table["source"], "sha256": digest},
                    "row_count": len(table["rows"]), "classification": "unmapped"}
        structure["tables"].append(metadata)
        header_index, headers, roles = None, {}, {}
        for index, cells in enumerate(table["rows"]):
            mapping = {cell.get("column_index", ci): _role(cell["raw_text"]) for ci, cell in enumerate(cells, 1)}
            if "description" in mapping.values() and ("quantity" in mapping.values() or "reference" in mapping.values()):
                header_index = index
                headers = {cell.get("column_index", ci): cell["raw_text"] for ci, cell in enumerate(cells, 1)}
                roles = mapping
                break
        if header_index is None:
            labels = {_header(row[0]["raw_text"]) for row in table["rows"] if row}
            mail_headers = {"from", "to", "date", "subject", "cc", "发件人", "收件人", "主题", "发送时间"}
            layout = len(table["rows"]) <= 1 or bool(labels) and labels <= mail_headers
            metadata["classification"] = "layout_or_unmapped" if layout else "unmapped_possible_products"
            structure["warnings"].append(table["id"] + "：无明确描述及数量或料号表头，未按商品解析")
            if not layout:
                structure["blockers"].append(table["id"] + ":unmapped_product_table")
            continue
        actual_header_row = table["rows"][header_index][0].get("source", {}).get("row_index", header_index + 1)
        metadata.update({"classification": "products", "header_row_index": actual_header_row,
                         "headers": [{"column_index": index, "raw_text": headers[index], "role": role}
                                     for index, role in sorted(roles.items())]})
        quantity_header_present = "quantity" in roles.values()
        if not quantity_header_present:
            metadata["quantity_not_provided"] = True
            structure["warnings"].append(table["id"] + "：该表未提供数量列，需业务确认")
        if (list(roles.values()).count("description") != 1 or list(roles.values()).count("quantity") > 1
                or table.get("merged") or table.get("malformed") or table.get("nested")
                or any(cell.get("attributes", {}).get("colspan", "1") != "1"
                       or cell.get("attributes", {}).get("rowspan", "1") != "1"
                       for cells in table["rows"] for cell in cells)):
            structure["blockers"].append(table["id"] + ":ambiguous_header_or_merged_table")
            continue
        for offset, cells in enumerate(table["rows"][header_index + 1:], header_index + 2):
            if not any(cell["raw_text"].strip() for cell in cells):
                continue
            cells = deepcopy(cells)
            offset = cells[0].get("source", {}).get("row_index", offset)
            if table["source"]["type"] == "xlsx":
                present_columns = {cell["column_index"] for cell in cells}
                for column in set(headers) - present_columns:
                    # Sparse OOXML omits truly blank cells. Retain an explicit
                    # empty structural slot; it is never a guessed quantity.
                    cells.append({"column_index": column, "raw_text": "", "cell": {},
                                  "source": {"type": "xlsx", "sheet": table["source"]["sheet"],
                                             "cell": _column_letters(column) + str(offset),
                                             "row_index": offset, "column_index": column,
                                             "cell_missing_in_source": True}})
                cells.sort(key=lambda cell: cell["column_index"])
            for ci, cell in enumerate(cells, 1):
                cell.setdefault("column_index", ci)
                cell["header"] = headers.get(cell["column_index"], "")
                cell["role"] = roles.get(cell["column_index"], "other")
                extracted_raw = cell["raw_text"]
                if table["source"]["type"] == "eml_html":
                    # p/div/br add structural separators, which are not part of
                    # a quantity value. Keep their hash in source metadata.
                    cell["raw_text"] = extracted_raw.strip()
                cell["source"] = {**table["source"], **cell.get("source", {}), "sha256": digest,
                                  "row_index": cell.get("source", {}).get("row_index", offset),
                                  "column_index": cell["column_index"], "raw_sha256": _hash(cell["raw_text"]),
                                  "extracted_raw_sha256": _hash(extracted_raw)}
            if (any(cell.get("nested_table") or cell.get("attributes", {}).get("colspan", "1") != "1"
                    or cell.get("attributes", {}).get("rowspan", "1") != "1" for cell in cells)
                    or (table["source"]["type"] != "xlsx" and {cell["column_index"] for cell in cells} - set(headers))):
                structure["blockers"].append(f"{table['id']}:row-{offset}:ambiguous_cells")
                continue
            if table["source"]["type"] != "xlsx" and len(cells) != len(headers):
                structure["blockers"].append(f"{table['id']}:row-{offset}:ambiguous_cells")
                continue
            product_name = next((cell["raw_text"] for cell in cells if cell["role"] == "description"), "")
            # A reference column can contain full technical requirements,
            # including material, prohibitions, coating and shape. Preserve all
            # description/reference cells in their original column order.
            description = "\n".join(cell["raw_text"] for cell in cells
                                    if (cell["role"] in {"description", "reference"}
                                        or _header(cell["header"]) in {"specification", "specifications", "规格", "规格型号", "型号", "standard", "标准", "grade", "等级", "material", "材质", "finish", "表色", "表面处理", "unit", "单位"})
                                    and cell["raw_text"].strip())
            quantity = next((cell["raw_text"] for cell in cells if cell["role"] == "quantity"), None)
            references = [cell["raw_text"] for cell in cells if cell["role"] == "reference"]
            try:
                if not product_name.strip() or (quantity_header_present and quantity is None) or _header(product_name) in {"total", "合计", "总计"}:
                    raise ValueError("不完整或汇总行")
                decimal_quantity = _quantity(quantity) if quantity_header_present else None
                if any(cell.get("cell", {}).get("formula") is not None or cell.get("cell", {}).get("data_type") == "e" for cell in cells):
                    raise ValueError("未核验公式或错误")
            except ValueError:
                structure["blockers"].append(f"{table['id']}:row-{offset}:unverified_quantity_or_product")
                continue
            candidate = {"id": "row_" + _hash(digest + ":" + table["id"] + ":" + str(offset)),
                         "table_id": table["id"], "row_index": offset, "cells": cells,
                         "description_text": description, "quantity_text": quantity,
                         "quantity_decimal": decimal_quantity, "reference_text": references[0] if references else None,
                         "source": {**table["source"], "sha256": digest, "row_index": offset}}
            if not quantity_header_present:
                candidate["source"]["quantity_not_provided"] = True
            if table["source"]["type"] == "eml_html":
                candidate["alignment_pattern"] = r"\s*".join(re.escape(_normalized(cell["raw_text"]))
                                                             for cell in cells if cell["raw_text"].strip())
            else:
                candidate["span"] = _marked_span(body, cells)
            candidates.append(candidate)
    expected = Counter(row.get("alignment_pattern") for row in candidates if "alignment_pattern" in row)
    cursor = 0
    for row in candidates:
        if "alignment_pattern" in row:
            pattern = row.pop("alignment_pattern")
            occurrences = [match.span() for match in re.finditer(pattern, normalized_body)]
            possible = [span for span in occurrences if body_starts[span[0]] >= cursor]
            if len(occurrences) != expected[pattern] or not possible:
                structure["blockers"].append(row["id"] + ":unaligned_or_ambiguous_body")
                continue
            start = body_starts[possible[0][0]]
            end = body_ends[possible[0][1] - 1]
        else:
            span = row.pop("span")
            if span is None:
                structure["blockers"].append(row["id"] + ":unaligned_body")
                continue
            start, end = span
        if start < cursor or start >= end:
            structure["blockers"].append(row["id"] + ":overlapping_body_rows")
            continue
        for cell in row["cells"]:
            for key in ("blocks", "cell", "attributes", "tag", "nested_table"):
                cell.pop(key, None)
        row.update({"start": start, "end": end, "text": body[start:end]})
        structure["rows"].append(row)
        cursor = end
    structure["warnings"] = list(dict.fromkeys(structure["warnings"]))
    structure["blockers"] = list(dict.fromkeys(structure["blockers"]))
    return result
