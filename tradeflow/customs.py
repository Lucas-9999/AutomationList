"""Bound-layout customs review drafts with explicit source-line continuations."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
from copy import deepcopy
from xml.etree import ElementTree as ET
import re
from xml.parsers import expat

from .calculations import recalculate_supported_formulas
from .drafts import _clear_unused_shared_strings, _decimal, _repack, _amount_precision, _serialize_xml_preserving_prefixes, _set_four_decimal_prices
from .profiles import candidate_profile, read_values
from .templates import MAIN, _cells, _package, _tag_end, _xml, inspect_template, patch_xlsx_cells

KNOWN_CUSTOMS = frozenset({
    "a66141e625ba187a32a10ef5b8c347bd68b438070ee284dbde3c0abec74a47d1",
    "66e98a3c5f45fd2fb9ea622d971c8c57bf1294c5bf550cd98bff5a8234932ca9",
})
DECIMAL_HEADERS = {"package_count", "gross_weight", "net_weight", "miscellaneous_fees"}
ITEM_FIELDS = {"line_id", "number", "hs_code", "description", "quantity", "unit", "legal_quantity", "legal_unit", "unit_price", "amount", "currency", "origin_country", "destination_country", "domestic_origin", "levy_method", "declaration_elements_text"}
DECIMAL_ITEMS = {"number", "quantity", "legal_quantity", "unit_price", "amount"}
CORE = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
REMOVED_CORE_NAMES = {DC + "}creator", CORE + "}lastModifiedBy", CORE + "}lastPrinted",
                      DCTERMS + "}created", DCTERMS + "}modified"}
PREFIXES = {"domestic_consignor_text": "境内发货人\n", "customs_port_text": "出境关别\n",
            "export_date_text": "出口日期\n", "declaration_date_text": "申报日期\n",
            "producer_text": "生产销售单位\n", "supervision_mode_text": "监管方式\n",
            "levy_nature_text": "征免性质\n", "marks_and_notes": "标记唛码及备注\n"}
CORE_EXPLICIT_BLANK_FIELDS = {"pre_entry_no", "customs_port_text", "export_date_text", "declaration_date_text"}


def normalize_hs_code(value: str) -> str:
    """Normalize only the explicit ten-digit syntax; never infer classification.

    The original input belongs in the caller's audited business record. A dotted
    eight-plus-two representation is the sole accepted alternate spelling.
    """
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9]{10}|[0-9]{8}\.[0-9]{2})", value):
        raise ValueError("HS编码须为明确十位数字或八位数字.两位数字；不推断、补位或分类")
    return value.replace(".", "")


def inspect_customs_template(content: bytes, *, core_fields_mode: bool = False) -> dict:
    if not isinstance(core_fields_mode, bool):
        raise ValueError("core_fields_mode 必须是布尔值")
    profile = candidate_profile(content, "customs")
    profile["generation_supported"] = False
    profile["source_line_capacity"] = 1
    profile["aggregation_supported"] = False
    profile["continuation_supported"] = True
    profile["structured_item_fields"] = sorted(ITEM_FIELDS)
    profile["decimal_item_fields"] = sorted(DECIMAL_ITEMS)
    if profile["template_sha256"] not in KNOWN_CUSTOMS:
        profile["blockers"].append("报关模板 SHA256 尚未绑定，禁止生成")
        return profile
    if profile.get("sheet") != "报关单" or len(profile.get("item_slots", [])) != 1 or profile["item_slots"][0]["row"] != 14:
        raise ValueError("已绑定报关模板没有预期的单个商品槽")
    for field in profile["fields"]:
        if field["field"] == "package_count_text":
            field["field"] = "package_count"
        field["value_type"] = "decimal" if field["field"] in DECIMAL_HEADERS else "text"
        field["prefix"] = PREFIXES.get(field["field"], "")
    profile["fields"].append({"field": "miscellaneous_fees_currency", "sheet": "报关单", "cell": "M10", "value_type": "text", "prefix": "", "compound_cell": True})
    if core_fields_mode:
        values = read_values(content, profile["inspection"])["报关单"]
        if not values.get("A2", "").startswith("预录入编号："):
            raise ValueError("已绑定报关模板预录入编号位置或前缀不符合原稿")
        profile["fields"].append({"field": "pre_entry_no", "sheet": "报关单", "cell": "A2", "value_type": "text", "prefix": "预录入编号：", "allow_empty": True})
        profile["core_fields_mode"] = True
        profile["explicit_blank_fields"] = sorted(CORE_EXPLICIT_BLANK_FIELDS)
    profile["generation_supported"] = True
    profile["generation_scope"] = "single_source_line_review_draft_only"
    profile["fixed_context"] = {"currency": "USD"}
    profile["known_external_formula_replacements"] = ["报关单!F8", "报关单!C8"]
    profile["historical_extra_cells"] = {"A15": "clear", "B16": "explicit_declaration_elements_text"}
    profile["blockers"] = ["只支持一条完整源商品，不能将多规格货物聚合或省略", "需逐字段业务审阅和逐页视觉验收", "HS编码适用性、申报要素和监管要求未由本引擎判定", "本输出未提交，不能作为已报关或税务办理完成依据"]
    return profile


def _remove_nodes(payload, predicate):
    """Remove selected XML element byte ranges, keeping namespace declarations."""
    _xml(payload)
    parser = expat.ParserCreate(namespace_separator="}")
    stack, ranges = [], []
    def start(name, attributes):
        offset = parser.CurrentByteIndex
        end = _tag_end(payload, offset)
        stack.append({"start": offset, "open_end": end, "self_closing": payload[offset:end].rstrip().endswith(b"/>"),
                      "selected": predicate(name, attributes), "ancestor_selected": any(x["selected"] for x in stack)})
    def finish(name):
        frame = stack.pop()
        if frame["selected"] and not frame["ancestor_selected"]:
            end = frame["open_end"] if frame["self_closing"] else _tag_end(payload, parser.CurrentByteIndex)
            ranges.append((frame["start"], end))
    parser.StartElementHandler, parser.EndElementHandler = start, finish
    try:
        parser.Parse(payload, True)
    except expat.ExpatError:
        raise ValueError("报关模板 XML 字节定位失败") from None
    for start_offset, end_offset in sorted(ranges, reverse=True):
        payload = payload[:start_offset] + payload[end_offset:]
    _xml(payload)
    return payload, len(ranges)


def _remove_external_dependencies(content):
    _, parts, _ = _package(content)
    removed = {name for name in parts if name.startswith("xl/externalLinks/")}
    removed |= {name for name in parts if name == "docProps/thumbnail.wmf"}
    replacements, changes = {}, []
    workbook = parts["xl/workbook.xml"]
    workbook, count = _remove_nodes(workbook, lambda name, attrs: name == MAIN + "}externalReferences" or name.split("}")[-1] == "AlternateContent")
    if count:
        replacements["xl/workbook.xml"] = workbook
        changes.append({"part": "xl/workbook.xml", "removed_nodes": count, "reason": "externalReferences与旧绝对路径元数据"})
    for name, payload in parts.items():
        if name in removed or not name.endswith(".rels"):
            continue
        changed, count = _remove_nodes(payload, lambda tag, attrs: tag.split("}")[-1] == "Relationship" and
                                       (attrs.get("Type", "").rsplit("/", 1)[-1] in {"externalLink", "externalLinkPath", "thumbnail"} or attrs.get("TargetMode") == "External"))
        if count:
            replacements[name] = changed
            changes.append({"part": name, "removed_nodes": count, "reason": "外链或历史预览关系"})
    types, count = _remove_nodes(parts["[Content_Types].xml"], lambda tag, attrs:
                                attrs.get("PartName", "").lstrip("/") in removed or attrs.get("Extension") == "wmf")
    if count:
        replacements["[Content_Types].xml"] = types
    records = [{"part": name, "sha256": sha256(parts[name]).hexdigest(),
                "classification": "historical_preview" if name == "docProps/thumbnail.wmf" else "external_link_or_cache"}
               for name in sorted(removed) if not name.endswith("/")]
    return _repack(content, replacements, removed), records, changes


def _prove_no_external_dependencies(content):
    inspection = inspect_template(content)
    _, parts, _ = _package(content)
    if inspection["external_link_parts"] or inspection["external_relationships"]:
        raise ValueError("报关草稿仍存在外部部件或关系")
    if _xml(parts["xl/workbook.xml"]).find(f"{{{MAIN}}}externalReferences") is not None:
        raise ValueError("报关草稿仍有 workbook 外部引用")
    for name, payload in parts.items():
        if name.endswith(".rels"):
            for relation in _xml(payload):
                if relation.get("Type", "").rsplit("/", 1)[-1] in {"externalLink", "externalLinkPath"} or relation.get("TargetMode") == "External":
                    raise ValueError("报关草稿仍含外部关系")
    for sheet in inspection["sheets"]:
        if any(any(symbol in f["formula"] for symbol in ("[", "]", "!")) for f in sheet["formulas"]):
            raise ValueError("报关草稿仍含跨表或外部公式")
    return {"external_link_parts": 0, "external_relationships": 0, "workbook_external_references": 0, "external_formulas": 0}


def _sanitize_customs_metadata(content):
    """Remove historical identity/custom properties, preserving other XML bytes.

    Only called after the public generator has bound the exact source version.
    Neither current wall time nor a new author is injected.
    """
    _, parts, _ = _package(content)
    replacements, removed, nodes, metadata_parts, relationship_changes = {}, set(), [], [], []
    if "docProps/core.xml" in parts:
        core = _xml(parts["docProps/core.xml"])
        if core.tag != "{" + CORE + "}coreProperties":
            raise ValueError("核心元数据命名空间不符合已检查原稿")
        for node in core.iter():
            if node.tag in {"{" + DC + "}title", "{" + DC + "}subject"} and "".join(node.itertext()).strip():
                raise ValueError("核心标题或主题含未分类文本，需先确认是否历史业务事实")
            qualified = node.tag[1:] if node.tag.startswith("{") else node.tag
            if qualified in REMOVED_CORE_NAMES:
                nodes.append({"part": "docProps/core.xml", "node": node.tag,
                              "value_sha256": sha256("".join(node.itertext()).encode("utf-8")).hexdigest()})
        core_bytes, count = _remove_nodes(parts["docProps/core.xml"], lambda name, attrs: name in REMOVED_CORE_NAMES)
        if count != len(nodes):
            raise ValueError("历史核心元数据节点定位数量不一致")
        if count:
            replacements["docProps/core.xml"] = core_bytes
    if "docProps/custom.xml" in parts:
        removed.add("docProps/custom.xml")
        metadata_parts.append({"part": "docProps/custom.xml",
                               "sha256": sha256(parts["docProps/custom.xml"]).hexdigest(),
                               "classification": "historical_custom_properties"})
        for name, payload in parts.items():
            if not name.endswith(".rels"):
                continue
            changed, count = _remove_nodes(payload, lambda tag, attrs: tag.split("}")[-1] == "Relationship" and
                                          (attrs.get("Type", "").rsplit("/", 1)[-1] == "custom-properties"
                                           or attrs.get("Target", "").lstrip("/") == "docProps/custom.xml"))
            if count:
                replacements[name] = changed
                relationship_changes.append({"part": name, "removed_nodes": count, "reason": "历史custom-properties关系"})
        types, count = _remove_nodes(parts["[Content_Types].xml"], lambda tag, attrs:
                                    attrs.get("PartName", "").lstrip("/") == "docProps/custom.xml")
        if count:
            replacements["[Content_Types].xml"] = types
    return _repack(content, replacements, removed), nodes, metadata_parts, relationship_changes


def _customs_item_updates(row, number, *, amount_decimals=None):
    r = str(number)
    amount = str(row["amount"]) if amount_decimals is None else format(row["amount"], ".2f")
    return {"A" + r: row["number"], "B" + r: row["hs_code"], "C" + r: row["description"],
            "E" + r: str(row["legal_quantity"]) + " " + row["legal_unit"] + "\n" + str(row["quantity"]) + " " + row["unit"],
            "G" + r: row["unit_price"], "H" + r: amount + " " + row["currency"],
            "I" + r: row["origin_country"], "J" + r: row["destination_country"],
            "L" + r: row["domestic_origin"], "M" + r: row["levy_method"],
            "A" + str(number + 1): None, "B" + str(number + 2): row["declaration_elements_text"]}


def _expand_customs_item_blocks(content, count):
    """Repeat bound rows 14..16; preserve row styles/heights and extend print area.

    The header exists once; Excel repeats rows 1..13 on continuation print pages.
    Each complete three-row item has a manual page boundary, preventing its
    declaration elements from separating from the corresponding product.
    """
    _, parts, _ = _package(content)
    inspection = inspect_template(content)
    part = inspection["sheets"][0]["part"]
    root = _xml(parts[part]); data = root.find(f"{{{MAIN}}}sheetData")
    rows = {int(row.get("r")): row for row in data}
    if not all(index in rows for index in (14, 15, 16)):
        raise ValueError("报关模板续明细缺少完整三行商品区")
    delta = (count - 1) * 3
    def shifted(reference, offset):
        return re.sub(r"([A-Z]+)([0-9]+)", lambda match: match[1] + str(int(match[2]) + offset), reference)
    originals = [deepcopy(rows[index]) for index in (14, 15, 16)]
    for row in data:
        number = int(row.get("r"))
        if number >= 17:
            row.set("r", str(number + delta))
            for cell in row:
                cell.set("r", shifted(cell.get("r"), delta))
                formula = cell.find(f"{{{MAIN}}}f")
                if formula is not None:
                    raise ValueError("报关续页页脚含未适配公式")
    insertion = list(data).index(rows[16]) + 1
    for index in range(1, count):
        for original in originals:
            row = deepcopy(original); row.set("r", str(int(row.get("r")) + index * 3))
            for cell in row:
                cell.set("r", shifted(cell.get("r"), index * 3))
                # Remove every historical value/cache/formula in copied item block.
                for child in list(cell):
                    if child.tag in {f"{{{MAIN}}}v", f"{{{MAIN}}}f", f"{{{MAIN}}}is"}:
                        cell.remove(child)
                cell.attrib.pop("t", None)
            data.insert(insertion, row); insertion += 1
    merges = root.find(f"{{{MAIN}}}mergeCells")
    if merges is not None:
        original_merges = []
        for merge in list(merges):
            reference = merge.get("ref")
            numbers = [int(x) for x in re.findall(r"[0-9]+", reference)]
            if min(numbers) >= 14 and max(numbers) <= 16:
                original_merges.append(reference)
            elif min(numbers) >= 17:
                merge.set("ref", shifted(reference, delta))
            elif min(numbers) <= 16 < max(numbers):
                raise ValueError("报关合并区域跨越商品续区边界")
        for index in range(1, count):
            for reference in original_merges:
                ET.SubElement(merges, f"{{{MAIN}}}mergeCell", {"ref": shifted(reference, index * 3)})
        merges.set("count", str(len(merges)))
    dimension = root.find(f"{{{MAIN}}}dimension")
    if dimension is not None:
        first, last = dimension.get("ref").split(":")
        dimension.set("ref", first + ":" + shifted(last, delta))
    breaks = root.find(f"{{{MAIN}}}rowBreaks")
    if breaks is not None:
        root.remove(breaks)
    breaks = ET.Element(f"{{{MAIN}}}rowBreaks", {"count": str(count - 1), "manualBreakCount": str(count - 1)})
    for index in range(count - 1):
        ET.SubElement(breaks, f"{{{MAIN}}}brk", {"id": str(16 + index * 3), "min": "0", "max": "16383", "man": "1"})
    # rowBreaks follows headerFooter in the OOXML worksheet sequence.
    root.append(breaks)
    workbook = _xml(parts["xl/workbook.xml"])
    names = workbook.find(f"{{{MAIN}}}definedNames")
    if names is None:
        names = ET.Element(f"{{{MAIN}}}definedNames")
        children = list(workbook)
        location = next((index for index, child in enumerate(children) if child.tag.split("}")[-1] in {"calcPr", "extLst"}), len(children))
        workbook.insert(location, names)
    for node in list(names):
        if node.get("name") in {"_xlnm.Print_Area", "_xlnm.Print_Titles"}:
            names.remove(node)
    ET.SubElement(names, f"{{{MAIN}}}definedName", {"name": "_xlnm.Print_Area", "localSheetId": "0"}).text = "'报关单'!$A$1:$M$" + str(21 + delta)
    ET.SubElement(names, f"{{{MAIN}}}definedName", {"name": "_xlnm.Print_Titles", "localSheetId": "0"}).text = "'报关单'!$1:$13"
    return _repack(content, {part: _serialize_xml_preserving_prefixes(root, parts[part]), "xl/workbook.xml": _serialize_xml_preserving_prefixes(workbook, parts["xl/workbook.xml"])})


def _validate_grouped_item_amount(quantity, unit_price, amount):
    """Validate a grouped display average without recalculating its source total.

    Source coverage and the sum of original amounts are the caller's job. This
    checks only the independently supplied, two-decimal total and its displayed
    four-decimal weighted average. All arguments must remain finite Decimals.
    """
    quantity = _decimal(quantity, "quantity")
    unit_price = _decimal(unit_price, "unit_price")
    amount = _decimal(amount, "amount")
    if quantity <= 0:
        raise ValueError("报关汇总交易数量必须为正")
    with localcontext() as context:
        context.prec = 500
        if amount != amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
            raise ValueError("报关汇总金额必须保留两位小数，不得静默舍入源金额")
        expected_price = (amount / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if unit_price != expected_price:
            raise ValueError("报关汇总单价必须为汇总金额除以交易数量后四舍五入保留四位")
        return {
            "display_unit_price": format(unit_price, ".4f"),
            "authoritative_amount": format(amount, ".2f"),
            "display_price_times_quantity": str(unit_price * quantity),
            "display_price_amount_difference": str(unit_price * quantity - amount),
        }


def generate_customs_draft(content: bytes, fields: dict, items: list, *, allow_multiple_items: bool = False, amount_decimals: int | None = None, core_fields_mode: bool = False, grouped_items: bool = False) -> tuple[bytes, dict]:
    """Generate an unsubmitted review copy covering every full source item.

    Multiple items require explicit opt-in and use same-workbook continuations.

    HS codes are explicit input; ten-digit syntax is not classification approval.
    The API must pass every allocated source line and verify their provenance.
    """
    _amount_precision(amount_decimals)
    if not isinstance(grouped_items, bool):
        raise ValueError("grouped_items 必须是布尔值")
    if grouped_items:
        # Group totals always retain the two-decimal source sum, even when the
        # caller omits the optional formatting setting used by legacy drafts.
        amount_decimals = 2
    if not isinstance(allow_multiple_items, bool):
        raise ValueError("allow_multiple_items 必须是布尔值")
    profile = inspect_customs_template(content, core_fields_mode=core_fields_mode)
    if not profile["generation_supported"]:
        raise ValueError("报关模板 SHA256 尚未绑定")
    inspection = profile["inspection"]
    if inspection["media_parts"] or any(s["drawings"] or s["legacy_drawings"] for s in inspection["sheets"]):
        raise ValueError("报关模板含未分类媒体或绘图，禁止继承签章")
    if len(inspection["sheets"]) != 1:
        raise ValueError("报关模板包含未映射工作表")
    if not isinstance(fields, dict) or set(fields) != {field["field"] for field in profile["fields"]}:
        raise ValueError("报关 fields 必须完整且仅覆盖全部显式字段")
    checked_fields = {}
    for key, value in fields.items():
        if key in DECIMAL_HEADERS:
            value = _decimal(value, key)
        elif not isinstance(value, str) or (key not in (CORE_EXPLICIT_BLANK_FIELDS if core_fields_mode else {"export_date_text", "declaration_date_text"}) and not value.strip()):
            raise ValueError(key + " 必须为明确非空文本；日期可显式空白")
        checked_fields[key] = value
    count = checked_fields["package_count"]
    if count <= 0 or count != count.to_integral_value():
        raise ValueError("package_count 必须为正整数 Decimal")
    if checked_fields["net_weight"] <= 0 or checked_fields["gross_weight"] < checked_fields["net_weight"]:
        raise ValueError("报关净重必须为正，毛重不能小于净重")
    if not re.fullmatch(r"[A-Z]{3}", checked_fields["incoterm"]):
        raise ValueError("incoterm 需要明确三字母代码，适用性须业务确认")
    if not re.fullmatch(r"[A-Z]{3}", checked_fields["miscellaneous_fees_currency"]):
        raise ValueError("杂费需要明确三字母币种")
    if not isinstance(items, list) or not items or (len(items) != 1 and not allow_multiple_items) or len(items) > 1000:
        raise ValueError("原报关模板仅能无损表达一条源商品；多行须明确启用续明细，禁止聚合、遗漏或截断")
    checked_items = []
    grouped_amount_checks = []
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
            raise ValueError("报关 item 必须完整提供单条源商品的全部结构化字段")
        checked = {}
        for key, value in item.items():
            if key in DECIMAL_ITEMS:
                value = _decimal(value, key)
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(key + " 必须为明确非空文本")
            checked[key] = value
        if checked["number"] != index or checked["quantity"] <= 0 or checked["legal_quantity"] <= 0:
            raise ValueError("报关单序号必须为 顺序编号，交易及法定数量必须为正")
        if core_fields_mode:
            checked["hs_code"] = normalize_hs_code(checked["hs_code"])
        elif not re.fullmatch(r"[0-9]{10}", checked["hs_code"]):
            raise ValueError("hs_code 必须由业务明确提供十位编码；不修正或推断历史点号编码")
        if checked["legal_unit"] not in {"KG", "千克"}:
            raise ValueError("当前已知模板的第一法定计量仅适配 KG/千克，其他单位需专用适配")
        if checked["destination_country"] != checked_fields["destination_country"]:
            raise ValueError("明细目的国与表头运抵国不一致")
        if checked["currency"] != "USD":
            raise ValueError("原稿 G14 单价格式固定 USD，商品币种仅支持 USD；其他币种需专用模板")
        if grouped_items:
            grouped_amount_checks.append({"line_id": checked["line_id"], **_validate_grouped_item_amount(
                checked["quantity"], checked["unit_price"], checked["amount"])})
        else:
            with localcontext() as context:
                context.prec = 500
                expected_amount = checked["quantity"] * checked["unit_price"]
                if amount_decimals is not None:
                    if checked["unit_price"] != checked["unit_price"].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP):
                        raise ValueError("美元单价必须先按四位小数换算确认")
                    expected_amount = expected_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if checked["amount"] != expected_amount:
                raise ValueError("报关金额与交易数量乘单价不一致")
        checked_items.append(checked)
    if len({row["line_id"] for row in checked_items}) != len(checked_items):
        raise ValueError("报关源行标识不能重复")
    with localcontext() as context:
        context.prec = 500
        if sum((row["legal_quantity"] for row in checked_items), Decimal(0)) != checked_fields["net_weight"]:
            raise ValueError("法定千克数量必须与本批净重一致")
        independent = {key: sum((row[key] for row in checked_items), Decimal(0)) for key in ("amount", "quantity", "legal_quantity")}
    checked = checked_items[0]
    updates = {}
    for field in profile["fields"]:
        key = field["field"]
        if key == "miscellaneous_fees_currency":
            continue
        value = checked_fields[key]
        updates[field["cell"]] = field["prefix"] + value if isinstance(value, str) else value
    updates["M10"] = str(checked_fields["miscellaneous_fees"]) + " " + checked_fields["miscellaneous_fees_currency"]
    updates["A1"] = "中华人民共和国海关出口货物报关单 / REVIEW DRAFT / 待审阅（未提交）"
    updates.update(_customs_item_updates(checked, 14, amount_decimals=amount_decimals))
    # One declaration workbook, one header, explicit continuation detail blocks.
    result = content
    if len(checked_items) > 1:
        result = _expand_customs_item_blocks(result, len(checked_items))
        for index, row in enumerate(checked_items[1:], 1):
            number = 14 + index * 3
            updates.update(_customs_item_updates(row, number, amount_decimals=amount_decimals))
    # H14/H17/... are independent text totals with currency, not G * quantity
    # formulas. A grouped rounded display average must never replace the sum.
    result = patch_xlsx_cells(result, {"报关单": updates}, allow_formula_overwrite=True)
    result, removed_parts, relationship_changes = _remove_external_dependencies(result)
    result, removed_metadata_nodes, removed_metadata_parts, metadata_relationship_changes = _sanitize_customs_metadata(result)
    removed_parts.extend(removed_metadata_parts)
    relationship_changes.extend(metadata_relationship_changes)
    result, calculation = recalculate_supported_formulas(result)
    if amount_decimals is not None:
        result = _set_four_decimal_prices(result, inspect_template(result), "报关单", {"G" + str(14 + index * 3) for index in range(len(checked_items))})
    output_inspection = inspect_template(result)
    result, cleared_count = _clear_unused_shared_strings(result, output_inspection)
    dependency_proof = _prove_no_external_dependencies(result)
    after = read_values(result, inspect_template(result))["报关单"]
    _, output_parts, _ = _package(result)
    output_cells = _cells(_xml(output_parts[inspection["sheets"][0]["part"]]))
    for index, row in enumerate(checked_items):
        number = 14 + index * 3
        hs_cell = output_cells["B" + str(number)]
        if hs_cell.get("t") != "inlineStr" or "".join(node.text or "" for node in hs_cell.iter(f"{{{MAIN}}}t")) != row["hs_code"]:
            raise ValueError("HS编码必须以原值 inlineStr 文本保存，不能丢失前导零")
        expected_cells = _customs_item_updates(row, number, amount_decimals=amount_decimals)
        for cell, expected in expected_cells.items():
            actual = after.get(cell)
            if expected is None:
                if actual:
                    raise ValueError("报关续明细旧值未清理：" + cell)
            elif isinstance(expected, Decimal):
                if Decimal(actual) != expected:
                    raise ValueError("报关草稿写入数值核对失败：" + cell)
            elif actual != expected:
                raise ValueError("报关草稿写入文本核对失败：" + cell)
    for cell, value in {"D10": checked_fields["gross_weight"], "F10": checked_fields["net_weight"]}.items():
        if Decimal(after[cell]) != value:
            raise ValueError("报关草稿写入数值核对失败：" + cell)
    if after["A15"] or after["C8"] != checked_fields["trade_country"] or after["F8"] != checked_fields["destination_country"]:
        raise ValueError("报关草稿历史额外项或国家写入核对失败")
    if core_fields_mode and after.get("A2") != "预录入编号：" + checked_fields["pre_entry_no"]:
        raise ValueError("报关预录入编号写入核对失败")
    return result, {
        "status": "review_draft", "document_type": "customs", "template_sha256": profile["template_sha256"],
        "output_sha256": sha256(result).hexdigest(), "format_accepted": False, "business_validation_passed": False,
        "submitted": False, "customs_status": "unsubmitted_review_draft", "unsigned": True,
        "source_line_ids": [row["line_id"] for row in checked_items], "source_line_count": len(checked_items),
        "continuation_blocks": len(checked_items), "amount_decimals": amount_decimals, "aggregation_performed": False,
        **({"grouped_items": True, "declaration_group_ids": [row["line_id"] for row in checked_items],
            "amount_authority": "caller_verified_sum_of_source_line_amounts",
            "unit_price_meaning": "group_amount_divided_by_group_quantity_rounded_to_4_decimals",
            "amount_storage": "independent_text_total_with_currency_no_display_price_formula",
            "grouped_amount_checks": grouped_amount_checks} if grouped_items else {}),
        **({"core_fields_mode": True, "hs_code_syntax": "ten_digits_normalized_from_explicit_input"} if core_fields_mode else {}),
        "modified_cells": [{"sheet": "报关单", "cell": cell} for cell in sorted(updates)],
        "replaced_external_formula_cells": ["报关单!F8"], "replaced_unverified_country_reference_cells": ["报关单!C8"],
        "removed_parts": removed_parts, "relationship_changes": relationship_changes,
        "removed_metadata_nodes": removed_metadata_nodes, "removed_metadata_parts": removed_metadata_parts,
        "external_dependency_proof": dependency_proof, "cleared_unused_shared_string_count": cleared_count,
        "formula_recalculation": calculation,
        "visual_acceptance": {"artifact_renderer_accepted": False, "native_excel_accepted": False,
                              "hs_code_storage": "inlineStr_normalized_ten_digits" if core_fields_mode else "inlineStr_preserves_exact_input",
                              "known_renderer_difference": "Artifact Tool曾将合成前导零编码0000000000渲染为0.00；原始OOXML保持文本，未改原样式"},
        "independent_checks": {"amount": str(independent["amount"]), "quantity": str(independent["quantity"]),
                               "legal_quantity_kg": str(independent["legal_quantity"]), "net_weight_kg": str(checked_fields["net_weight"])},
        "remaining_checks": ["调用方核验分类覆盖全部源行且无重复，组金额须为源行金额之和；显示均价乘数量可能存在舍入差额" if grouped_items else "调用方核验全部源行来源；续明细逐行核对，禁止聚合", "业务确认HS适用性、申报要素、贸易监管方式及双计量", "预录入编号由调用方逐票确认；未提交、未签章" if core_fields_mode else "官方编号和签章空白，尚未提交", "标题追加文字可能溢出，保持字号，需逐页视觉验收", "Artifact Tool与原生Excel渲染均未验收，不以截图格式替换源样式"],
    }
