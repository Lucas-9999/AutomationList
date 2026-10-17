"""Connect persisted business facts to known template cells without guessing."""
from copy import deepcopy
from decimal import Decimal
import re


NUMERIC_ITEM_FIELDS = {"number", "quantity", "unit_price", "amount", "net_weight", "gross_weight", "small_box_count"}


def prepare_document(profile: dict, order: dict, fields: dict, item_extras: dict, *, allow_partial_slots: bool = False) -> dict:
    """Return a fill plan and missing inputs; no workbook is generated here.

    Header supplements are flat profile field names. Item supplements are keyed
    by immutable order line_id and cannot change order quantity, unit or price.
    Empty text must be explicitly supplied rather than copied from old cells.
    """
    if profile["document_type"] not in {"ci", "pi", "contract", "pl", "receipt"}:
        raise ValueError("当前草稿填充支持合同、CI、PI、PL、签收单；报关单待专用适配")
    if order.get("currency") != "USD":
        raise ValueError("当前已知模板的银行和金额格式按 USD 设置，其他币种需专用映射")
    fixed = profile.get("fixed_context", {})
    if fixed.get("seller_names"):
        seller = order.get("seller")
        name = seller.get("name") if isinstance(seller, dict) else seller
        normalize_name = lambda value: re.sub(r"[\W_]", "", value or "").casefold()
        if normalize_name(name) not in {normalize_name(x) for x in fixed["seller_names"]}:
            raise ValueError("订单卖方与模板固定抬头不一致，需要专用卖方模板")
    if not isinstance(fields, dict) or not isinstance(item_extras, dict):
        raise ValueError("fields 和 item_extras 必须是对象")
    wanted = {x["field"] for x in profile["fields"]}
    if set(fields) - wanted:
        raise ValueError("存在未映射的表头字段：" + ", ".join(sorted(set(fields) - wanted)))
    order_lines = {x["line_id"]: x for x in order["items"]}
    if set(item_extras) - set(order_lines):
        raise ValueError("补充资料引用了不存在的订单行")
    derived = {}
    buyer = order.get("buyer")
    if isinstance(buyer, dict):
        derived.update({"buyer." + key: value for key, value in buyer.items() if key in {"name", "address", "address_extra", "contact"}})
    elif isinstance(buyer, str):
        derived["buyer.name"] = buyer
    for key in ("order_no", "consignee_text", "trade_terms", "payment_terms", "delivery_date"):
        if key in order and isinstance(order[key], str) and order[key].strip():
            derived[key] = order[key]
    if "incoterm" in order:
        incoterm = order["incoterm"]
        if not isinstance(incoterm, str) or not re.fullmatch(r"[A-Za-z]{3}", incoterm.strip()):
            raise ValueError("已存订单 incoterm 必须是明确的三字母贸易术语")
        proposed = derived.get("trade_terms", fields.get("trade_terms"))
        if proposed is not None and (not isinstance(proposed, str) or not re.search(r"\b" + re.escape(incoterm.strip()) + r"\b", proposed, re.I)):
            raise ValueError("trade_terms 与已存订单 incoterm 不一致")
        if fixed.get("incoterm") and incoterm.strip().upper() != fixed["incoterm"]:
            raise ValueError("已存订单 incoterm 与模板固定条款不一致")
    output_fields, missing = {}, []
    for key in sorted(wanted):
        value = derived.get(key, fields.get(key))
        if key in derived and key in fields and fields[key] != value:
            raise ValueError("补充值不能覆盖已保存的订单事实：" + key)
        if value is None:
            missing.append("fields." + key)
        elif not isinstance(value, str):
            raise ValueError("表头值必须是字符串：" + key)
        else:
            output_fields[key] = value
    slots = profile["item_slots"]
    if (not isinstance(allow_partial_slots, bool)):
        raise ValueError("allow_partial_slots 必须是布尔值")
    count_matches = 1 <= len(order["items"]) <= len(slots) if allow_partial_slots else len(slots) == len(order["items"])
    if not count_matches:
        missing.append(f"template_capacity: 模板 {len(slots)} 行，本次业务 {len(order['items'])} 行；需匹配容量模板")
    output_items = []
    for index, row in enumerate(order["items"], 1):
        # Preserve every source line in the fill plan even when capacity blocks output.
        slot = slots[min(index - 1, len(slots) - 1)] if slots else {"cells": {}}
        extras = item_extras.get(row["line_id"], {})
        if not isinstance(extras, dict) or set(extras) - set(slot["cells"]):
            raise ValueError("明细补充字段不在模板映射内：" + row["line_id"])
        known = {key: row[key] for key in slot["cells"] if key in row}
        known["number"] = str(index)
        if "description" not in known and isinstance(row.get("description_en"), str):
            known["description"] = row["description_en"]
        item = {}
        for key in slot["cells"]:
            if key in known and key in extras and str(extras[key]) != str(known[key]):
                raise ValueError(f"不能覆盖已保存的订单行事实：{row['line_id']}.{key}")
            value = known.get(key, extras.get(key))
            if value is None:
                missing.append("items." + row["line_id"] + "." + key)
            elif not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
                raise ValueError("明细值必须是明确文本或十进制字符串")
            else:
                item[key] = str(value)
        output_items.append(item)
    return {"ready_for_review_draft": not missing, "missing": missing, "fields": output_fields,
            "items": output_items, "line_ids": list(order_lines), "document_type": profile["document_type"],
            "template_sha256": profile["template_sha256"], "format_accepted": False,
            "note": "沿用模板固定公司抬头、银行和条款，仍需逐项核对；本结果不是签署或正式单据验收。"}


def decimal_items(plan: dict) -> list:
    result = deepcopy(plan["items"])
    for item in result:
        for key in NUMERIC_ITEM_FIELDS & item.keys():
            # The draft engine applies finite/range/business validation as well.
            try:
                item[key] = Decimal(item[key])
            except Exception as exc:
                raise ValueError("草稿明细不是十进制数：" + key) from exc
    return result
