"""Exact-version delivery-note drafts with an empty, unsigned receipt area."""
from __future__ import annotations

from decimal import Decimal, localcontext
from hashlib import sha256
import re

from .calculations import recalculate_supported_formulas
from .drafts import MEDIA_CLASSIFICATION, _clear_unused_shared_strings, _decimal, _repack, _strip_signatures, _rewrite_template_formulas
from .profiles import read_values
from .templates import MAIN, _cell_spans, _cells, _package, _xml, inspect_template, patch_xlsx_cells
from .local_settings import template_seller_names

KNOWN_RECEIPTS = {
    "fcc94ffd09b259cf18270e69dd6e4af97cf8b2739e6a3a09018dce84b357ef0e": {"first_row": 33, "line_capacity": 1, "total_row": 34, "signature_row": 45, "layout": "contract_header"},
    "77f66ffca525b840ff3832e48d2596c55ca4b2c02cab2c60031a171ad2c8762a": {"first_row": 35, "line_capacity": 11, "total_row": 46, "signature_row": 57, "layout": "invoice_header"},
}
ITEM_COLUMNS = {"number": "A", "description": "B", "specification": "C", "quantity": "D", "unit": "E", "net_weight": "F", "gross_weight": "G", "small_box_count": "H", "carton_numbers": "I"}
NUMERIC = {"number", "quantity", "net_weight", "gross_weight", "small_box_count"}
TITLE = "DELIVERY & RECEIPT NOTE 发货签收单"


def inspect_receipt_template(content: bytes) -> dict:
    """Inspect sparse OOXML and map only the two explicitly reviewed originals."""
    inspection = inspect_template(content)
    profile = {"document_type": "receipt", "template_sha256": inspection["sha256"],
               "status": "candidate_mapping", "format_accepted": False,
               "fields": [], "item_slots": [], "signature_clear_cells": [],
               "signature_review_required": True, "inspection": inspection, "blockers": []}
    settings = KNOWN_RECEIPTS.get(inspection["sha256"])
    if settings is None:
        profile["blockers"].append("签收单模板 SHA256 未绑定；需重新检查原稿布局和签章")
        return profile
    values = read_values(content, inspection)
    if list(values) != ["PL"] or values["PL"].get("A7") != TITLE:
        raise ValueError("已绑定签收模板的工作表或标题与预期不符")
    profile.update(settings)
    profile["sheet"] = "PL"
    profile["title_cell"] = "A7"
    profile["fixed_context"] = {
        "seller_names": template_seller_names(),
        "currency": "USD", "incoterm": "EXW",
    }
    profile["unwritable_item_fields"] = []
    profile["unwritable_item_cells"] = []
    def field(name, cell, prefix=""):
        if cell not in values["PL"]:
            raise ValueError("签收模板缺少映射单元格：" + cell)
        profile["fields"].append({"field": name, "sheet": "PL", "cell": cell, "value_type": "text", "prefix": prefix})
    short = settings["layout"] == "contract_header"
    field("document_no", "A8", "Contract No.: " if short else "CI No.: ")
    field("issue_date", "A10", "Date: ")
    if short:
        field("validity", "A12", "Validity: ")
    for name, cell, prefix in [("buyer.name", "A16", ""), ("buyer.address", "A18", ""),
                              ("buyer.address_extra", "A20", ""), ("buyer.contact", "A22", "Contact: "),
                              ("consignee_text", "A24", "Consignee: ")]:
        field(name, cell, prefix)
    if not short:
        field("order_no", "B26")
    field("trade_terms", "A26" if short else "A28", "Terms of Trade: ")
    field("payment_terms", "B28" if short else "B30")
    field("delivery_date", "A30" if short else "A32", "Delivery: ")
    for row in range(settings["first_row"], settings["first_row"] + settings["line_capacity"]):
        cells = {name: column + str(row) for name, column in ITEM_COLUMNS.items()}
        missing = set(cells.values()) - values["PL"].keys()
        if missing:
            if missing != {cells["carton_numbers"]} or not short:
                raise ValueError("签收模板明细槽不完整")
            profile["unwritable_item_fields"].append("carton_numbers")
            profile["unwritable_item_cells"].append({"field": "carton_numbers", "cell": cells["carton_numbers"], "only_allowed_value": ""})
        profile["item_slots"].append({"row": row, "cells": cells})
    # The original acknowledgement and the Received-by label are fixed template
    # text. Every other existing cell below that label is explicitly cleared.
    profile["signature_label_cell"] = "B" + str(settings["signature_row"])
    profile["signature_clear_cells"] = [cell for cell in values["PL"]
                                        if int(re.search(r"[0-9]+$", cell)[0]) >= settings["signature_row"]
                                        and cell != profile["signature_label_cell"]]
    _, parts, _ = _package(content)
    profile["media_classification"] = [{"part": part, "sha256": sha256(parts[part]).hexdigest(),
                                         "classification": MEDIA_CLASSIFICATION.get(sha256(parts[part]).hexdigest(), "unknown")}
                                        for part in inspection["media_parts"]]
    profile["blockers"].extend(["字段映射和逐页视觉版式尚未验收", "历史客户签章必须删除，签收姓名日期签字保持空白"])
    if inspection["external_link_parts"] or inspection["external_relationships"]:
        profile["blockers"].append("存在外部引用，不允许生成")
    if any(media["classification"] == "unknown" for media in profile["media_classification"]):
        profile["blockers"].append("存在未分类媒体，不允许生成")
    return profile


