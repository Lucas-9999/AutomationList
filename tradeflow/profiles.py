"""Explicit field maps for the user's inspected Korean Excel layouts.

Profiles are candidates, not an assertion of business or visual acceptance.
"""
from __future__ import annotations

from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .templates import MAIN, _xml, _cells, inspect_template
from .local_settings import template_seller_names


def read_values(content: bytes, inspection: dict) -> dict:
    with ZipFile(BytesIO(content)) as package:
        shared = []
        if "xl/sharedStrings.xml" in package.namelist():
            root = _xml(package.read("xl/sharedStrings.xml"))
            shared = ["".join(n.text or "" for n in item.iter(f"{{{MAIN}}}t")) for item in root.findall(f"{{{MAIN}}}si")]
        sheets = {}
        for sheet in inspection["sheets"]:
            values = {}
            root = _xml(package.read(sheet["part"]))
            for cell in _cells(root).values():
                value = cell.find(f"{{{MAIN}}}v")
                raw = value.text or "" if value is not None else ""
                if cell.get("t") == "s":
                    try:
                        index = int(raw)
                        if index < 0:
                            raise IndexError
                        raw = shared[index]
                    except (ValueError, IndexError):
                        raise ValueError("模板共享字符串引用无效") from None
                elif cell.get("t") == "inlineStr":
                    raw = "".join(n.text or "" for n in cell.iter(f"{{{MAIN}}}t"))
                values[cell.attrib["r"]] = raw
            sheets[sheet["name"]] = values
        return sheets


