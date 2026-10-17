"""Read-only source ingestion. Values and their original locations stay together."""
from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REQUIRED = ("product_name", "specification", "quantity", "unit", "surface_color", "grade")
ALIASES = {
    "product_name": {"品名", "产品名称", "商品名称", "物料名称"},
    "specification": {"规格", "规格型号", "型号规格", "物料规格"},
    "quantity": {"数量", "出货数量", "实发数量", "发货数量"},
    "unit": {"单位", "计量单位"},
    "surface_color": {"表色", "表面颜色", "表面处理", "颜色"},
    "grade": {"等级", "质量等级", "产品等级"},
    "line_id": {"行ID", "行号", "明细号"},
    "unit_price": {"单价", "销售单价"},
}


class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _decode(raw: bytes, charset=None) -> str:
    codecs = list(dict.fromkeys([charset, "utf-8-sig", "gb18030"]))
    for codec in codecs:
        if codec:
            try:
                return raw.decode(codec)
            except (UnicodeDecodeError, LookupError):
                pass
    raise ValueError("文本编码无法识别；需要 UTF-8、GB18030 或有效邮件 charset")


def parse_email(content: bytes, filename: str) -> dict:
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    result = {"subject": "", "sender": "", "message_id": "", "body": "",
              "sha256": hashlib.sha256(content).hexdigest(), "warnings": [], "attachments": []}
    if extension == ".txt":
        result["body"] = _decode(content)
    elif extension == ".eml":
        message = BytesParser(policy=policy.default).parsebytes(content)
        for name, header in (("subject", "Subject"), ("sender", "From"), ("message_id", "Message-ID")):
            result[name] = str(message.get(header, ""))
        text_parts, html_parts = [], []
        def visit(part):
            raw = part.get_payload(decode=True) or b""
            if part.get_content_disposition() == "attachment" or part.get_filename() or (not part.is_multipart() and part.get_content_type() not in {"text/plain", "text/html"}):
                if part.is_multipart():
                    raw = part.as_bytes(policy=policy.default)
                result["attachments"].append({"filename": part.get_filename() or "",
                    "content_type": part.get_content_type(), "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest()})
                return
            if part.is_multipart():
                for child in part.iter_parts():
                    visit(child)
                return
            if part.get_content_type() in {"text/plain", "text/html"}:
                text = _decode(raw, part.get_content_charset())
                if part.get_content_type() == "text/html":
                    parser = _TextHTML()
                    parser.feed(text)
                    html_parts.append("".join(parser.parts).strip())
                else:
                    text_parts.append(text)
        visit(message)
        result["body"] = "\n".join(text_parts or html_parts)
        if not text_parts and html_parts:
            result["warnings"].append("邮件正文从 HTML 提取；脚本与样式不执行且不进入正文")
        if result["attachments"]:
            result["warnings"].append("邮件含附件；仅记录附件元信息，附件需求尚未解析")
        if message.defects:
            result["warnings"].append("邮件结构存在解析缺陷，需核对原件")
    else:
        raise ValueError("询价只支持 .eml 或 .txt 文件")
    if not result["body"].strip():
        raise ValueError("邮件没有可分析的文本正文")
    return result


def _xml(archive, name):
    try:
        raw = archive.read(name)
        declaration_check = raw.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in declaration_check or b"<!ENTITY" in declaration_check:
            raise ValueError("工作簿 XML 包含不允许的实体声明")
        return ET.fromstring(raw)
    except (KeyError, ET.ParseError) as exc:
        raise ValueError("工作簿 XML 缺失或损坏: " + name) from exc


def _col(ref):
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", ref)
    if not match:
        raise ValueError("无效单元格坐标: " + ref)
    return match.group(1), int(match.group(2))


def _col_num(letters):
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value


def _read_sheet(archive, path, shared):
    root = _xml(archive, path)
    rows = {}
    total = 0
    for row in root.findall("s:sheetData/s:row", NS):
        for cell in row.findall("s:c", NS):
            total += 1
            if total > 500000:
                raise ValueError("工作表单元格超过解析上限")
            ref = cell.get("r", "")
            column, number = _col(ref)
            values = rows.setdefault(number, {})
            if column in values:
                raise ValueError("重复单元格坐标: " + ref)
            typ = cell.get("t", "")
            value = cell.find("s:v", NS)
            text = value.text if value is not None and value.text is not None else ""
            if typ == "s":
                try:
                    index = int(text)
                    if index < 0:
                        raise IndexError
                    text = shared[index]
                except (ValueError, IndexError) as exc:
                    raise ValueError("共享字符串索引无效: " + ref) from exc
            elif typ == "inlineStr":
                text = "".join(node.text or "" for node in cell.findall("s:is//s:t", NS))
            formula_node = cell.find("s:f", NS)
            values[column] = {"cell": ref, "raw": text, "type": typ,
                              "formula": cell.find("s:f", NS) is not None,
                              "cached": value is not None and value.text is not None,
                              "formula_text": formula_node.text if formula_node is not None else None}
    merges = [node.get("ref", "") for node in root.findall("s:mergeCells/s:mergeCell", NS)]
    return rows, merges


def _headers(row):
    entries = {}
    for column, cell in row.items():
        title = cell["raw"].strip()
        if title:
            entries.setdefault(title, []).append(column)
    return entries


def _infer(headers):
    found = {}
    for field, aliases in ALIASES.items():
        titles = [title for title in headers if title in aliases]
        if len(titles) > 1:
            raise ValueError("同一字段对应多个候选表头: " + field)
        if titles:
            found[field] = titles[0]
    return found


def _decimal(raw, field, cell):
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{field} 不是有效十进制数: {cell}") from exc
    if not value.is_finite() or (value <= 0 if field == "quantity" else value < 0):
        raise ValueError(f"{field} 必须是有限且有效的数值: {cell}")
    return str(value)


def parse_erp_xlsx(content: bytes, filename: str, mapping: dict | None = None) -> dict:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError("ERP 解析只支持 .xlsx 文件")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效 XLSX 文件") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(infos) > 2000 or sum(i.file_size for i in infos) > 100 * 1024 * 1024:
            raise ValueError("工作簿解压大小或文件数量超过上限")
        if len(set(names)) != len(names):
            raise ValueError("工作簿 ZIP 包含重复路径")
        for info in infos:
            if info.file_size > 30 * 1024 * 1024 or (info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 200):
                raise ValueError("工作簿压缩比例或单文件大小超过上限")
            if info.flag_bits & 1 or ".." in PurePosixPath(info.filename).parts or info.filename.startswith(("/", "\\")):
                raise ValueError("工作簿 ZIP 路径或加密状态不支持")
        workbook = _xml(archive, "xl/workbook.xml")
        relations = _xml(archive, "xl/_rels/workbook.xml.rels")
        targets = {}
        for relation in relations.findall("{" + REL_NS + "}Relationship"):
            if relation.get("TargetMode") == "External":
                continue
            target = relation.get("Target", "")
            path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
            if not path.startswith("xl/"):
                raise ValueError("工作簿关系指向非法路径")
            targets[relation.get("Id")] = path
        shared = []
        if "xl/sharedStrings.xml" in names:
            shared = ["".join(n.text or "" for n in item.findall(".//s:t", NS))
                      for item in _xml(archive, "xl/sharedStrings.xml").findall("s:si", NS)]
        sheets = {}
        for sheet in workbook.findall("s:sheets/s:sheet", NS):
            name = sheet.get("name", "")
            if name in sheets:
                raise ValueError("重复工作表名称")
            rid = sheet.get("{" + DOC_REL_NS + "}id")
            if rid not in targets:
                raise ValueError("工作表缺少本地关系")
            sheets[name] = targets[rid]
        if mapping is not None:
            if not isinstance(mapping, dict) or set(mapping) != {"sheet", "header_row", "columns"}:
                raise ValueError("mapping 必须包含 sheet、header_row、columns")
            if mapping["sheet"] not in sheets or type(mapping["header_row"]) is not int or mapping["header_row"] < 1:
                raise ValueError("mapping 工作表或表头行无效")
            fields = mapping["columns"]
            if not isinstance(fields, dict) or any(k not in ALIASES or not isinstance(v, str) or not v.strip() for k, v in fields.items()):
                raise ValueError("mapping 列字段无效")
            if len(set(fields.values())) != len(fields):
                raise ValueError("mapping 多个字段不能引用同一列")
            selected = mapping["sheet"], mapping["header_row"], fields
            rows, merges = _read_sheet(archive, sheets[selected[0]], shared)
        else:
            candidates = []
            for sheet, path in sheets.items():
                rows_here, merges_here = _read_sheet(archive, path, shared)
                for number in sorted(rows_here):
                    if number > 30:
                        break
                    headers = _headers(rows_here[number])
                    matches = sum(any(t in aliases for t in headers) for field, aliases in ALIASES.items() if field in REQUIRED)
                    if matches >= 3:
                        fields = _infer(headers)
                        candidates.append((sheet, number, fields, rows_here, merges_here))
            if len(candidates) != 1:
                raise ValueError("发现多个候选表/表头或未找到唯一明细表；请提供 mapping")
            sheet, number, fields, rows, merges = candidates[0]
            selected = sheet, number, fields
        sheet, header_row, fields = selected
        missing = set(REQUIRED) - set(fields)
        if missing:
            raise ValueError("缺失关键字段映射: " + ", ".join(sorted(missing)))
        headers = _headers(rows.get(header_row, {}))
        if any(len(cols) != 1 for cols in headers.values()):
            raise ValueError("表头重复，无法唯一定位列")
        if any(title not in headers for title in fields.values()):
            raise ValueError("mapping 中的原始列标题不存在")
        columns = {field: headers[title][0] for field, title in fields.items()}
        for header in rows.get(header_row, {}).values():
            if header["formula"]:
                raise ValueError("公式表头不能作为可靠字段名")
        warnings, items, skipped_rows = [], [], []
        data_rows = {}
        footer_labels = ("总金额", "合计", "总计", "商户地址", "打印时间", "备注：", "备注:", "制单人签名", "客户签名", "签名：", "签名:")
        for number, row in rows.items():
            if number <= header_row or not any(c["raw"].strip() or c["formula"] for c in row.values()):
                continue
            if any(c["formula"] and not c["cached"] for c in row.values()):
                raise ValueError(f"公式没有缓存值，不能可靠解析: {sheet} 第 {number} 行")
            has_product = any(row.get(columns[field], {}).get("raw", "").strip() for field in ("product_name", "specification"))
            labels = [c["raw"].strip() for c in row.values() if c["raw"].strip()]
            quantity_raw = row.get(columns["quantity"], {}).get("raw", "").strip()
            unit_raw = row.get(columns["unit"], {}).get("raw", "").strip()
            is_total = any(label.startswith(("总金额", "合计", "总计")) for label in labels)
            footer_without_quantity = (not quantity_raw or quantity_raw.startswith(("打印时间", "客户签名"))) and not unit_raw
            if not has_product and any(label.startswith(footer_labels) for label in labels) and (is_total or footer_without_quantity):
                skipped_rows.append({"row": number, "reason": "已识别汇总或签名/打印信息行", "cells": [dict(c) for c in row.values() if c["raw"] or c["formula"]]})
                warnings.append(f"{sheet} 第 {number} 行按汇总/页脚排除，原文已保留")
            else:
                data_rows[number] = row
        mapped_numbers = {_col_num(column) for column in columns.values()}
        for merge in merges:
            endpoints = merge.split(":")
            left, top = _col(endpoints[0])
            right, bottom = _col(endpoints[-1])
            intersects_rows = top <= header_row <= bottom or any(top <= number <= bottom for number in data_rows)
            if intersects_rows and any(_col_num(left) <= column <= _col_num(right) for column in mapped_numbers):
                raise ValueError("合并单元格与字段列重叠，明细归属不明确: " + merge)
        unknown_columns = {col: cell["raw"] for col, cell in rows[header_row].items() if col not in columns.values() and cell["raw"].strip()}
        if unknown_columns:
            warnings.append("未知列保留原文但未映射: " + ", ".join(unknown_columns.values()))
        for number in sorted(data_rows):
            row = data_rows[number]
            if any(c["formula"] and not c["cached"] for c in row.values()):
                raise ValueError(f"公式没有缓存值，不能可靠解析: {sheet} 第 {number} 行")
            if any(c["type"] == "e" for c in row.values()):
                raise ValueError(f"工作表含错误单元格: {sheet} 第 {number} 行")
            item = {"source_row": number, "source_cells": {}, "extra_fields": {}}
            for field, column in columns.items():
                cell = row.get(column, {"raw": "", "cell": column + str(number), "formula": False})
                raw = cell["raw"]
                if field in REQUIRED and not raw.strip():
                    raise ValueError(f"关键字段 {field} 为空: {sheet}!{cell['cell']}")
                if field in {"quantity", "unit_price"}:
                    item[field] = _decimal(raw, field, cell["cell"])
                else:
                    item[field] = raw.strip()
                item["source_cells"][field] = {"sheet": sheet, "cell": cell["cell"], "raw": raw, "formula": cell["formula"]}
                if cell["formula"]:
                    item["source_cells"][field]["formula_text"] = cell["formula_text"]
                    warnings.append(f"{sheet}!{cell['cell']} 使用公式缓存；未重新计算")
            for column, title in unknown_columns.items():
                if column in row:
                    item["extra_fields"][title] = {**row[column], "sheet": sheet}
            items.append(item)
        if not items:
            raise ValueError("工作表没有有效 ERP 明细")
        preamble_cells = [{"sheet": sheet, **cell} for number, row in rows.items() if number < header_row for cell in row.values() if cell["raw"]]
        sales_order = any("销售订单" in cell["raw"] for cell in preamble_cells)
        if sales_order:
            warnings.append("原文件标题为销售订单；不能作为实际出货或签收事实自动确认")
        return {"items": items, "sha256": hashlib.sha256(content).hexdigest(),
                "warnings": list(dict.fromkeys(warnings)),
                "source": {"filename": filename, "sheet": sheet, "header_row": header_row, "columns": fields,
                           "document_type": "sales_order" if sales_order else "unknown", "preamble_cells": preamble_cells,
                           "skipped_rows": skipped_rows}}