def _expand_sparse_totals(content, profile):
    """Normalize the inspected 9.24 sparse shared group, not arbitrary formulas.

    Original shared ref D46:G46 has D/F/G formulas and a deliberately blank E46.
    The calculator rejects incomplete rectangles. Expand exactly those three
    known sums to normal formulas; no missing shared member is guessed globally.
    """
    if profile["layout"] != "invoice_header":
        return content, []
    _, parts, _ = _package(content)
    part = profile["inspection"]["sheets"][0]["part"]
    payload = parts[part]
    cells, spans = _cells(_xml(payload)), _cell_spans(payload)
    if cells["E46"].find(f"{{{MAIN}}}f") is not None:
        raise ValueError("签收单原空白单位合计格发生变化")
    patches, report = [], []
    for cell, formula in {"D46": "SUM(D35:D45)", "F46": "SUM(F35:F45)", "G46": "SUM(G35:G45)"}.items():
        original = cells[cell].find(f"{{{MAIN}}}f")
        expected_attributes = {"t": "shared", "si": "1"}
        if cell == "D46":
            expected_attributes["ref"] = "D46:G46"
        if original is None or dict(original.attrib) != expected_attributes or (original.text or "") != ("SUM(D35:D45)" if cell == "D46" else ""):
            raise ValueError("签收单稀疏共享合计组与已检查原稿不一致")
        span = next(child for child in spans[cell]["children"] if child["name"] == "f")
        opening = payload[span["start"]:span["end"]]
        qualified = re.match(br"<([A-Za-z_][A-Za-z0-9_.:-]*)", opening)[1]
        replacement = b"<" + qualified + b">" + formula.encode("ascii") + b"</" + qualified + b">"
        patches.append((span["start"], span["end"], replacement))
        report.append({"sheet": "PL", "cell": cell,
                       "before": {"formula": original.text or "", "attributes": dict(original.attrib)},
                       "after": {"formula": formula, "attributes": {}},
                       "reason": "已绑定原稿稀疏共享合计组展开，保留E46空白"})
    for start, end, replacement in sorted(patches, reverse=True):
        payload = payload[:start] + replacement + payload[end:]
    _cells(_xml(payload))
    return _repack(content, {part: payload}), report


