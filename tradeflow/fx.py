"""Read-only ERP price projection and explicit CNY-per-USD conversion."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re


_NUMBER = re.compile(r"[+]?[0-9]+(?:\.[0-9]+)?\Z")
_CELL = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
_EXTRA = {
    "material": {"材质", "材料"}, "standard": {"标准号", "标准"},
    "customer_part_no": {"客户料号", "客户品号"},
    "amount_cny": {"金额", "销售金额", "人民币金额"},
}


def _number(value, field, *, positive=False):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError(f"{field} 必须是十进制字符串，长度不超过 64")
    text = value.strip()
    if not _NUMBER.fullmatch(text):
        raise ValueError(f"{field} 不是有效有限十进制数")
    integer, _, fraction = text.lstrip("+").partition(".")
    if len(integer) > 40 or len(fraction) > 18:
        raise ValueError(f"{field} 超过整数 40 位或小数 18 位限制")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field} 不是有效十进制数") from exc
    if positive and result <= 0:
        raise ValueError(f"{field} 必须大于零")
    return result


def _fact(value, field):
    if not isinstance(value, dict) or not isinstance(value.get("raw"), str):
        raise ValueError(f"{field} 缺少原始文本")
    if not isinstance(value.get("sheet"), str) or not value["sheet"] or not _CELL.fullmatch(str(value.get("cell", ""))):
        raise ValueError(f"{field} 缺少明确单元格定位")
    return value


def _header(source):
    result = dict.fromkeys(("order_no", "order_date", "buyer_name", "seller_name"))
    refs = {}
    cells = source.get("preamble_cells", [])
    labels = {"客户名称": "buyer_name", "单据编号": "order_no", "单据日期": "order_date"}
    matches = {name: [] for name in result}
    for cell in cells:
        if not isinstance(cell, dict) or not isinstance(cell.get("raw"), str):
            continue
        _fact(cell, "ERP 表头")
        text = cell["raw"].strip()
        for label, field in labels.items():
            match = re.fullmatch(re.escape(label) + r"\s*[:：]\s*(.*)", text)
            if not match:
                continue
            if match[1]:
                matches[field].append((match[1].strip(), [cell]))
            else:
                col, row = _CELL.fullmatch(cell["cell"]).groups()
                candidates = [c for c in cells if isinstance(c, dict) and isinstance(c.get("raw"), str)
                              and c["raw"].strip() and _CELL.fullmatch(str(c.get("cell", "")))
                              and _CELL.fullmatch(c["cell"])[2] == row
                              and _column(_CELL.fullmatch(c["cell"])[1]) > _column(col)]
                candidates.sort(key=lambda c: _column(_CELL.fullmatch(c["cell"])[1]))
                if candidates and not re.match(r".*[:：]", candidates[0]["raw"]):
                    matches[field].append((candidates[0]["raw"].strip(), [cell, candidates[0]]))
        title = re.fullmatch(r"(.+?)[-－—]\s*销售订单", text)
        if title:
            matches["seller_name"].append((title[1].strip(), [cell]))
    for name, found in matches.items():
        if len(found) == 1:
            result[name], refs[name] = found[0]
    return result, deepcopy(refs)


def _column(text):
    result = 0
    for char in text:
        result = result * 26 + ord(char) - 64
    return result


def project_erp_source(record: dict) -> dict:
    """Project a parsed ERP record, preserving raw source facts without mutation."""
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        raise ValueError("需要已导入 ERP 记录")
    payload = record["payload"]
    source = payload.get("source", {})
    digest = payload.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("ERP 原件哈希无效")
    if not isinstance(record.get("id"), str) or not record["id"]:
        raise ValueError("ERP 来源记录 ID 缺失")
    if not isinstance(source, dict) or not isinstance(source.get("sheet"), str) or not source["sheet"]:
        raise ValueError("ERP 工作表来源缺失")
    header, refs = _header(source)
    output = {"source_id": record["id"], "source_sha256": digest,
              "source_sheet": source["sheet"], "filename": source.get("filename"),
              "source_currency": "CNY", "source_currency_basis": "user_confirmed",
              "header": header, "header_sources": refs, "items": [],
              "warnings": deepcopy(payload.get("warnings", []))}
    rows = payload.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("ERP 没有产品明细")
    seen = set()
    with localcontext() as context:
        context.prec = 200
        quantity_total = Decimal(0)
        amount_total = Decimal(0)
        amounts_complete = True
        for row in rows:
            number = row.get("source_row")
            if type(number) is not int or number < 1 or number in seen:
                raise ValueError("ERP 明细源行号缺失或重复")
            seen.add(number)
            facts = row.get("source_cells", {})
            if not isinstance(facts, dict):
                raise ValueError("ERP 明细缺少来源单元格")
            item = {"line_id": "erp-" + str(number), "source_row": number,
                    "source_cells": deepcopy(facts), "extra_fields": deepcopy(row.get("extra_fields", {}))}
            for field in ("product_name", "specification", "quantity", "unit", "surface_color", "grade", "unit_price"):
                if field not in facts:
                    if field == "unit_price":
                        raise ValueError(f"第 {number} 行缺少人民币单价")
                    raise ValueError(f"第 {number} 行 {field} 缺少来源")
                fact = _fact(facts[field], field)
                if fact["sheet"] != source["sheet"] or int(_CELL.fullmatch(fact["cell"])[2]) != number:
                    raise ValueError(f"第 {number} 行来源单元格归属不一致")
                value = row.get(field)
                if field in {"quantity", "unit_price"}:
                    parsed = _number(value, field, positive=True)
                    if parsed != _number(fact["raw"], field, positive=True):
                        raise ValueError(f"第 {number} 行 {field} 与原单元格不一致")
                elif not isinstance(value, str) or value != fact["raw"].strip():
                    raise ValueError(f"第 {number} 行 {field} 与原单元格不一致")
                item["unit_price_cny" if field == "unit_price" else field] = value
            for field, aliases in _EXTRA.items():
                found = [value for label, value in item["extra_fields"].items() if label.strip() in aliases]
                if len(found) > 1:
                    raise ValueError(f"第 {number} 行 {field} 来源不唯一")
                item[field] = None
                if found:
                    fact = _fact(found[0], field)
                    if fact["sheet"] != source["sheet"] or int(_CELL.fullmatch(fact["cell"])[2]) != number:
                        raise ValueError(f"第 {number} 行附加字段来源不一致")
                    item[field] = fact["raw"].strip() or None
                    item["source_cells"][field] = deepcopy(fact)
            quantity = _number(item["quantity"], "数量", positive=True)
            price = _number(item["unit_price_cny"], "人民币单价", positive=True)
            quantity_total += quantity
            if item["amount_cny"] is None:
                amounts_complete = False
            else:
                amount = _number(item["amount_cny"], "人民币金额", positive=True)
                expected = quantity * price
                if amount != expected and amount != expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
                    raise ValueError(f"第 {number} 行人民币金额与数量×单价不一致")
                amount_total += amount
            output["items"].append(item)
        output["totals"] = {"quantity": format(quantity_total, "f"),
                            "total_cny": format(amount_total, "f") if amounts_complete else None}
    return output


def convert_erp_prices(projection: dict, exchange_rate: str) -> dict:
    """Apply one manually supplied CNY-per-USD rate, never an automatic rate."""
    rate = _number(exchange_rate, "汇率", positive=True)
    if not isinstance(projection, dict) or projection.get("source_currency") != "CNY":
        raise ValueError("换算来源币种必须是 CNY")
    output = deepcopy(projection)
    rows = output.get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("换算缺少 ERP 明细")
    output.update(exchange_rate=exchange_rate.strip(), exchange_rate_convention="CNY_per_USD",
                  target_currency="USD", rounding_policy={"unit_price_decimals": 4, "amount_decimals": 2,
                  "rounding": "ROUND_HALF_UP", "total_basis": "sum_rounded_lines"})
    with localcontext() as context:
        context.prec = 200
        total = Decimal(0)
        raw_total = Decimal(0)
        amounts_complete = True
        for item in rows:
            quantity = _number(item.get("quantity"), "数量", positive=True)
            price = _number(item.get("unit_price_cny"), "人民币单价", positive=True)
            exact = price / rate
            rounded = exact.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            amount = (quantity * rounded).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            item.update(unit_price_usd_exact=format(exact, "f"), unit_price_usd=format(rounded, ".4f"),
                        amount_usd=format(amount, ".2f"))
            total += amount
            if rounded == 0:
                output.setdefault("warnings", []).append(f"第 {item.get('source_row')} 行美元单价按四位小数舍入为零，请复核")
            if item.get("amount_cny") is None:
                amounts_complete = False
            else:
                original = _number(item["amount_cny"], "人民币金额", positive=True)
                expected = quantity * price
                if original != expected and original != expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
                    raise ValueError("人民币金额与数量×单价不一致")
                raw_total += original
        direct = (raw_total / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if amounts_complete else None
        output["totals"] = {**output.get("totals", {}), "total_usd": format(total, ".2f"),
                            "direct_total_usd": format(direct, ".2f") if direct is not None else None,
                            "rounding_difference_usd": format(total - direct, ".2f") if direct is not None else None}
    return output
