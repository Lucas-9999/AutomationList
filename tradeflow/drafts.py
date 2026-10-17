"""In-memory review drafts for explicitly inspected template versions only."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
from io import BytesIO
import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .calculations import recalculate_supported_formulas, _prepare_formulas
from .profiles import candidate_profile, read_values
from .templates import MAIN, DOC_REL, _package, _xml, _cells, _cell_spans, patch_xlsx_cells

KNOWN_TEMPLATES = {
    "1eb445af6122e7c19b91230481c53ad18bd2f12c346aa912b090950883589748": "ci",
    "559c500167e7b460bca1ad63b0c3c0e8c9f899e0caf30e9772dbfbcb9ce7c63d": "contract",
    "e1752637d8e052900cc60a951110c96cf8008d966b5db6abdf9264bfe1147286": "pi",
    "ff96fa39d977287388f395dff125ac72c29a999a790e8a9da826d88f77156081": "pl",
    "21465457048fa448e8dc9dd1f4142a5ada90e2dd8959ccb1b3224c25c3218942": "contract",
    "d6f53fc4a02b5950d9c8404b64a33c4b224101dfa471c048ceedb0db4ede4135": "ci",
    "ff04d9b7c4835e90e6833f1af5eb8db4178ce1eaed7f76ffff0c78fa602e6581": "pi",
    "df0ea261a74511ebbddf686dd1f9e816da409c7ff86d88e9a55dc9ca7caede9c": "pl",
}
MEDIA_CLASSIFICATION = {
    "f8d77e97b03a52124fa8eaa3a33421de4f3f5cc9b1dd5245703f1d2c25d32289": "historical_company_stamp",
    "2c458bd057f85ff3870fbaa3657a73883649c4f8a79402123e442fb07b8c223c": "historical_customer_signature",
    "f8c5419b71044b1617b11d60c3a3cb5ef40df76a964bfa829a87d0bcb5a93551": "logo",
    "69af6766ced47824f4a368a0e9222eb68cbf17ece70c4be52352389d0472558b": "logo",
}
NUMERIC_FIELDS = {"number", "quantity", "unit_price", "amount", "net_weight", "gross_weight", "small_box_count"}
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _repack(content, replacements, removed=()):
    infos, parts, comment = _package(content)
    output = BytesIO()
    with ZipFile(output, "w") as package:
        package.comment = comment
        for info in infos:
            if info.filename not in removed:
                package.writestr(info, replacements.get(info.filename, parts[info.filename]))
    return output.getvalue()


def _strip_signatures(content):
    _, parts, _ = _package(content)
    removed, report = set(), []
    for name, payload in parts.items():
        if not name.startswith("xl/media/") or name.endswith("/"):
            continue
        digest = sha256(payload).hexdigest()
        classification = MEDIA_CLASSIFICATION.get(digest)
        if classification is None:
            raise ValueError("模板含未分类媒体，禁止复制：" + name)
        if classification != "logo":
            removed.add(name)
            report.append({"part": name, "sha256": digest, "classification": classification})
    replacements, referenced_removed = {}, set()
    for name, payload in parts.items():
        if not name.startswith("xl/drawings/_rels/") or not name.endswith(".rels"):
            continue
        drawing_name = "xl/drawings/" + posixpath.basename(name)[:-5]
        if drawing_name not in parts:
            raise ValueError("绘图关系缺少对应 drawing")
        relationships, identities = _xml(payload), set()
        for relation in list(relationships):
            target = relation.get("Target", "")
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl/drawings", target))
            if resolved in removed:
                if relation.get("Type") != DOC_REL + "/image":
                    raise ValueError("签章部件具有非图像引用")
                identities.add(relation.get("Id"))
                referenced_removed.add(resolved)
                relationships.remove(relation)
        if not identities:
            continue
        drawing = _xml(parts[drawing_name])
        removed_ids = set()
        for anchor in list(drawing):
            linked = {node.get(f"{{{DOC_REL}}}embed") for node in anchor.iter(f"{{{A}}}blip")}
            if linked & identities:
                if anchor.tag not in {f"{{{XDR}}}twoCellAnchor", f"{{{XDR}}}oneCellAnchor", f"{{{XDR}}}absoluteAnchor"} or linked - identities:
                    raise ValueError("签章绘图具有不支持的复合锚点")
                drawing.remove(anchor)
                removed_ids |= linked & identities
        if removed_ids != identities:
            raise ValueError("签章 drawing 锚点与关系不一致")
        replacements[name] = ET.tostring(relationships, encoding="utf-8", xml_declaration=True)
        replacements[drawing_name] = ET.tostring(drawing, encoding="utf-8", xml_declaration=True)
    if referenced_removed != removed:
        raise ValueError("签章部件未通过唯一已知绘图引用定位")
    # No other relationship may retain a removed image reference.
    for name, payload in parts.items():
        if not name.endswith(".rels"):
            continue
        base = posixpath.dirname(posixpath.dirname(name))
        for relation in _xml(replacements.get(name, payload)):
            target = relation.get("Target", "")
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
            if resolved in removed:
                raise ValueError("签章仍被其他 OOXML 部件引用")
    if removed and "[Content_Types].xml" in parts:
        types = _xml(parts["[Content_Types].xml"])
        changed = False
        for node in list(types):
            if node.get("PartName", "").lstrip("/") in removed:
                types.remove(node)
                changed = True
        if changed:
            replacements["[Content_Types].xml"] = ET.tostring(types, encoding="utf-8", xml_declaration=True)
    return _repack(content, replacements, removed), report


def _clear_unused_shared_strings(content, inspection):
    _, parts, _ = _package(content)
    if "xl/sharedStrings.xml" not in parts:
        return content, 0
    strings = _xml(parts["xl/sharedStrings.xml"])
    entries = strings.findall(f"{{{MAIN}}}si")
    used = set()
    for sheet in inspection["sheets"]:
        root = _xml(parts[sheet["part"]])
        for cell in root.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row/{{{MAIN}}}c"):
            if cell.get("t") == "s":
                value = cell.find(f"{{{MAIN}}}v")
                if value is None or not (value.text or "").isdigit() or int(value.text) >= len(entries):
                    raise ValueError("模板共享字符串索引无效")
                used.add(int(value.text))
    count = 0
    for index, item in enumerate(entries):
        if index not in used:
            item.clear()
            count += 1
    if not count:
        return content, 0
    return _repack(content, {"xl/sharedStrings.xml": ET.tostring(strings, encoding="utf-8", xml_declaration=True)}), count


def _decimal(value, field):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(field + " 必须是有限 Decimal，拒绝 float/bool/字符串")
    if value < 0 or len(value.as_tuple().digits) > 100 or abs(value.as_tuple().exponent) > 100:
        raise ValueError(field + " 必须非负且精度不超过支持范围")
    return value


def _serialize_xml_preserving_prefixes(root, source):
    """Retain declared prefixes used by mc:Ignorable and other QName values."""
    declarations = list(dict(re.findall(br'xmlns(?::([A-Za-z_][A-Za-z0-9_.-]*))?="([^"]+)"', source)).items())
    for prefix, uri in declarations:
        name = prefix.decode("ascii")
        if not re.fullmatch(r"ns[0-9]+", name):
            ET.register_namespace(name, uri.decode("utf-8"))
    serialized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    start = serialized.find(b"<", serialized.find(b"?>") + 2)
    end = serialized.find(b">", start)
    opening = serialized[start:end]
    additions = []
    for prefix, uri in declarations:
        attribute = b"xmlns" + (b":" + prefix if prefix else b"")
        if not re.search(br"(?:^|\s)" + re.escape(attribute) + br"=", opening):
            additions.append(b" " + attribute + b'="' + uri + b'"')
    return serialized[:end] + b"".join(additions) + serialized[end:]


def _set_four_decimal_prices(content, inspection, sheet, price_cells):
    """Clone only price cell number styles, preserving every visual property."""
    if not price_cells:
        return content
    _, parts, _ = _package(content)
    if "xl/styles.xml" not in parts:
        raise ValueError("单价格式缺少样式部件")
    styles = _xml(parts["xl/styles.xml"])
    from copy import deepcopy
    formats = styles.find(f"{{{MAIN}}}numFmts")
    if formats is None:
        formats = ET.Element(f"{{{MAIN}}}numFmts", {"count": "0"})
        styles.insert(0, formats)
    xfs = styles.find(f"{{{MAIN}}}cellXfs")
    if xfs is None:
        raise ValueError("单价格式缺少 cellXfs")
    codes = {int(node.get("numFmtId")): node.get("formatCode") for node in formats}
    next_id = max([163] + list(codes)) + 1
    part = next(entry["part"] for entry in inspection["sheets"] if entry["name"] == sheet)
    payload = parts[part]; cells = _cells(_xml(payload)); spans = _cell_spans(payload)
    replacements, patches = {}, []
    for reference in sorted(price_cells):
        original = int(cells[reference].get("s", "0"))
        if original not in replacements:
            xf = deepcopy(xfs[original]); old_code = codes.get(int(xf.get("numFmtId", "0")), "0.0000")
            code = re.sub(r"0\.[0#]+", "0.0000", old_code)
            if code == old_code and "0.0000" not in code:
                code = "0.0000"
            ET.SubElement(formats, f"{{{MAIN}}}numFmt", {"numFmtId": str(next_id), "formatCode": code})
            xf.set("numFmtId", str(next_id)); xf.set("applyNumberFormat", "1"); next_id += 1
            replacements[original] = len(xfs); xfs.append(xf)
        span = spans[reference]
        raw = payload[span["start"]:span["end"]]
        opening_end = raw.find(b">")
        opening = raw[:opening_end]
        if re.search(br'\ss="[^"]*"', opening):
            opening = re.sub(br'\ss="[^"]*"', b' s="' + str(replacements[original]).encode() + b'"', opening, count=1)
        else:
            opening += b' s="' + str(replacements[original]).encode() + b'"'
        patches.append((span["start"], span["end"], opening + raw[opening_end:]))
    for start, end, replacement in sorted(patches, reverse=True):
        payload = payload[:start] + replacement + payload[end:]
    formats.set("count", str(len(formats))); xfs.set("count", str(len(xfs)))
    return _repack(content, {part: payload, "xl/styles.xml": _serialize_xml_preserving_prefixes(styles, parts["xl/styles.xml"])})


def _rewrite_template_formulas(content, inspection, sheet, *, clear_cells=(), round_cells=(), amount_decimals=None):
    """Normalize inspected shared formulas before clearing slots or rounding money.

    Only formula children are changed: style, cell attributes, merges, row heights
    and all other XML bytes survive. Shared rectangles may then safely lose a cell.
    """
    _, parts, _ = _package(content)
    part = next(entry["part"] for entry in inspection["sheets"] if entry["name"] == sheet)
    payload = parts[part]
    formulas, _ = _prepare_formulas(_cells(_xml(payload)), sheet)
    spans = _cell_spans(payload)
    patches = []
    for reference, expression in formulas.items():
        span = next(child for child in spans[reference]["children"] if child["name"] == "f")
        opening = payload[span["start"]:span["end"]]
        qualified = re.match(br"<([A-Za-z_][A-Za-z0-9_.:-]*)", opening)[1]
        if reference in clear_cells:
            replacement = b""
        else:
            if reference in round_cells and amount_decimals is not None:
                expression = f"ROUND({expression},{amount_decimals})"
            replacement = b"<" + qualified + b">" + expression.encode("ascii") + b"</" + qualified + b">"
        patches.append((span["start"], span["end"], replacement))
    for start, end, replacement in sorted(patches, reverse=True):
        payload = payload[:start] + replacement + payload[end:]
    return _repack(content, {part: payload})


def _amount_precision(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value != 2):
        raise ValueError("amount_decimals 仅支持显式 2 位金额或旧路径 None")


def generate_review_draft(content: bytes, document_type: str, fields: dict, items: list, *, allow_partial_slots: bool = False, amount_decimals: int | None = None) -> tuple[bytes, dict]:
    """Return an unsigned REVIEW DRAFT copy and a non-acceptance report.

    Fields are flat profile names mapped to explicit strings. Item numeric values
    must be Decimal, including sequence numbers. No file system or network I/O.
    """
    _amount_precision(amount_decimals)
    if not isinstance(allow_partial_slots, bool):
        raise ValueError("allow_partial_slots 必须是布尔值")
    if document_type not in {"ci", "pi", "contract", "pl"}:
        raise ValueError("审阅草稿目前只支持 ci/pi/contract/pl；报关单与签收单未实现")
    template_hash = sha256(content).hexdigest()
    if KNOWN_TEMPLATES.get(template_hash) != document_type:
        raise ValueError("模板 SHA256 或单据类型尚未绑定已检查布局")
    profile = candidate_profile(content, document_type)
    if not profile.get("line_capacity") or "sheet" not in profile:
        raise ValueError("模板没有完整已知映射")
    inspection, sheet = profile["inspection"], profile["sheet"]
    if inspection["external_link_parts"] or inspection["external_relationships"]:
        raise ValueError("模板存在外部引用，禁止生成草稿")
    source_values = read_values(content, inspection)
    if any(name != sheet and any(value for value in values.values()) for name, values in source_values.items()):
        raise ValueError("辅助工作表含未映射数据，需要单独审阅")
    expected_fields = {f["field"] for f in profile["fields"]}
    if not isinstance(fields, dict) or set(fields) != expected_fields or any(not isinstance(v, str) for v in fields.values()):
        raise ValueError("fields 必须逐一覆盖且仅包含全部映射字段，值使用字符串")
    if not re.search(r"(?<![\d.])30\s*%\s*down\s+payment", fields["payment_terms"], re.I):
        raise ValueError("payment_terms 必须明确 30% down payment")
    if any(Decimal(value) != 30 for value in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*down\s+payment", fields["payment_terms"], re.I)):
        raise ValueError("payment_terms 含冲突预付款比例")
    trade_terms = fields["trade_terms"]
    if not re.search(r"\bEXW\b", trade_terms, re.I) or re.search(r"\b(?:FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP)\b", trade_terms, re.I):
        raise ValueError("当前已知模板含 EXW 固定标题与提货条款，仅支持明确 EXW 条款")
    valid_count = isinstance(items, list) and (1 <= len(items) <= profile["line_capacity"] if allow_partial_slots else len(items) == profile["line_capacity"])
    if not valid_count:
        raise ValueError("明细行数必须与模板容量完全一致，禁止遗留旧明细")
    values_before = source_values[sheet]
    formulas = {s["name"]: {f["cell"] for f in s["formulas"]} for s in inspection["sheets"]}
    updates = {f["cell"]: f["prefix"] + fields[f["field"]] for f in profile["fields"]}
    expected_numeric, normalized_items = {}, []
    cleared_slot_cells = {cell for slot in profile["item_slots"][len(items):] for cell in slot["cells"].values()}
    updates.update({cell: None for cell in cleared_slot_cells})
    explicit_fact_formula_cells = ({cell for slot in profile["item_slots"][:len(items)]
                                    for field, cell in slot["cells"].items() if field != "amount"}
                                   & formulas[sheet]) if allow_partial_slots else set()
    for index, (item, slot) in enumerate(zip(items, profile["item_slots"]), 1):
        if not isinstance(item, dict) or set(item) != set(slot["cells"]):
            raise ValueError("每行必须完整且仅覆盖该明细槽全部字段")
        normalized = {}
        for field, cell in slot["cells"].items():
            value = item[field]
            if field in NUMERIC_FIELDS:
                value = _decimal(value, field)
                if field in {"quantity", "number"} and value <= 0:
                    raise ValueError(field + " 必须大于零")
                if field in {"number", "small_box_count"} and value != value.to_integral_value():
                    raise ValueError(field + " 必须为整数")
                if field == "number" and value != index:
                    raise ValueError("number 必须从 1 顺序编号")
                expected_numeric[cell] = value
            elif not isinstance(value, str) or (field not in {"remark", "carton_numbers"} and not value.strip()):
                raise ValueError(field + " 必须为显式非空字符串；备注和箱号可显式留空")
            normalized[field] = value
            if cell not in formulas[sheet] or cell in explicit_fact_formula_cells:
                updates[cell] = value
        if document_type != "pl":
            with localcontext() as context:
                context.prec = 500
                expected_amount = normalized["quantity"] * normalized["unit_price"]
                if amount_decimals is not None:
                    if normalized["unit_price"] != normalized["unit_price"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP):
                        raise ValueError("美元单价必须先按四位小数换算确认")
                    expected_amount = expected_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if expected_amount != normalized["amount"]:
                    raise ValueError("明细金额与数量乘单价不一致")
        elif normalized["gross_weight"] < normalized["net_weight"]:
            raise ValueError("毛重不得小于净重")
        normalized_items.append(normalized)
    if len({item["unit"] for item in normalized_items}) != 1:
        raise ValueError("当前模板总数量仅支持统一计量单位")
    title = "A7" if document_type == "pl" else "A6"
    updates[title] = values_before[title] + " / REVIEW DRAFT / 待审阅"
    total_columns = {"quantity": "F", "net_weight": "H", "gross_weight": "I"} if document_type == "pl" else {"quantity": "H", "amount": "K"}
    totals = {}
    with localcontext() as context:
        context.prec = 500
        for key, column in total_columns.items():
            total = sum((item[key] for item in normalized_items), Decimal(0))
            totals[key] = str(total)
            for reference, value in values_before.items():
                if reference.startswith("A") and (value.strip() == "S.TOTAL" or "G.TOTAL" in value):
                    cell = column + reference[1:]
                    if cell not in inspection["sheets"][next(i for i,s in enumerate(inspection["sheets"]) if s["name"] == sheet)]["cells"]:
                        raise ValueError("模板缺少预期合计单元格")
                    expected_numeric[cell] = total
                    if cell not in formulas[sheet]:
                        updates[cell] = total
    result = content
    if cleared_slot_cells or explicit_fact_formula_cells or amount_decimals is not None:
        round_cells = {slot["cells"]["amount"] for slot in profile["item_slots"][:len(items)] if "amount" in slot["cells"]}
        result = _rewrite_template_formulas(result, inspection, sheet, clear_cells=cleared_slot_cells | explicit_fact_formula_cells, round_cells=round_cells, amount_decimals=amount_decimals)
    result = patch_xlsx_cells(result, {sheet: updates})
    result, calculation = recalculate_supported_formulas(result)
    if amount_decimals is not None and document_type != "pl":
        price_cells = {slot["cells"]["unit_price"] for slot in profile["item_slots"][:len(items)]}
        result = _set_four_decimal_prices(result, inspection, sheet, price_cells)
    values_after = read_values(result, inspection)[sheet]
    for cell, expected in expected_numeric.items():
        try:
            actual = Decimal(values_after[cell])
        except (KeyError, ArithmeticError):
            raise ValueError("重算后数值缺失或无效：" + cell) from None
        if actual != expected:
            raise ValueError("重算值与独立明细/合计不一致：" + cell)
    if any(values_after.get(cell) for cell in cleared_slot_cells):
        raise ValueError("空余商品槽未完全清理")
    result, removed_media = _strip_signatures(result)
    result, cleared_count = _clear_unused_shared_strings(result, inspection)
    retained = [{"sheet": sheet, "cell": reference, "value": value}
                for reference, value in values_before.items()
                if value and reference not in updates and reference not in formulas[sheet]]
    return result, {
        "status": "review_draft", "document_type": document_type,
        "template_sha256": template_hash, "output_sha256": sha256(result).hexdigest(),
        "format_accepted": False, "business_validation_passed": False, "unsigned": True,
        "modified_cells": [{"sheet": sheet, "cell": cell} for cell in sorted(updates)],
        "removed_media": removed_media, "cleared_unused_shared_string_count": cleared_count,
        "formula_recalculation": calculation, "independent_totals": totals,
        "source_line_count": len(items), "cleared_unused_item_cells": sorted(cleared_slot_cells), "amount_decimals": amount_decimals,
        "replaced_historical_item_formula_cells": sorted(explicit_fact_formula_cells),
        "retained_template_constants_for_review": retained,
        "remaining_constants": "固定卖方抬头、银行及条款来自已绑定哈希模板，仍需本单业务审阅",
        "remaining_checks": ["逐字段业务审阅", "逐页视觉对照验收", "卖方银行和固定条款复核", "正式签署事实另行采集"],
    }