def generate_receipt_draft(content: bytes, fields: dict, items: list, *, allow_partial_slots: bool = False) -> tuple[bytes, dict]:
    """Generate an unsigned draft with explicit dispatch facts and no receipt fact.

    Numeric item fields require Decimal. The calling API must bind these facts
    to an actual confirmed allocation, rather than trusting arbitrary quantities.
    """
    profile = inspect_receipt_template(content)
    if "sheet" not in profile:
        raise ValueError("签收模板 SHA256 未绑定")
    inspection = profile["inspection"]
    if inspection["external_link_parts"] or inspection["external_relationships"]:
        raise ValueError("签收模板存在外部依赖")
    if not isinstance(fields, dict) or set(fields) != {field["field"] for field in profile["fields"]} or any(not isinstance(value, str) for value in fields.values()):
        raise ValueError("签收 fields 必须完整且仅覆盖映射字段，禁止注入签收姓名、日期或状态")
    if not re.search(r"(?<![\d.])30\s*%\s*down\s+payment", fields["payment_terms"], re.I) or any(Decimal(value) != 30 for value in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*down\s+payment", fields["payment_terms"], re.I)):
        raise ValueError("签收单付款条款必须明确 30% down payment 且无冲突比例")
    if not re.search(r"\bEXW\b", fields["trade_terms"], re.I) or re.search(r"\b(?:FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP)\b", fields["trade_terms"], re.I):
        raise ValueError("当前签收单仅支持 EXW 固定模板条款")
    if not isinstance(allow_partial_slots, bool):
        raise ValueError("allow_partial_slots 必须是布尔值")
    valid_count = isinstance(items, list) and (1 <= len(items) <= profile["line_capacity"] if allow_partial_slots else len(items) == profile["line_capacity"])
    if not valid_count:
        raise ValueError("签收单明细必须恰好匹配原模板容量，不能少行遗留历史事实或插入超容量行")
    updates = {field["cell"]: field["prefix"] + fields[field["field"]] for field in profile["fields"]}
    updates["A7"] = TITLE + " / REVIEW DRAFT / 待审阅"
    updates.update({cell: None for cell in profile["signature_clear_cells"]})
    formula_cells = {formula["cell"] for formula in inspection["sheets"][0]["formulas"]}
    missing_cells = {entry["cell"] for entry in profile["unwritable_item_cells"]}
    expected_numeric, normalized = {}, []
    cleared_slot_cells = {cell for slot in profile["item_slots"][len(items):] for cell in slot["cells"].values() if cell not in missing_cells}
    updates.update({cell: None for cell in cleared_slot_cells})
    explicit_fact_formula_cells = ({cell for slot in profile["item_slots"][:len(items)] for cell in slot["cells"].values()} & formula_cells) if allow_partial_slots else set()
    for index, (item, slot) in enumerate(zip(items, profile["item_slots"]), 1):
        if not isinstance(item, dict) or set(item) != set(ITEM_COLUMNS):
            raise ValueError("每条签收明细须完整覆盖全部明细字段")
        row = {}
        for name, cell in slot["cells"].items():
            value = item[name]
            if name in NUMERIC:
                value = _decimal(value, name)
                if name in {"number", "quantity"} and value <= 0:
                    raise ValueError(name + " 必须为正数")
                if name in {"number", "small_box_count"} and value != value.to_integral_value():
                    raise ValueError(name + " 必须为整数")
                if name == "number" and value != index:
                    raise ValueError("签收单序号必须从 1 顺序排列")
                expected_numeric[cell] = value
            elif not isinstance(value, str) or (name != "carton_numbers" and not value.strip()):
                raise ValueError(name + " 必须为明确字符串；仅箱号可显式留空")
            if cell in missing_cells:
                if name != "carton_numbers" or value != "":
                    raise ValueError("当前10.8模板箱号格不存在，仅允许显式空箱号；非空需可编辑模板适配")
            elif cell not in formula_cells or cell in explicit_fact_formula_cells:
                updates[cell] = value
            row[name] = value
        if row["gross_weight"] < row["net_weight"]:
            raise ValueError("签收明细毛重不得小于净重")
        normalized.append(row)
    if len({row["unit"] for row in normalized}) != 1:
        raise ValueError("签收单总数量仅支持统一单位")
    totals = {}
    with localcontext() as context:
        context.prec = 500
        for name, column in {"quantity": "D", "net_weight": "F", "gross_weight": "G"}.items():
            total = sum((row[name] for row in normalized), Decimal(0))
            totals[name] = str(total)
            cell = column + str(profile["total_row"])
            expected_numeric[cell] = total
            if cell not in formula_cells:
                updates[cell] = total
    result, formula_rewrites = _expand_sparse_totals(content, profile)
    if cleared_slot_cells or explicit_fact_formula_cells:
        result = _rewrite_template_formulas(result, inspection, "PL", clear_cells=cleared_slot_cells | explicit_fact_formula_cells)
    result = patch_xlsx_cells(result, {"PL": updates})
    result, calculation = recalculate_supported_formulas(result)
    current = read_values(result, inspection)["PL"]
    for cell, expected in expected_numeric.items():
        try:
            actual = Decimal(current[cell])
        except (KeyError, ArithmeticError):
            raise ValueError("签收单重算后数值无效：" + cell) from None
        if actual != expected:
            raise ValueError("签收单公式与本批实际输入/独立合计不一致：" + cell)
    if any(current.get(cell) for cell in cleared_slot_cells):
        raise ValueError("签收单空余商品槽未完全清理")
    result, removed_media = _strip_signatures(result)
    result, cleared_count = _clear_unused_shared_strings(result, inspection)
    after = read_values(result, inspection)["PL"]
    if any(after.get(cell) for cell in profile["signature_clear_cells"]):
        raise ValueError("签收空白区域清理失败")
    original = read_values(content, inspection)["PL"]
    return result, {
        "status": "review_draft", "document_type": "receipt",
        "template_sha256": profile["template_sha256"], "output_sha256": sha256(result).hexdigest(),
        "format_accepted": False, "business_validation_passed": False,
        "receipt_status": "awaiting_signature", "received_confirmed": False,
        "received_by": None, "signed_at": None, "signature_evidence": None,
        "unsigned": True, "modified_cells": [{"sheet": "PL", "cell": cell} for cell in sorted(updates)],
        "cleared_signature_cells": profile["signature_clear_cells"],
        "removed_media": removed_media, "cleared_unused_shared_string_count": cleared_count,
        "formula_recalculation": calculation, "independent_totals": totals,
        "formula_normalizations": formula_rewrites,
        "source_line_count": len(items), "cleared_unused_item_cells": sorted(cleared_slot_cells),
        "replaced_historical_item_formula_cells": sorted(explicit_fact_formula_cells),
        "unwritable_item_cells": profile["unwritable_item_cells"],
        "retained_template_constants_for_review": [{"sheet": "PL", "cell": cell, "value": value}
                                                    for cell, value in original.items() if value and cell not in updates and cell not in formula_cells],
        "remaining_checks": ["实际出货 allocation 绑定由调用方核验", "逐字段与逐页视觉验收；标题追加待审阅文字可能溢出，字号保持原稿", "实际签收后另行采集收货人、签收时间和签字依据"],
    }
