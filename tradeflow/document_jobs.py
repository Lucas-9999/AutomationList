"""ERP-based document sessions: no duplicate order entry or shipment assertion."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import ZipFile, ZIP_DEFLATED

from .customs import KNOWN_CUSTOMS, inspect_customs_template, generate_customs_draft
from .customs_data import prepare_customs_document, customs_engine_inputs
from .document_data import prepare_document, decimal_items
from .drafts import KNOWN_TEMPLATES, generate_review_draft
from .fx import project_erp_source, convert_erp_prices
from .layout_review import inspect_text_fit
from .profiles import candidate_profile
from .receipts import KNOWN_RECEIPTS, inspect_receipt_template, generate_receipt_draft
from .store import ConflictError
from .hs_formats import apply_formats
from .hs_catalog_bindings import validate_bindings, review_bindings
from .customs_review import default_inputs, header_metadata, item_metadata, confirmation_review
from .local_settings import default_seller_name


DOCUMENT_LABELS = {"ci": "CI 商业发票", "pi": "PI 形式发票", "pl": "PL 装箱单", "customs": "报关单", "receipt": "发货签收单"}
VERSION = "erp-document-job-v3-customs-categories"
OPTIONAL_HEADERS = {"buyer.address_extra", "buyer.contact", "consignee_text", "delivery_date", "validity", "remittance_reference_text", "marks_and_notes"}
OPTIONAL_ITEMS = {"remark", "carton_numbers"}
CUSTOMS_BLANK_HEADERS = {"pre_entry_no", "customs_port_text", "export_date_text", "declaration_date_text"}
ITEM_INPUTS = {"description", "description_en", "surface_color_en", "material", "remark", "carton_numbers", "net_weight", "gross_weight", "small_box_count", "hs_code", "legal_quantity", "legal_unit", "origin_country", "destination_country", "domestic_origin", "levy_method", "declaration_elements_text"}
DECIMAL_FIELDS = {"net_weight", "gross_weight", "small_box_count", "legal_quantity", "package_count", "miscellaneous_fees"}
LABELS = {"document_no": "单据编号", "issue_date": "制单日期", "buyer.name": "买方开票名称", "buyer.address": "买方地址", "buyer.address_extra": "买方地址补充", "buyer.contact": "买方联系方式", "consignee_text": "收货方", "trade_terms": "贸易条款", "payment_terms": "付款条款", "delivery_date": "交期", "validity": "有效期", "remittance_reference_text": "汇款备注", "description": "英文品名及技术说明", "material": "材质", "remark": "备注", "carton_numbers": "箱号", "net_weight": "净重（kg）", "gross_weight": "毛重（kg）", "small_box_count": "小盒数量", "hs_code": "HS编码", "legal_quantity": "法定数量", "legal_unit": "法定计量单位", "origin_country": "原产国", "destination_country": "运抵国", "domestic_origin": "境内货源地", "levy_method": "征免方式", "declaration_elements_text": "申报要素", "domestic_consignor_text": "境内发货人", "customs_port_text": "出境关别", "export_date_text": "出口日期", "declaration_date_text": "申报日期", "transport_mode": "运输方式", "producer_text": "生产销售单位", "supervision_mode_text": "监管方式", "levy_nature_text": "征免性质", "contract_no": "合同号", "trade_country": "贸易国", "destination_port": "指运港", "package_type": "包装种类", "package_count": "包装件数", "incoterm": "成交方式", "miscellaneous_fees": "杂费金额", "miscellaneous_fees_currency": "杂费币种", "marks_and_notes": "标记唛码及备注"}
TERMS = {"内六角圆柱头螺钉": "Hexagon socket head cap screw"}
SURFACES = {"黑锌": "Black zinc plated", "镀镍": "Nickel plated"}
UNITS = {"支": "PCS", "个": "PCS", "件": "PCS", "PCS": "PCS", "pcs": "PCS"}
LABELS.update(pre_entry_no="预录入编号", hs_code="商品编码（HS编码）", legal_quantity="法定数量（千克）", description="商品名称及规格型号", consignee_text="境外收货人", contract_no="合同协议号")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _engine_version():
    engine = sha256()
    for name in ("document_jobs.py", "customs_groups.py", "customs_review.py", "hs_formats.py", "hs_catalog_bindings.py", "fx.py", "drafts.py", "receipts.py", "customs.py", "document_data.py", "customs_data.py", "calculations.py", "profiles.py", "templates.py"):
        engine.update((Path(__file__).parent / name).read_bytes())
    return engine.hexdigest()


def _original(store, record):
    payload = record["payload"]
    digest = payload.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("来源原件哈希无效")
    try:
        content = (store.root / "raw" / digest).read_bytes()
    except OSError:
        raise ValueError("来源原件不可读取") from None
    if sha256(content).hexdigest() != digest:
        raise ValueError("来源原件哈希核验失败")
    return content


def source_projection(store, identity):
    record = store.get(identity)
    if record["kind"] not in {"erp_source", "shipment"}:
        raise ValueError("请选择已导入的ERP Excel来源")
    _original(store, record)
    return project_erp_source(record)


def list_sources(store):
    candidates = store.list_records("erp_source", limit=10000) + sorted(store.list_records("shipment", limit=10000), key=lambda row: row["created_at"])
    result, seen = [], set()
    for record in candidates:
        payload = record["payload"]
        key = (payload.get("sha256"), payload.get("source", {}).get("sheet"))
        if key in seen:
            continue
        try:
            projection = source_projection(store, record["id"])
        except ValueError:
            continue
        seen.add(key)
        result.append(projection)
    return result


def _profile(content, kind):
    if kind == "receipt":
        return inspect_receipt_template(content)
    if kind == "customs":
        return inspect_customs_template(content, core_fields_mode=True)
    return candidate_profile(content, kind)


def _default_template(store, kind, line_count):
    candidates = []
    for record in store.list_records("template", limit=10000):
        payload = record["payload"]
        if payload.get("document_type") != kind:
            continue
        digest = payload.get("sha256")
        known = (digest in KNOWN_RECEIPTS if kind == "receipt" else digest in KNOWN_CUSTOMS if kind == "customs" else KNOWN_TEMPLATES.get(digest) == kind)
        if not known:
            continue
        content = _original(store, record)
        profile = _profile(content, kind)
        capacity = profile.get("line_capacity", profile.get("source_line_capacity", 0))
        if kind == "customs" and profile.get("generation_supported"):
            capacity = max(capacity, line_count)
        if capacity >= line_count:
            candidates.append((capacity, digest, record, content, profile))
    if not candidates:
        raise ValueError(DOCUMENT_LABELS[kind] + "尚无可覆盖全部商品的已适配默认模板")
    _, _, record, content, profile = sorted(candidates, key=lambda row: row[:2])[0]
    return record, content, profile


def _validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("制单请求必须是对象")
    validate_bindings(request)
    if request.get("customs_groups") is not None and not isinstance(request["customs_groups"], list):
        raise ValueError("报关分类须为列表")
    kinds = request.get("document_types")
    if not isinstance(kinds, list) or not kinds or any(kind not in DOCUMENT_LABELS for kind in kinds) or len(set(kinds)) != len(kinds):
        raise ValueError("请选择至少一种支持的单据，不能重复")
    for key in ("fields", "document_fields", "item_extras"):
        if not isinstance(request.get(key, {}), dict):
            raise ValueError("制单补充信息格式无效")
    fields = request.get("fields", {})
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in fields.items()):
        raise ValueError("补充信息须使用明确文本或十进制字符串")
    for kind, values in request.get("document_fields", {}).items():
        if kind not in kinds or not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
            raise ValueError("单据专用补充信息无效")
    for identity, values in request.get("item_extras", {}).items():
        if not isinstance(identity, str) or not isinstance(values, dict) or set(values) - ITEM_INPUTS or any(not isinstance(value, str) for value in values.values()):
            raise ValueError("商品补充值无效，不能覆盖ERP数量、单价、金额或规格")
    confirmations = request.get("customs_confirmations", {})
    if not isinstance(confirmations, dict) or any(not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for key, value in confirmations.items()):
        raise ValueError("报关核对记录无效，请重新核对本票资料")


def _order(conversion, fields, item_extras):
    header = conversion.get("header", {})
    number = header.get("order_no") or "ERP-" + conversion["source_sha256"][:12].upper()
    if fields.get("order_no", number) != number:
        raise ValueError("ERP订单编号不能被补充值覆盖；单据编号可单独填写")
    items = []
    for raw in conversion["items"]:
        extras = item_extras.get(raw["line_id"], {})
        item = {key: raw[key] for key in ("line_id", "product_name", "specification", "quantity", "grade")}
        item.update(unit=UNITS.get(raw["unit"], raw["unit"]), source_unit=raw["unit"],
                    unit_price=raw["unit_price_usd"], amount=raw["amount_usd"],
                    surface_color=extras.get("surface_color_en", SURFACES.get(raw["surface_color"], raw["surface_color"])))
        description = extras.get("description", extras.get("description_en", TERMS.get(raw["product_name"])))
        if description:
            item["description_en"] = description
        if raw.get("material"):
            item["material"] = raw["material"]
        for key in ITEM_INPUTS - {"description", "description_en", "surface_color_en"}:
            if key in extras:
                if key == "material" and item.get(key) and item[key] != extras[key]:
                    raise ValueError("材质补充值不能覆盖ERP材质原文")
                item[key] = extras[key]
        item.setdefault("remark", "Grade: " + raw["grade"])
        item.setdefault("carton_numbers", "")
        items.append(item)
    buyer = {key: value for key, value in {"name": fields.get("buyer.name", header.get("buyer_name")), "address": fields.get("buyer.address")}.items() if value}
    order = {"order_no": number, "contract_no": number, "currency": "USD", "items": items,
            "total": conversion["totals"]["total_usd"],
            "buyer": buyer,
            "seller": {"name": header.get("seller_name") or default_seller_name()},
            "incoterm": fields.get("incoterm", "EXW"),
            "trade_terms": fields.get("trade_terms", "EXW – Ex Works"),
            "payment_terms": fields.get("payment_terms", "30% down payment as per signing the PO, 100% PREPAID before shipment")}
    for key in ("destination_country", "trade_country", "consignee_text", "domestic_consignor_text", "customs_port_text", "producer_text", "supervision_mode_text", "levy_nature_text", "miscellaneous_fees_currency"):
        if fields.get(key):
            order[key] = fields[key]
    return order


def _document_fields(order, profile, common, specific):
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    defaults = {key: "" for key in OPTIONAL_HEADERS}
    if profile["document_type"] == "customs":
        defaults.pop("marks_and_notes", None)
        defaults.pop("consignee_text", None)
    defaults.update(document_no=order["order_no"], issue_date=today, order_no=order["order_no"])
    wanted = {field["field"] for field in profile["fields"]}
    values = {key: value for key, value in defaults.items() if key in wanted}
    values.update({key: value for key, value in common.items() if key in wanted})
    values.update(specific)
    return values


def _field_spec(scope, key, value, missing, kinds, line_id=None):
    return {"scope": scope, "key": key, "line_id": line_id, "label": "ERP订单号" if key == "order_no" else LABELS.get(key, key),
            "input_type": "decimal" if key in DECIMAL_FIELDS else "date" if key in {"issue_date"} else "textarea" if key in {"description", "declaration_elements_text", "marks_and_notes", "consignee_text", "domestic_consignor_text", "producer_text"} or (isinstance(value, str) and "\n" in value) else "text",
            "allow_empty": key in ((CUSTOMS_BLANK_HEADERS if "customs" in kinds else OPTIONAL_HEADERS) if scope == "fields" else OPTIONAL_ITEMS),
            "value": value, "missing": missing, "documents": kinds}


def _weight_consistency(order, fields, document_fields, kinds):
    """Compare explicit kg facts across this document set; never infer ERP weight."""
    def weight(value):
        try:
            result = Decimal(value)
        except (InvalidOperation, TypeError):
            raise ValueError("净毛重及法定数量须为明确十进制数") from None
        if not result.is_finite() or result < 0 or len(result.as_tuple().digits) > 64 or abs(result.as_tuple().exponent) > 30:
            raise ValueError("净毛重及法定数量无效")
        return result

    totals = {}
    with localcontext() as context:
        context.prec = 200
        for row in order["items"]:
            if "net_weight" in row and "gross_weight" in row and weight(row["gross_weight"]) < weight(row["net_weight"]):
                raise ValueError("商品毛重不得小于净重：" + row["line_id"])
            if "customs" in kinds and row.get("legal_unit") in {"KG", "千克"} and "legal_quantity" in row and "net_weight" in row:
                if weight(row["legal_quantity"]) != weight(row["net_weight"]):
                    raise ValueError("报关法定千克数量与商品净重不一致：" + row["line_id"])
        for key in ("net_weight", "gross_weight"):
            if all(key in row and row[key].strip() for row in order["items"]):
                totals[key] = sum((weight(row[key]) for row in order["items"]), Decimal(0))
        if "customs" in kinds:
            specific = document_fields.get("customs", {})
            for key, total in totals.items():
                for provided in (fields, specific):
                    if key in provided and weight(provided[key]) != total:
                        raise ValueError("报关" + LABELS[key] + "与商品逐行合计不一致")
                fields.setdefault(key, format(total, "f"))
            if all("legal_quantity" in row and row.get("legal_unit") in {"KG", "千克"} for row in order["items"]):
                legal_total = sum((weight(row["legal_quantity"]) for row in order["items"]), Decimal(0))
                for provided in (fields, specific):
                    if "net_weight" in provided and weight(provided["net_weight"]) != legal_total:
                        raise ValueError("报关净重与法定千克数量合计不一致")


def _grouped_customs_inputs(store, conversion, fields, extras, document_fields, group_requests):
    from .customs_groups import prepare_groups
    state = prepare_groups(store, conversion, group_requests)
    grouped_extras = deepcopy(state["extras"])
    single_from_header, member_weights = set(), set()

    def net_value(raw):
        try:
            value = Decimal(raw)
        except (InvalidOperation, TypeError):
            raise ValueError("类别净重须为明确十进制数") from None
        if not value.is_finite() or value < 0 or len(value.as_tuple().digits) > 64 or abs(value.as_tuple().exponent) > 30:
            raise ValueError("类别净重无效")
        return value
    known_totals = []
    for group in state["groups"]:
        identity = group["group_id"]
        values = grouped_extras.setdefault(identity, {})
        values.setdefault("legal_unit", "KG")
        # Only this job's explicitly supplied physical weights are eligible.
        weights = [extras.get(line, {}).get("net_weight") for line in group["member_line_ids"]]
        known = [net_value(value) for value in weights if value is not None and str(value).strip()]
        with localcontext() as precision:
            precision.prec = 200
            total = sum(known, Decimal(0))
        known_totals.append(total)
        if weights and len(known) == len(weights):
            if "legal_quantity" in values and net_value(values["legal_quantity"]) != total:
                raise ValueError("分类净重与其产品本次填写的净重合计不一致")
            values.setdefault("legal_quantity", format(total, "f"))
            member_weights.add(identity)
        if len(state["groups"]) == 1 and not values.get("legal_quantity"):
            single_from_header.add(identity)
            net = document_fields.get("customs", {}).get("net_weight", fields.get("net_weight"))
            if isinstance(net, str) and net.strip():
                values["legal_quantity"] = net
        if values.get("legal_quantity") and total > net_value(values["legal_quantity"]):
            raise ValueError("类别净重不得小于已填写产品的净重合计")
    with localcontext() as precision:
        precision.prec = 200
        known_total = sum(known_totals, Decimal(0))
    for provided in (fields, document_fields.get("customs", {})):
        if provided.get("net_weight") and known_total > net_value(provided["net_weight"]):
            raise ValueError("报关净重不得小于已填写产品的净重合计")
    grouped_order = _order(state["conversion"], fields, grouped_extras)
    by_id = {row["line_id"]: row for row in state["conversion"]["items"]}
    for row in grouped_order["items"]:
        row["unit"] = by_id[row["line_id"]]["unit"]
        if row.get("legal_quantity"):
            row["net_weight"] = row["legal_quantity"]
    _weight_consistency(grouped_order, fields, document_fields, ["customs"])
    state.update(order=grouped_order, effective_extras=grouped_extras,
                 single_from_header=single_from_header, member_weights=member_weights)
    return state


def prepare_job(store, request):
    _validate_request(request)
    source = source_projection(store, request["source_id"])
    conversion = convert_erp_prices(source, request.get("exchange_rate"))
    fields, extras = deepcopy(request.get("fields", {})), request.get("item_extras", {})
    if set(fields) - set(LABELS) - {"order_no"}:
        raise ValueError("存在未映射的补充字段")
    line_ids = {row["line_id"] for row in conversion["items"]}
    if set(extras) - line_ids:
        raise ValueError("补充信息引用了其他ERP文件的商品行")
    order = _order(conversion, fields, extras)
    _weight_consistency(order, fields, request.get("document_fields", {}), request["document_types"])
    grouped = request.get("customs_groups") is not None and "customs" in request["document_types"]
    group_state = _grouped_customs_inputs(store, conversion, fields, extras, request.get("document_fields", {}), request["customs_groups"]) if grouped else None
    if grouped:
        if request.get("hs_catalog_bindings"):
            raise ValueError("分类报关请在类别内引用HS目录，不能混用逐产品目录引用")
        catalog_request = {"hs_catalog_bindings": group_state["catalog_bindings"], "item_extras": group_state["effective_extras"]}
        catalog_review, catalog_context = review_bindings(store, catalog_request, {row["group_id"] for row in group_state["groups"]})
    else:
        catalog_review, catalog_context = review_bindings(store, request, line_ids) if "customs" in request["document_types"] else ([], [])
    documents, specs, prepared = [], {}, {}
    customs_origin, hs_bindings = {}, {}
    for kind in request["document_types"]:
        document_order = group_state["order"] if grouped and kind == "customs" else order
        template, content, profile = _default_template(store, kind, len(document_order["items"]))
        wanted = {field["field"] for field in profile["fields"]}
        specific = request.get("document_fields", {}).get(kind, {})
        if set(specific) - wanted:
            raise ValueError("存在所选单据未使用的专用补充字段")
        values = _document_fields(document_order, profile, fields, specific)
        if kind == "customs":
            customs_conversion = group_state["conversion"] if grouped else conversion
            customs_extras = group_state["effective_extras"] if grouped else extras
            formatted_extras, hs_bindings = apply_formats(store, customs_conversion, customs_extras)
            values, formatted_extras, customs_order, customs_origin = default_inputs(content, profile, document_order, values, formatted_extras, hs_bindings)
            if grouped:
                for group in group_state["groups"]:
                    prefix = "items." + group["group_id"] + "."
                    customs_origin[prefix + "unit_price"] = "本类美元金额合计÷成交数量合计，四位显示；不回算总额"
                    customs_origin[prefix + "amount_and_currency"] = "本类原ERP商品换算后逐行美元金额之和"
                    customs_origin[prefix + "quantity_and_units"] = "本类ERP数量合计及本次实际类别净重"
                    if group["group_id"] in group_state["single_from_header"]:
                        customs_origin[prefix + "legal_quantity"] = "本票仅一类，直接使用本票净重"
                    elif group["group_id"] in group_state["member_weights"]:
                        customs_origin[prefix + "legal_quantity"] = "所含产品本次明确填写的实际净重合计"
            filtered = {identity: {key: value for key, value in row.items() if key in profile.get("structured_item_fields", [])} for identity, row in formatted_extras.items()}
            plan = prepare_customs_document(profile, customs_order, values, filtered, allow_multiple_items=True, **({"grouped_items": True} if grouped else {}))
        else:
            allowed = set().union(*(slot["cells"] for slot in profile["item_slots"]))
            filtered = {identity: {key: value for key, value in row.items() if key in allowed} for identity, row in extras.items()}
            plan = prepare_document(profile, order, values, filtered, allow_partial_slots=True)
        # Empty required fields remain actionable missing inputs in the preview.
        blank_headers = CUSTOMS_BLANK_HEADERS if kind == "customs" else OPTIONAL_HEADERS
        for key, value in plan["fields"].items():
            if key not in blank_headers and isinstance(value, str) and not value.strip():
                plan["missing"].append("fields." + key)
        for line_id, row in zip(plan["line_ids"], plan["items"]):
            for key, value in row.items():
                if key not in OPTIONAL_ITEMS and isinstance(value, str) and not value.strip():
                    plan["missing"].append("items." + line_id + "." + key)
        plan["missing"] = list(dict.fromkeys(plan["missing"]))
        plan["ready_for_review_draft"] = not plan["missing"]
        documents.append({"document_type": kind, "label": DOCUMENT_LABELS[kind], "template_id": template["id"],
                          "template_filename": template["payload"]["filename"], "template_sha256": template["payload"]["sha256"],
                          "ready": plan["ready_for_review_draft"], "missing": plan["missing"], "note": plan["note"]})
        prepared[kind] = (template, content, profile, plan)
        for field in profile["fields"]:
            key = field["field"]
            value = plan["fields"].get(key)
            identity = ("fields", key, None)
            spec = _field_spec("fields", key, value, "fields." + key in plan["missing"], [kind])
            if kind == "customs":
                spec.update(header_metadata(key), source=customs_origin.get("fields." + key, "ERP或本次填写"))
            if identity in specs:
                specs[identity]["documents"].append(kind)
                specs[identity]["missing"] |= spec["missing"]
                specs[identity]["allow_empty"] &= spec["allow_empty"]
                if kind == "customs":
                    specs[identity].update(header_metadata(key), source=spec["source"])
                    specs[identity]["value"] = value
            else:
                specs[identity] = spec
        for line in document_order["items"]:
            if kind == "customs":
                planned = plan["items"][plan["line_ids"].index(line["line_id"])]
                for key in profile["structured_item_fields"]:
                    if key in {"line_id", "number"}:
                        continue
                    identity = ("items", key, line["line_id"])
                    metadata = item_metadata(key)
                    # The read-only ERP quantity and prices appear in the review
                    # table, not duplicate editable supplemental fields.
                    if metadata["readonly"]:
                        continue
                    spec = _field_spec("items", key, planned.get(key), "items." + line["line_id"] + "." + key in plan["missing"], [kind], line["line_id"])
                    spec.update(metadata, source=customs_origin.get("items." + line["line_id"] + "." + key, "本次填写"))
                    if grouped:
                        group = next(row for row in group_state["groups"] if row["group_id"] == line["line_id"])
                        spec.update(group_id=group["group_id"], group_name=group["name"], member_line_ids=group["member_line_ids"], member_count=len(group["member_line_ids"]))
                        if key == "legal_quantity":
                            spec["label"] = "本类净重（千克）"
                            if line["line_id"] in group_state["single_from_header"] | group_state["member_weights"]:
                                spec.update(readonly=True)
                    if identity in specs:
                        spec["documents"] = list(dict.fromkeys(specs[identity]["documents"] + [kind]))
                    specs[identity] = spec
                continue
            for path in plan["missing"]:
                prefix = "items." + line["line_id"] + "."
                if not path.startswith(prefix):
                    continue
                key = path[len(prefix):]
                identity = ("items", key, line["line_id"])
                spec = _field_spec("items", key, extras.get(line["line_id"], {}).get(key), True, [kind], line["line_id"])
                if identity in specs:
                    specs[identity]["documents"].append(kind)
                else:
                    specs[identity] = spec
            provided = extras.get(line["line_id"], {})
            editable = (set(profile.get("structured_item_fields", [])) if kind == "customs" else
                        set().union(*(slot["cells"] for slot in profile["item_slots"])))
            for key, value in provided.items():
                if key not in editable:
                    continue
                identity = ("items", key, line["line_id"])
                if identity not in specs:
                    specs[identity] = _field_spec("items", key, value, False, [kind], line["line_id"])
                elif kind not in specs[identity]["documents"]:
                    specs[identity]["documents"].append(kind)
    context = {"version": VERSION, "request": {key: deepcopy(value) for key, value in request.items() if key != "preview_key"},
               "source_sha256": source["source_sha256"], "source_sheet": source["source_sheet"], "templates": [{"type": row["document_type"], "sha256": row["template_sha256"]} for row in documents],
               "engine": _engine_version(), "effective_fields": {kind: values[3]["fields"] for kind, values in prepared.items()},
               "effective_items": {kind: values[3]["items"] for kind, values in prepared.items()}, "hs_formats": hs_bindings,
               "hs_catalog": catalog_context}
    if grouped:
        context["request"]["customs_groups"] = [
            {"group_id": row["group_id"], "member_line_ids": row["member_line_ids"],
             "category_id": row.get("category_id"), "fields": row["fields"],
             "catalog_snapshot_id": row.get("catalog_snapshot_id")} for row in group_state["groups"]]
        context["customs_groups"] = deepcopy(group_state["groups"])
        context["customs_category_bindings"] = deepcopy(group_state["category_bindings"])
        context["customs_grouping"] = True
    review = None
    if "customs" in prepared:
        plan = prepared["customs"][3]
        review = confirmation_review(plan, context, request.get("customs_confirmations", {}), LABELS, customs_origin)
        if "customs_confirmations" in context["request"]:
            review_ids = {item["id"] for item in review["items"]}
            context["request"]["customs_confirmations"] = {
                key: value for key, value in context["request"]["customs_confirmations"].items() if key in review_ids}
        unconfirmed = review["total_count"] - review["confirmed_count"]
        for document in documents:
            if document["document_type"] == "customs":
                document["unconfirmed_count"] = unconfirmed
                document["ready"] = document["ready"] and not unconfirmed and not any(row["blocked"] for row in catalog_review)
                document["catalog_blocked"] = any(row["blocked"] for row in catalog_review)
                if grouped:
                    document["category_blocked"] = any(row.get("category_stale") for row in group_state["groups"])
                    document["ready"] = document["ready"] and not document["category_blocked"]
        if grouped:
            group_names = {row["group_id"]: row["name"] for row in group_state["groups"]}
            for item in review["items"]:
                if item.get("line_id") in group_names:
                    item.update(group_id=item["line_id"], group_name=group_names[item["line_id"]])
    fingerprint = deepcopy(context)
    fingerprint["request"].pop("source_id", None)
    public = {"preview_key": sha256(_canonical(fingerprint).encode()).hexdigest(), "source_id": request["source_id"], "conversion": conversion,
              "documents": documents, "field_specs": list(specs.values()), "ready": all(row["ready"] for row in documents),
              "warnings": ["销售合同仅作为制单来源，生成单据不表示货物已发出、已报关或已签收。", "原表重量和包规未注明实际净毛重及包装事实，相关单据需要明确补充。", "美元金额按四位美元单价计算并逐行保留两位，总额可能与人民币总额直接除汇率存在舍入差额。"]}
    if review is not None:
        public["customs_review"] = review
        public["hs_catalog_review"] = catalog_review
        if any(row["blocked"] for row in catalog_review):
            public["warnings"].append("所选HS目录记录已更新、过期、超过本机缓存有效期或法定单位未适配；请查看目录核对提示，刷新并重新应用。取消目录引用后仍须按实际货物手动核对。")
        if any(row["unit_unsupported"] for row in catalog_review):
            public["warnings"].append("所查HS的第一法定单位不为千克；当前报关模板引擎仅适配KG／千克，需要专用适配，不能直接套用。")
    if grouped:
        public["customs_groups"] = deepcopy(group_state["groups"])
        public["warnings"].append("报关按类别汇总原明细数量和美元金额；四位显示单价由总金额÷总数量倒算，显示单价乘数量的差额不会改写总额。")
        for group in group_state["groups"]:
            public["warnings"].extend(group.get("warnings", []))
            if group.get("category_stale"):
                public["warnings"].append(group["name"] + "的常用类别已有新版本，请重新选择当前版本后核对。")
    return public, prepared, context


def create_job(store, request):
    public, prepared, context = prepare_job(store, request)
    if request.get("preview_key") != public["preview_key"]:
        raise ConflictError("制单数据或模板已变化，请重新预览并确认本次汇率")
    if not public["ready"]:
        raise ValueError("所选单据仍有缺项或报关红字/核心项目尚未逐项核对，请完成后重新预览")
    key = public["preview_key"]
    previous = store.find("document_job", key)
    if previous:
        return previous
    staged = []
    for kind in request["document_types"]:
        template, content, profile, plan = prepared[kind]
        if kind == "customs":
            headers, items = customs_engine_inputs(plan)
            output, report = generate_customs_draft(content, headers, items, allow_multiple_items=True, amount_decimals=2, core_fields_mode=True,
                                                   **({"grouped_items": True} if context.get("customs_grouping") else {}))
        elif kind == "receipt":
            output, report = generate_receipt_draft(content, plan["fields"], decimal_items(plan), allow_partial_slots=True)
        else:
            output, report = generate_review_draft(content, kind, plan["fields"], decimal_items(plan), allow_partial_slots=True, amount_decimals=2)
        report["layout_preflight"] = inspect_text_fit(output)
        staged.append((kind, output, report, template))
    number = public["conversion"].get("header", {}).get("order_no") or public["conversion"]["source_sha256"][:12]
    safe_number = re.sub(r"[^\w.\-]", "_", number)[:80]
    files, archive = [], BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
        for kind, output, report, template in staged:
            filename = kind.upper() + "-" + safe_number + ".xlsx"
            digest = store.save_original(output)
            files.append({"id": kind, "document_type": kind, "filename": filename, "sha256": digest, "report": report,
                          "template_id": template["id"], "template_sha256": template["payload"]["sha256"]})
            package.writestr(filename, output)
    zip_digest = store.save_original(archive.getvalue())
    payload = {"source_id": request["source_id"], "source_sha256": public["conversion"]["source_sha256"],
               "source_order_no": number, "exchange_rate": public["conversion"]["exchange_rate"],
               "rounding_policy": public["conversion"]["rounding_policy"], "conversion": public["conversion"],
               "totals": public["conversion"]["totals"], "document_types": request["document_types"],
               "request": context["request"], "engine_version": context["engine"], "preview_key": key,
               "raw_request": deepcopy(request),
               "effective_fields": context["effective_fields"], "effective_items": context["effective_items"],
               "hs_format_bindings": context["hs_formats"], "hs_catalog_bindings": context["hs_catalog"], "customs_review": public.get("customs_review"),
               "customs_groups": context.get("customs_groups", []), "customs_category_bindings": context.get("customs_category_bindings", []),
               "status": "review_documents", "format_accepted": False, "shipment_facts_confirmed": False,
               "received_confirmed": False, "files": files, "zip_sha256": zip_digest, "zip_filename": "外贸单据-" + safe_number + ".zip"}
    try:
        return store.create_record("document_job", key, payload)
    except ConflictError:
        winner = store.find("document_job", key)
        if winner is None:
            raise
        return winner


def public_job(record):
    record = deepcopy(record)
    for item in record["payload"]["files"]:
        item["download_url"] = "/document-jobs/" + record["id"] + "/files/" + item["id"] + "/download"
    record["payload"]["zip_download_url"] = "/document-jobs/" + record["id"] + "/download"
    return record