def candidate_profile(content: bytes, document_type: str) -> dict:
    inspection = inspect_template(content)
    values = read_values(content, inspection)
    profile = {
        "template_sha256": inspection["sha256"], "document_type": document_type,
        "status": "candidate_mapping", "format_accepted": False,
        "signature_review_required": inspection["signature_review_required"],
        "fields": [], "item_slots": [], "blockers": [], "inspection": inspection,
    }
    matches = []
    expected_title = {"ci": "Commercial Invoice", "pi": "PROFORMA INVOICE", "contract": "Contract Agreement", "pl": "PACKING LIST"}
    for sheet, cells in values.items():
        if document_type in {"ci", "pi", "contract"} and cells.get("A6", "").strip() == expected_title[document_type]:
            matches.append((sheet, cells, "invoice"))
        elif document_type == "pl" and cells.get("A7", "").strip() == "PACKING LIST":
            matches.append((sheet, cells, "packing"))
        elif document_type == "customs" and "出口货物报关单" in cells.get("A1", ""):
            matches.append((sheet, cells, "customs"))
    if len(matches) != 1:
        profile["blockers"].append("未找到唯一已知模板布局；需建立专用字段映射")
        return profile
    sheet, cells, layout = matches[0]
    profile["sheet"] = sheet
    if layout in {"invoice", "packing"}:
        profile["fixed_context"] = {
            "seller_names": template_seller_names(),
            "currency": "USD", "incoterm": "EXW",
            "note": "固定卖方抬头、银行和工厂提货条款；其他卖方、币种或贸易术语需新适配",
        }

    def field(path, cell, value_type="text", prefix=""):
        if cell not in cells:
            profile["blockers"].append("目标单元格不存在：" + sheet + "!" + cell)
            return
        profile["fields"].append({"field": path, "sheet": sheet, "cell": cell, "value_type": value_type, "prefix": prefix})

    if layout == "invoice":
        for path, cell, prefix in [
            ("document_no", "A7", "Contract No.: "), ("issue_date", "A9", "Date: "),
            ("validity", "A11", "Validity: "), ("buyer.name", "A15", ""),
            ("buyer.address", "A17", ""), ("buyer.address_extra", "A19", ""),
            ("buyer.contact", "A21", "Contact: "), ("consignee_text", "A23", "Consignee: "),
            ("trade_terms", "A25", "Terms of Trade: "), ("payment_terms", "B27", ""),
            ("delivery_date", "A29", "Delivery: "),
        ]:
            field(path, cell, prefix=prefix)
        rows = []
        for row in range(32, 201):
            if cells.get(f"A{row}", "").strip() == "S.TOTAL":
                break
            if cells.get(f"B{row}", "").strip() and cells.get(f"E{row}", "").strip():
                rows.append(row)
        columns = {"number": "A", "description": "B", "specification": "E", "material": "F", "surface_color": "G", "quantity": "H", "unit": "I", "unit_price": "J", "amount": "K", "remark": "M"}
        for row in rows:
            profile["item_slots"].append({"row": row, "cells": {name: f"{column}{row}" for name, column in columns.items() if f"{column}{row}" in cells}})
        for cell, value in cells.items():
            if "Please" in value and "remittance" in value:
                field("remittance_reference_text", cell)
    elif layout == "packing":
        for path, cell, prefix in [
            ("document_no", "A8", "CI No.: "), ("issue_date", "A10", "Date: "),
            ("buyer.name", "A16", ""), ("buyer.address", "A18", ""),
            ("buyer.address_extra", "A20", ""), ("buyer.contact", "A22", "Contact: "),
            ("consignee_text", "A24", "Consignee: "), ("order_no", "B26", ""),
            ("trade_terms", "A28", "Terms of Trade: "), ("payment_terms", "B30", ""),
            ("delivery_date", "A32", "Delivery: "),
        ]:
            field(path, cell, prefix=prefix)
        columns = {"number": "A", "description": "B", "specification": "E", "quantity": "F", "unit": "G", "net_weight": "H", "gross_weight": "I", "small_box_count": "J", "carton_numbers": "K"}
        for row in range(35, 201):
            if "G.TOTAL" in cells.get(f"A{row}", ""):
                break
            if cells.get(f"B{row}", "").strip() and cells.get(f"E{row}", "").strip():
                profile["item_slots"].append({"row": row, "cells": {name: f"{column}{row}" for name, column in columns.items() if f"{column}{row}" in cells}})
    else:
        # Declarant text is supplied verbatim; no inferred code, port or tax fact.
        for path, cell in [
            ("domestic_consignor_text", "A3"), ("customs_port_text", "C3"),
            ("export_date_text", "F3"), ("declaration_date_text", "I3"),
            ("consignee_text", "A5"), ("transport_mode", "C5"),
            ("producer_text", "A6"), ("supervision_mode_text", "C6"),
            ("levy_nature_text", "F6"), ("contract_no", "A8"),
            ("trade_country", "C8"), ("destination_country", "F8"),
            ("destination_port", "I8"), ("package_type", "A10"),
            ("package_count_text", "C10"), ("gross_weight", "D10"),
            ("net_weight", "F10"), ("incoterm", "H10"),
            ("miscellaneous_fees", "M10"), ("marks_and_notes", "A12"),
        ]:
            field(path, cell, "decimal" if path in {"gross_weight", "net_weight"} else "text")
        columns = {"number": "A", "hs_code": "B", "description": "C", "quantity_and_units": "E", "unit_price": "G", "amount_and_currency": "H", "origin_country": "I", "destination_country": "J", "domestic_origin": "L", "levy_method": "M"}
        for row in range(14, 20):
            if cells.get(f"C{row}", "").strip() and cells.get(f"B{row}", "").strip():
                profile["item_slots"].append({"row": row, "cells": {name: f"{column}{row}" for name, column in columns.items() if f"{column}{row}" in cells}})
        profile["blockers"].append("报关聚合行与具体商品明细的对应、编码和双计量单位需明确验证")
    profile["line_capacity"] = len(profile["item_slots"])
    if not profile["item_slots"]:
        profile["blockers"].append("未发现可映射明细行")
    profile["blockers"].extend([
        "映射候选尚未逐字段与逐页版式验收",
        "历史签章和固定图片需分类处理，不能复制旧客户签署事实",
    ])
    if any(sheet["formulas"] for sheet in inspection["sheets"]):
        profile["blockers"].append("金额及合计公式需要重算和独立核对，不能采用旧缓存")
    if inspection["external_link_parts"] or inspection["external_relationships"]:
        profile["blockers"].append("模板外部链接待清除或替换为已确认的本单数据")
    return profile
