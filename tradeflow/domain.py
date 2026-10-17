"""Deterministic trade rules; no network, files, or invented business facts.

All money and quantities are exact Decimal strings. Additional input fields,
including inquiry_id, original_text, hashes and cell provenance, are preserved.
Workflow evidence is persisted by the caller; this module validates it only.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import re


STATES = (
    "inquiry_received", "technical_ready", "order_confirmed", "shipment_verified",
    "documents_ready", "dispatched", "delivered", "closed",
)
DOCUMENT_TYPES = ("contract", "ci", "pi", "pl", "customs", "receipt")
ITEM_FIELDS = ("product_name", "specification", "unit", "surface_color", "grade")


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _decimal(value: object, field: str, *, positive: bool = False) -> Decimal:
    # Floats cannot preserve the user's original decimal representation.
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{field}: 需要明确的十进制数值，不能缺失或使用浮点数")
    raw = str(value).strip()
    if not raw or len(raw) > 256:
        raise ValueError(f"{field}: 十进制数值为空或过长")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{field}: 不是合法十进制数值") from None
    if not number.is_finite():
        raise ValueError(f"{field}: 数值必须有限")
    if abs(number.as_tuple().exponent) > 100 or number.adjusted() > 100:
        raise ValueError(f"{field}: 超出业务数值精度范围")
    if number < 0 or (positive and number == 0):
        raise ValueError(f"{field}: {'必须大于 0' if positive else '不能为负数'}")
    return number


def _party_valid(value: object, *, address: bool = False) -> bool:
    if isinstance(value, dict):
        return _text(value.get("name")) and (not address or _text(value.get("address")))
    return not address and _text(value)


def normalize_order(payload: dict) -> dict:
    """Validate an order draft; named party strings are allowed until readiness.

    Rich buyer/seller objects require name and address. No unknown numeric value
    defaults to zero. Supplied amount/total must equal the calculated value.
    """
    if not isinstance(payload, dict):
        raise ValueError("订单必须是对象")
    result = deepcopy(payload)
    if not _text(result.get("order_no")):
        raise ValueError("order_no: 缺少订单号")
    result["order_no"] = result["order_no"].strip()
    for field in ("buyer", "seller"):
        value = result.get(field)
        if not _party_valid(value, address=isinstance(value, dict)):
            raise ValueError(f"{field}: 需要非空主体名称；对象需要 name 和 address")
    currency = result.get("currency")
    if not _text(currency) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise ValueError("currency: 需要三字母币种代码")
    result["currency"] = currency.strip().upper()
    if not isinstance(result.get("items"), list) or not result["items"]:
        raise ValueError("items: 订单至少需要一行")
    seen = set()
    with localcontext() as context:
        context.prec = 1024
        total = Decimal("0")
        for index, item in enumerate(result["items"], 1):
            if not isinstance(item, dict):
                raise ValueError(f"items[{index}]: 必须是对象")
            for field in ("line_id",) + ITEM_FIELDS:
                if not _text(item.get(field)):
                    raise ValueError(f"items[{index}].{field}: 缺少明确字段")
                item[field] = item[field].strip()
            if item["line_id"] in seen:
                raise ValueError(f"line_id: 重复行 ID {item['line_id']}")
            seen.add(item["line_id"])
            quantity = _decimal(item.get("quantity"), f"items[{index}].quantity", positive=True)
            price = _decimal(item.get("unit_price"), f"items[{index}].unit_price")
            amount = quantity * price
            if "amount" in item and _decimal(item["amount"], "amount") != amount:
                raise ValueError(f"items[{index}].amount: 与数量乘单价不一致")
            item.update(quantity=str(quantity), unit_price=str(price), amount=str(amount))
            total += amount
        if "total" in result and _decimal(result["total"], "total") != total:
            raise ValueError("total: 与订单行金额汇总不一致")
        result["total"] = str(total)
    return result


def transition(current: str, target: str, evidence: dict) -> str:
    """Validate sequential workflow transition and return the new status.

    Required evidence:
      technical_ready: analysis_id, technical_approved=True
      order_confirmed: order_id, customer_confirmation_ref, commercial_approved=True
      shipment_verified: shipment_id, reconciliation_passed=True
      documents_ready: nonempty document_ids, validation_passed=True
      dispatched: dispatch_ref
      delivered: receipt_ref, signed_at (ISO datetime, actual signature evidence)
      closed: closure_approved=True
    A generated blank receipt never constitutes delivery evidence. References
    must resolve to real records in the caller; boolean flags alone cannot prove
    reconciliation or document validation.
    """
    if current not in STATES or target not in STATES:
        raise ValueError("未知流程状态")
    if not isinstance(evidence, dict):
        raise ValueError("evidence 必须是对象")
    if current == target:
        return current
    if STATES.index(target) != STATES.index(current) + 1:
        raise ValueError("流程状态必须顺序推进，禁止跳步或回退")
    refs = {
        "technical_ready": ("analysis_id",),
        "order_confirmed": ("order_id", "customer_confirmation_ref"),
        "shipment_verified": ("shipment_id",),
        "dispatched": ("dispatch_ref",),
        "delivered": ("receipt_ref", "signed_at"),
    }
    for field in refs.get(target, ()):
        if not _text(evidence.get(field)):
            raise ValueError(f"{target}: 缺少真实证据 {field}")
    flags = {
        "technical_ready": "technical_approved", "order_confirmed": "commercial_approved",
        "shipment_verified": "reconciliation_passed", "documents_ready": "validation_passed",
        "closed": "closure_approved",
    }
    if target in flags and evidence.get(flags[target]) is not True:
        raise ValueError(f"{target}: 需要 {flags[target]}=true")
    if target == "documents_ready":
        identities = evidence.get("document_ids")
        if not isinstance(identities, list) or not identities or not all(_text(x) for x in identities):
            raise ValueError("documents_ready: 缺少已生成并验收的 document_ids")
        if len(identities) != len(set(identities)):
            raise ValueError("documents_ready: document_ids 重复")
    if target == "delivered":
        try:
            timestamp = evidence["signed_at"].replace("Z", "+00:00")
            if "T" not in timestamp and " " not in timestamp:
                raise ValueError
            datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError("delivered: signed_at 必须是实际签收的 ISO 日期时间") from None
    return target


def _reconcile(order: dict, shipment: dict | None) -> dict:
    issues = []
    if not isinstance(shipment, dict):
        return {"passed": False, "issues": ["shipment: 未提供 ERP 出货记录"]}
    items = shipment.get("items")
    if not isinstance(items, list) or not items or not all(isinstance(x, dict) for x in items):
        return {"passed": False, "issues": ["shipment.items: 出货明细为空或无效"]}
    if shipment.get("partial_shipment") is True or order.get("partial_shipment") is True:
        issues.append("partial_shipment: 当前版本不支持部分出货，需明确出货分配后再验收")
    order_items = order.get("items", [])
    use_ids = all(_text(x.get("line_id")) for x in order_items + items)

    def key(item):
        if use_ids:
            return item["line_id"].strip()
        return tuple(str(item.get(field, "")).strip() for field in ITEM_FIELDS)

    def index_rows(rows, label):
        lookup = {}
        for row in rows:
            for field in ITEM_FIELDS:
                if not _text(row.get(field)):
                    issues.append(f"{label}.{field}: 缺少对账字段")
            row_key = key(row)
            if row_key in lookup:
                issues.append(f"{label}: 对账键重复，不能猜测合并行 {row_key}")
            lookup[row_key] = row
        return lookup

    expected, actual = index_rows(order_items, "order"), index_rows(items, "shipment")
    if expected.keys() != actual.keys():
        issues.append("shipment.items: 出货行与订单行不完整匹配；漏行、多行或部分出货均阻断")
    for row_key in expected.keys() & actual.keys():
        before, after = expected[row_key], actual[row_key]
        for field in ITEM_FIELDS:
            if before.get(field, "").strip() != str(after.get(field, "")).strip():
                issues.append(f"shipment[{row_key}].{field}: 与订单不一致")
        try:
            number = _decimal(after.get("quantity"), f"shipment[{row_key}].quantity", positive=True)
            if number != _decimal(before.get("quantity"), "order.quantity", positive=True):
                issues.append(f"shipment[{row_key}].quantity: 与订单不一致；当前不支持部分出货")
        except ValueError as error:
            issues.append(str(error))
    return {"passed": not issues, "issues": issues}


def check_document_readiness(order: dict, shipment: dict | None, template_types: list[str]) -> dict:
    """Readiness for all six documents and independently for each document.

    Templates supplied here must have passed format acceptance in the caller.
    Order fields: buyer/seller={name,address}, incoterm, payment_terms,
    destination_country. Customs additionally needs each item's hs_code and
    origin_country; these are validated as supplied facts, never inferred.
    Shipment fields: shipment_no, consignor/consignee={name,address},
    package_count, package_type, gross_weight, net_weight, weight_unit.
    CI/PL/customs/receipt require complete reconciliation. Contract/PI do not
    wait for shipment. Receipt readiness means preparation of the blank form,
    never actual receipt or signature. ready is the all-six aggregate.
    """
    per_document = {kind: {"ready": False, "issues": []} for kind in DOCUMENT_TYPES}
    try:
        normalized = normalize_order(order)
    except ValueError as error:
        issue = f"order: {error}"
        for result in per_document.values():
            result["issues"].append(issue)
        return {"ready": False, "issues": [issue], "per_document": per_document,
                "reconciliation": {"passed": False, "issues": [issue]}}
    if not isinstance(template_types, list) or not all(isinstance(x, str) for x in template_types):
        raise ValueError("template_types 必须是单据类型列表")
    if any(kind not in DOCUMENT_TYPES for kind in template_types):
        raise ValueError("template_types 含未知单据类型")
    reconciliation = _reconcile(normalized, shipment)
    common = []
    for field in ("buyer", "seller"):
        if not _party_valid(normalized.get(field), address=True):
            common.append(f"order.{field}: 单据需要 name 和 address")
    for field in ("incoterm", "payment_terms", "destination_country"):
        if not _text(normalized.get(field)):
            common.append(f"order.{field}: 缺少已确认商业字段")
    shipment_issues = list(reconciliation["issues"])
    if isinstance(shipment, dict):
        for field in ("shipment_no", "package_type", "weight_unit"):
            if not _text(shipment.get(field)):
                shipment_issues.append(f"shipment.{field}: 缺少出货字段")
        for field in ("consignor", "consignee"):
            if not _party_valid(shipment.get(field), address=True):
                shipment_issues.append(f"shipment.{field}: 需要 name 和 address")
        values = {}
        for field in ("package_count", "gross_weight", "net_weight"):
            try:
                values[field] = _decimal(shipment.get(field), f"shipment.{field}", positive=True)
            except ValueError as error:
                shipment_issues.append(str(error))
        if "package_count" in values and values["package_count"] != values["package_count"].to_integral_value():
            shipment_issues.append("shipment.package_count: 包装件数必须为整数")
        if "gross_weight" in values and "net_weight" in values and values["gross_weight"] < values["net_weight"]:
            shipment_issues.append("shipment.gross_weight: 毛重不能小于净重")
    customs_issues = []
    for item in normalized["items"]:
        hs_code = item.get("hs_code")
        if not _text(hs_code) or not re.fullmatch(r"\d{6,10}", hs_code.strip()):
            customs_issues.append(f"order[{item['line_id']}].hs_code: 需要已确认的 6 至 10 位编码")
        if not _text(item.get("origin_country")):
            customs_issues.append(f"order[{item['line_id']}].origin_country: 缺少原产国")
    for kind, result in per_document.items():
        issues = result["issues"]
        if kind not in template_types:
            issues.append(f"template.{kind}: 缺少已通过格式验收的用户模板")
        issues.extend(common)
        if kind in ("ci", "pl", "customs", "receipt"):
            issues.extend(shipment_issues)
        if kind == "customs":
            issues.extend(customs_issues)
        result["issues"] = list(dict.fromkeys(issues))
        result["ready"] = not result["issues"]
    all_issues = list(dict.fromkeys(issue for result in per_document.values() for issue in result["issues"]))
    return {"ready": not all_issues, "issues": all_issues,
            "per_document": per_document, "reconciliation": reconciliation}
