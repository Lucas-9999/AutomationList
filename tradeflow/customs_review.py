"""Template defaults and per-ticket confirmation of customs red/core cells."""
from copy import deepcopy
from hashlib import sha256
import json
import re

from .profiles import read_values

CORE_HEADERS = {"pre_entry_no", "consignee_text", "contract_no", "package_count", "gross_weight", "net_weight", "incoterm", "marks_and_notes"}
RED_HEADERS = {"domestic_consignor_text", "consignee_text", "contract_no", "package_count", "gross_weight", "net_weight"}
CORE_ITEMS = {"hs_code", "description", "legal_quantity", "legal_unit", "quantity", "unit", "unit_price", "amount", "currency"}
RED_ITEMS = CORE_ITEMS | {"domestic_origin"}
READONLY_ITEMS = {"quantity", "unit", "unit_price", "amount", "currency", "number", "line_id"}


def _text(raw, field):
    value = str(raw or "")
    prefix = field.get("prefix", "")
    if prefix and value.startswith(prefix):
        value = value[len(prefix):]
    labels = {"domestic_consignor_text": "境内发货人", "producer_text": "生产销售单位", "supervision_mode_text": "监管方式", "levy_nature_text": "征免性质", "customs_port_text": "出境关别", "export_date_text": "出口日期", "declaration_date_text": "申报日期", "pre_entry_no": "预录入编号"}
    if field["field"] in labels:
        value = re.sub(r"^" + labels[field["field"]] + r"[：:\s]*", "", value)
    return value.strip()


def default_inputs(content, profile, order, values, extras, bindings):
    """Only template common content is reused; old goods/prices/weights never are."""
    all_values = read_values(content, profile["inspection"])[profile["sheet"]]
    result, output, origin = deepcopy(values), deepcopy(extras), {}
    by_key = {field["field"]: field for field in profile["fields"]}
    for key, field in by_key.items():
        if key in result:
            origin["fields." + key] = "本次填写或已识别信息"
            continue
        if key in {"pre_entry_no", "customs_port_text", "export_date_text", "declaration_date_text"}:
            result[key] = ""
            origin["fields." + key] = "模板空白；本次可填写"
        elif key == "consignee_text":
            if order.get("buyer", {}).get("name"):
                result[key] = order["buyer"]["name"]
                origin["fields." + key] = "ERP客户名称；请核对并补充本批地址"
        elif key == "contract_no":
            result[key] = order["order_no"]
            origin["fields." + key] = "ERP合同/订单号；本票可修改"
        elif key == "incoterm":
            result[key] = order.get("incoterm", "EXW")
            origin["fields." + key] = "模板常用成交方式；每票确认"
        elif key == "miscellaneous_fees_currency":
            result[key] = "CNY"
            origin["fields." + key] = "原模板杂费的人民币格式"
        elif key not in CORE_HEADERS:
            raw = all_values.get(field["cell"])
            value = _text(raw, field)
            if value:
                result[key] = value
                origin["fields." + key] = "模板常用值；可按本批修改"
    item_defaults = {"origin_country": "I14", "destination_country": "J14", "domestic_origin": "L14", "levy_method": "M14", "declaration_elements_text": "B16"}
    for row in order["items"]:
        identity = row["line_id"]
        supplied = output.setdefault(identity, {})
        for key, cell in item_defaults.items():
            if key in supplied:
                origin["items." + identity + "." + key] = "按HS保存的用户格式" if key in bindings.get(identity, {}).get("fields", {}) else "本次填写"
                continue
            value = result.get("destination_country") if key == "destination_country" else all_values.get(cell)
            if value:
                supplied[key] = str(value)
                origin["items." + identity + "." + key] = "模板常用值；可按本批修改"
        # Source ERP weight is never treated as actual net kg. Only explicit
        # packing facts supplied in this job can supply legal kg quantity.
        if "legal_quantity" not in supplied and row.get("net_weight"):
            supplied["legal_quantity"] = row["net_weight"]
            origin["items." + identity + ".legal_quantity"] = "本次填写的逐行净重（千克）"
        if "legal_unit" not in supplied:
            supplied["legal_unit"] = "KG"
            origin["items." + identity + ".legal_unit"] = "用户模板的千克计量格式"
        if "description" in supplied:
            origin["items." + identity + ".description"] = "按HS保存的用户格式" if "description" in bindings.get(identity, {}).get("fields", {}) else "本次填写的申报名称规格"
    custom_order = deepcopy(order)
    derived_keys = {"incoterm", "destination_country", "trade_country", "consignee_text", "contract_no", "domestic_consignor_text", "customs_port_text", "producer_text", "supervision_mode_text", "levy_nature_text", "miscellaneous_fees_currency"}
    for key in derived_keys:
        if key in result:
            custom_order[key] = result[key]
    return result, output, custom_order, origin


def item_metadata(key):
    return {"core": key in CORE_ITEMS, "red": key in RED_ITEMS, "show_primary": key in CORE_ITEMS or key in RED_ITEMS,
            "readonly": key in READONLY_ITEMS}


def header_metadata(key):
    return {"core": key in CORE_HEADERS, "red": key in RED_HEADERS, "show_primary": key in CORE_HEADERS or key in RED_HEADERS}


def confirmation_review(plan, context, confirmations, labels, origin):
    business = deepcopy(context)
    business["request"].pop("customs_confirmations", None)
    business["request"].pop("source_id", None)
    seed = json.dumps(business, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result = []

    def entry(identity, label, value, scope, key, line_id=None, *, red=False, core=True, editable=True, source=None):
        token = sha256((seed + "\n" + identity + "\n" + str(value)).encode()).hexdigest() if value is not None else None
        result.append({"id": identity, "label": label, "value": value, "scope": scope, "key": key, "line_id": line_id,
                       "red": red, "core": core, "editable": editable, "source": source or origin.get(identity, "本次资料"),
                       "token": token, "confirmed": token is not None and confirmations.get(identity) == token})

    for key in ("pre_entry_no", "domestic_consignor_text", "consignee_text", "contract_no", "package_count", "gross_weight", "net_weight", "incoterm", "marks_and_notes"):
        value = plan["fields"].get(key)
        if "fields." + key in plan["missing"]:
            value = None
        entry("fields." + key, labels.get(key, key), value, "fields", key, red=key in RED_HEADERS, core=key in CORE_HEADERS)
    for identity, row in zip(plan["line_ids"], plan["items"]):
        prefix = "items." + identity + "."
        missing = set(plan["missing"])
        groups = [("hs_code", "商品编码（HS编码）", ["hs_code"]), ("description", "商品名称及规格型号", ["description"]),
                  ("quantity_and_units", "数量及单位", ["legal_quantity", "legal_unit", "quantity", "unit"]),
                  ("unit_price", "美元单价", ["unit_price"]), ("amount_and_currency", "总价／币种", ["amount", "currency"]),
                  ("domestic_origin", "境内货源地", ["domestic_origin"])]
        for key, label, members in groups:
            value = " / ".join(str(row[member]) for member in members) if all(member in row and prefix + member not in missing for member in members) else None
            source = origin.get(prefix + key) or ("ERP数量及本次净重；换算值由汇率确定" if key == "quantity_and_units" else "ERP人民币价格按本次汇率换算" if key in {"unit_price", "amount_and_currency"} else "本次填写")
            entry(prefix + key, label, value, "items", key, identity, red=True,
                  core=key != "domestic_origin", editable=key not in {"unit_price", "amount_and_currency"}, source=source)
    return {"items": result, "confirmed_count": sum(item["confirmed"] for item in result), "total_count": len(result)}
