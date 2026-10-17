"""Bind review inputs to source lines or explicitly validated declaration groups."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext

from .customs import DECIMAL_HEADERS, DECIMAL_ITEMS, ITEM_FIELDS, _validate_grouped_item_amount


def prepare_customs_document(profile: dict, order: dict, fields: dict, item_extras: dict, *, allow_multiple_items: bool = False, grouped_items: bool = False) -> dict:
    if not isinstance(grouped_items, bool):
        raise ValueError("grouped_items 必须是布尔值")
    if not profile.get("generation_supported"):
        raise ValueError("报关模板尚未适配")
    fixed_currency = profile.get("fixed_context", {}).get("currency")
    if fixed_currency and order.get("currency") != fixed_currency:
        raise ValueError("报关模板单价格式仅支持 " + fixed_currency + "，不能用于其他订单币种")
    if not isinstance(fields, dict) or not isinstance(item_extras, dict):
        raise ValueError("fields 和 item_extras 必须是对象")
    wanted = {item["field"] for item in profile["fields"]}
    if set(fields) - wanted:
        raise ValueError("存在未映射的报关表头字段")
    lines = {row["line_id"]: row for row in order["items"]}
    if grouped_items and len(lines) != len(order["items"]):
        raise ValueError("报关分类标识不能重复或导致分类数量丢失")
    if set(item_extras) - lines.keys():
        raise ValueError("报关补充值引用不存在的订单行")
    missing, headers, items = [], {}, []
    if not isinstance(allow_multiple_items, bool):
        raise ValueError("allow_multiple_items 必须是布尔值")
    if not lines or (len(lines) != 1 and not allow_multiple_items):
        missing.append("template_capacity: 报关模板只支持一条完整源商品，本批 " + str(len(lines)) + " 行；不能聚合或省略")
    if not grouped_items and any(len(row.get("allocation_sources", [])) > 1 for row in order["items"]):
        missing.append("template_source_capacity: 多条 ERP 原行对应同一订单行，当前报关模板不支持聚合表达")
    derived = {key: order[key] for key in ("incoterm", "destination_country", "trade_country", "consignee_text", "contract_no", "domestic_consignor_text", "customs_port_text", "producer_text", "supervision_mode_text", "levy_nature_text", "miscellaneous_fees_currency")
               if isinstance(order.get(key), str) and order[key].strip()}

    def text_value(key, value):
        if not isinstance(value, str):
            raise ValueError("报关输入须保留原始十进制或文本字符串：" + key)
        return value

    for key in sorted(wanted):
        if key in derived and key in fields and fields[key] != derived[key]:
            raise ValueError("报关补充值不能覆盖已保存的订单事实：" + key)
        value = derived.get(key, fields.get(key))
        if value is None:
            missing.append("fields." + key)
        else:
            headers[key] = text_value(key, value)
    grouped_amount_checks = []
    for index, row in enumerate(order["items"], 1):
        if grouped_items and any(key not in row for key in ("quantity", "unit", "unit_price", "amount")):
            raise ValueError("报关分类订单必须提供已验证的数量、单位、汇总单价及金额")
        extras = item_extras.get(row["line_id"], {})
        if not isinstance(extras, dict) or set(extras) - ITEM_FIELDS:
            raise ValueError("存在未映射的报关明细补充字段")
        known = {key: row[key] for key in ITEM_FIELDS if key in row}
        known.update(number=str(index), currency=order["currency"])
        if "description" not in known and row.get("customs_description"):
            known["description"] = row["customs_description"]
        if "destination_country" in derived:
            known["destination_country"] = derived["destination_country"]
        item = {}
        for key in sorted(ITEM_FIELDS):
            if key in known and key in extras and str(known[key]) != str(extras[key]):
                raise ValueError("报关补充值不能覆盖已保存的订单行事实：" + row["line_id"] + "." + key)
            value = known.get(key, extras.get(key))
            if value is None:
                missing.append("items." + row["line_id"] + "." + key)
            else:
                item[key] = text_value(key, value)
        items.append(item)
        if grouped_items and all(key in item for key in ("quantity", "unit_price", "amount")):
            try:
                quantities = [Decimal(item[key]) for key in ("quantity", "unit_price", "amount")]
            except (InvalidOperation, TypeError):
                raise ValueError("报关汇总数量、单价及金额必须为明确十进制字符串") from None
            grouped_amount_checks.append({"line_id": row["line_id"], **_validate_grouped_item_amount(*quantities)})
    if grouped_items and "total" in order:
        try:
            total = Decimal(order["total"])
        except (InvalidOperation, TypeError):
            raise ValueError("报关分类订单总金额必须为明确十进制数") from None
        with localcontext() as context:
            context.prec = 500
            if not total.is_finite() or total != sum((Decimal(row["amount"]) for row in items), Decimal(0)):
                raise ValueError("报关分类金额合计必须与已验证订单总金额一致")
    return {"ready_for_review_draft": not missing, "missing": missing, "fields": headers, "items": items,
            "line_ids": list(lines), "document_type": "customs", "template_sha256": profile["template_sha256"],
            **({"grouped_items": True, "amount_authority": "caller_verified_sum_of_source_line_amounts",
                "grouped_amount_checks": grouped_amount_checks} if grouped_items else {}),
            "format_accepted": False, "note": ("调用方须保证各分类完整覆盖本批源商品且不重复；汇总金额以源行金额之和为准，四位显示均价乘数量可能存在舍入差额。" if grouped_items else "全部本批源商品均须覆盖；") + "HS、申报要素和双计量须明确输入并核对。此输出未提交，不能视为已报关。"}


def customs_engine_inputs(plan: dict) -> tuple[dict, list]:
    headers, items = deepcopy(plan["fields"]), deepcopy(plan["items"])
    for row, numeric in [(headers, DECIMAL_HEADERS)] + [(row, DECIMAL_ITEMS) for row in items]:
        for key in numeric:
            try:
                row[key] = Decimal(row[key])
            except (InvalidOperation, TypeError, KeyError):
                raise ValueError("报关字段不是明确十进制数：" + key) from None
    return headers, items
