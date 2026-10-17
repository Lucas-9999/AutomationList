"""Read-only historical candidates; never creates sales, dispatch or receipt facts."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re

from .calculations import recalculate_supported_formulas
from .ingestion import parse_erp_xlsx
from .profiles import candidate_profile, read_values
from .receipts import _expand_sparse_totals, inspect_receipt_template
from .templates import inspect_template

# File selection/grouping is explicit. No business quantity, price or weight is
# encoded here. The September contract predates its CI, and is grouped by the
# inspected document set, not by pretending its issue date was September 24.
DOCUMENTS = (
    ("9.16 Contract2.xlsx", "contract", "9.24"), ("9.24 CI.xlsx", "ci", "9.24"),
    ("9.24 PI.xlsx", "pi", "9.24"), ("9.24 PL.xlsx", "pl", "9.24"),
    ("10.8 Contract.xlsx", "contract", "10.8"), ("10.8 CI.xlsx", "ci", "10.8"),
    ("10.8 PI.xlsx", "pi", "10.8"), ("10.8 PL.xlsx", "pl", "10.8"),
)
ERP_FILES = (("第一批.xlsx", "9.24"), ("第二批.xlsx", "10.8"))
NUMERIC = {"number", "quantity", "unit_price", "amount", "net_weight", "gross_weight", "small_box_count"}


def decimal_text(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("历史数值必须保留十进制原文")
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise ValueError("历史数值不是有效十进制") from None
    if not number.is_finite() or len(number.as_tuple().digits) > 100 or abs(number.as_tuple().exponent) > 100:
        raise ValueError("历史数值非有限或超出精度范围")
    return number


def specification_key(raw: str) -> str:
    """Only explicit metric diameter/length and an optional explicit pitch suffix.

    This is punctuation/decimal normalization, never approximate translation.
    Pitch remains in the original and is used to detect ambiguous ERP identities.
    """
    match = re.fullmatch(r"\s*M\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*(?:[（(]\s*P\s*\d+(?:\.\d+)?\s*[)）])?\s*", raw)
    if not match:
        raise ValueError("规格不属于已支持的严格公制直径×长度格式")
    def canonical(value):
        text = format(decimal_text(value), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return "M" + canonical(match[1]) + "x" + canonical(match[2])


def _sum(values) -> str:
    with localcontext() as context:
        context.prec = 500
        return str(sum((decimal_text(value) for value in values), Decimal(0)))


def _value(fact: dict | None) -> str:
    return fact["value"] if fact else ""


def _historical_date(raw: str) -> str | None:
    text = re.sub(r"^(?:Date|Delivery)\s*:\s*", "", raw.replace("\xa0", " ").strip(), flags=re.I).strip()
    for pattern in ("%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return None


def extract_document(path: Path, kind: str, batch: str) -> dict:
    content = Path(path).read_bytes()
    digest = sha256(content).hexdigest()
    profile = inspect_receipt_template(content) if kind == "receipt" else candidate_profile(content, kind)
    if "sheet" not in profile or not profile["item_slots"]:
        raise ValueError("历史文件没有明确的字段映射：" + Path(path).name)
    inspection, sheet = profile["inspection"], profile["sheet"]
    before = read_values(content, inspection)[sheet]
    normalized, normalizations = _expand_sparse_totals(content, profile) if kind == "receipt" else (content, [])
    calculated, calculation = recalculate_supported_formulas(normalized)
    after_inspection = inspect_template(calculated)
    after = read_values(calculated, after_inspection)[sheet]
    formulas = {item["cell"]: item for item in next(s for s in inspection["sheets"] if s["name"] == sheet)["formulas"]}

    def evidence(cell):
        formula = formulas.get(cell)
        return {"value": after.get(cell, ""), "raw": before.get(cell, ""), "present": cell in before,
                "sha256": digest, "sheet": sheet, "cell": cell,
                "formula": formula["formula"] if formula else None,
                "formula_attributes": formula["attributes"] if formula else None,
                "source_cached_value": formula["cached_value"] if formula else None,
                "value_basis": "recalculated_in_memory" if formula else "source_literal"}

    headers = {field["field"]: evidence(field["cell"]) for field in profile["fields"]}
    items = []
    for slot in profile["item_slots"]:
        facts = {key: evidence(cell) for key, cell in slot["cells"].items()}
        for key in NUMERIC & facts.keys():
            decimal_text(facts[key]["value"])
        items.append({"source_line_id": digest + ":" + sheet + ":" + str(slot["row"]),
                      "source_row": slot["row"], "specification_key": specification_key(_value(facts.get("specification"))), "facts": facts})
    totals = {key: _sum(_value(item["facts"][key]) for item in items)
              for key in ("quantity", "amount", "net_weight", "gross_weight", "small_box_count")
              if all(key in item["facts"] for item in items)}
    currency_evidence = [evidence(cell) for cell, value in before.items() if re.search(r"\bUSD\b", value)]
    # Currency is the inspected template's explicit mapping, not inferred from
    # an ERP unit price or an exchange rate. All source USD mentions are retained.
    currency = profile.get("fixed_context", {}).get("currency")
    return {"filename": Path(path).name, "path": str(Path(path).absolute()), "sha256": digest,
            "kind": kind, "batch_group": batch, "headers": headers, "items": items, "totals": totals,
            "currency": currency, "currency_basis": "inspected_template_mapping", "currency_evidence": currency_evidence,
            "formula_recalculation": calculation, "formula_normalizations": normalizations,
            "source_formula_cache_differences": [{"sheet": sheet, "cell": cell, "source_cached_value": before.get(cell, ""), "recalculated_value": after.get(cell, "")} for cell in formulas if before.get(cell, "") != after.get(cell, "")],
            "source_unchanged": Path(path).read_bytes() == content,
            "receipt_status": "historical_document_only_not_a_new_receipt" if kind == "receipt" else None}


def extract_erp(path: Path, batch: str) -> dict:
    content = Path(path).read_bytes()
    parsed = parse_erp_xlsx(content, Path(path).name)
    for row in parsed["items"]:
        row["specification_key"] = specification_key(row["specification"])
        row["source_line_id"] = parsed["sha256"] + ":" + parsed["source"]["sheet"] + ":" + str(row["source_row"])
        for fact in list(row["source_cells"].values()) + list(row["extra_fields"].values()):
            fact["sha256"] = parsed["sha256"]
    return {**parsed, "path": str(Path(path).absolute()), "filename": Path(path).name, "kind": "erp", "batch_group": batch,
            "total_quantity": _sum(item["quantity"] for item in parsed["items"]),
            "source_unchanged": Path(path).read_bytes() == content}


def reconcile_history(documents: list[dict], erps: list[dict], rules: dict) -> dict:
    """Compare extracted evidence and return review candidates, with no Store use."""
    checks, observations, boundaries = [], [], []
    groups = defaultdict(dict)
    for doc in documents:
        if doc["kind"] in groups[doc["batch_group"]]:
            raise ValueError("历史组中同类型文件不唯一")
        groups[doc["batch_group"]][doc["kind"]] = doc

    def compare(name, left, right, numeric=False):
        equal = decimal_text(left) == decimal_text(right) if numeric else left.strip() == right.strip()
        checks.append({"check": name, "passed": equal, "left": left, "right": right})
        return equal

    global_erp = defaultdict(list)
    for erp in erps:
        for row in erp["items"]:
            global_erp[row["specification_key"]].append((erp, row))
    all_ci = defaultdict(list)
    candidate_orders = []
    line_candidates = {}
    for batch, docs in groups.items():
        if not {"contract", "ci", "pi", "pl", "receipt"} <= docs.keys():
            raise ValueError("历史组缺少合同、CI、PI、PL或签收单")
        ci = docs["ci"]
        indexes = {}
        for kind, doc in docs.items():
            index = {}
            for row in doc["items"]:
                key = row["specification_key"]
                if key in index:
                    raise ValueError("单据中存在同规格多行，禁止仅按规格合并")
                index[key] = row
            indexes[kind] = index
        base_keys = set(indexes["ci"])
        for kind in ("contract", "pi", "pl", "receipt"):
            compare(batch + ":complete_specs:ci_vs_" + kind, ",".join(sorted(base_keys)), ",".join(sorted(indexes[kind])))
            for key in sorted(base_keys & indexes[kind].keys()):
                left, right = indexes["ci"][key]["facts"], indexes[kind][key]["facts"]
                for field in (("quantity", "unit_price", "amount") if kind in {"contract", "pi"} else ("quantity",)):
                    compare(batch + ":" + key + ":ci_vs_" + kind + ":" + field, _value(left.get(field)), _value(right.get(field)), True)
                compare(batch + ":" + key + ":ci_vs_" + kind + ":unit", _value(left.get("unit")), _value(right.get("unit")))
        for key in sorted(indexes["pl"].keys() & indexes["receipt"].keys()):
            for field in ("net_weight", "gross_weight", "small_box_count", "carton_numbers"):
                left = _value(indexes["pl"][key]["facts"].get(field))
                right = _value(indexes["receipt"][key]["facts"].get(field))
                compare(batch + ":" + key + ":pl_vs_receipt:" + field, left, right, field != "carton_numbers")
        for date_field in ("issue_date", "delivery_date"):
            date_sources = {kind: doc["headers"].get(date_field) for kind, doc in docs.items()}
            dates = {kind: _value(source) for kind, source in date_sources.items()}
            normalized_dates = {kind: _historical_date(raw) for kind, raw in dates.items()}
            actual_dates = {value for value in normalized_dates.values() if value is not None}
            if len(actual_dates) > 1 or (date_field == "delivery_date" and any(value is None for value in normalized_dates.values())):
                observations.append({"kind": "historical_" + date_field + "_text_differs", "batch_group": batch,
                                     "values": dates, "source_facts": date_sources, "normalized_dates": normalized_dates,
                                     "resolution": "保留原文；Sep/September等格式差异不算日期不同，不完整日期不推断，不将历史组名视为实际发货日期"})
        for kind, doc in docs.items():
            terms = doc["headers"].get("payment_terms")
            observations.append({"kind": "payment_terms", "batch_group": batch, "document_type": kind,
                                 "source": terms, "replay_down_payment_percent": rules["default_down_payment_percent"],
                                 "authority": rules.get("payment_terms_source", "explicit caller policy"), "original_document_modified": False})
        candidates = []
        for key, row in indexes["ci"].items():
            all_ci[key].append((ci, row))
            facts = row["facts"]
            with localcontext() as context:
                context.prec = 500
                amount = decimal_text(_value(facts["quantity"])) * decimal_text(_value(facts["unit_price"]))
            compare(batch + ":" + key + ":quantity_times_price", str(amount), _value(facts["amount"]), True)
            sources = global_erp.get(key, [])
            # Do not resolve same-size items with different pitch/grade/finish.
            identities = {(x["product_name"], x["specification"].replace(" ", "").replace("\n", ""), x["surface_color"], x["grade"], x["unit"]) for _, x in sources}
            unambiguous = len(identities) == 1
            source_finish = sources[0][1]["surface_color"] if unambiguous else None
            doc_chinese = set(re.findall(r"[\u3400-\u9fff]+", _value(facts.get("surface_color"))))
            finish_consistent = bool(source_finish and source_finish in doc_chinese)
            checks.append({"check": batch + ":" + key + ":erp_unique_identity_and_exact_chinese_finish", "passed": unambiguous and finish_consistent,
                           "erp_source_line_ids": [x["source_line_id"] for _, x in sources], "approximate_english_translation_used": False})
            source_unit = sources[0][1]["unit"] if unambiguous else None
            same_piece_unit = source_unit in {"支", "PCS"} and _value(facts.get("unit")) in {"支", "PCS"}
            checks.append({"check": batch + ":" + key + ":explicit_piece_unit_alias", "passed": same_piece_unit,
                           "erp_unit_raw": source_unit, "document_unit_raw": _value(facts.get("unit")), "alias_scope": "only exact 支/PCS; no inferred conversion"})
            candidate = {"candidate_line_id": row["source_line_id"], "specification_key": key,
                         "quantity": _value(facts["quantity"]), "unit_price": _value(facts["unit_price"]), "amount": _value(facts["amount"]),
                         "unit_raw": _value(facts["unit"]), "source_facts": facts,
                         "packing_facts": indexes["pl"].get(key, {}).get("facts", {}),
                         "erp_source_line_ids": [x["source_line_id"] for _, x in sources],
                         "erp_identity": {field: sources[0][1][field] for field in ("product_name", "specification", "surface_color", "grade", "unit")} if unambiguous else None,
                         "identity_status": "unique_with_exact_chinese_finish" if unambiguous and finish_consistent else "needs_explicit_mapping"}
            candidates.append(candidate)
            line_candidates[key] = candidate if key not in line_candidates else None
        candidate_orders.append({"candidate_only": True, "batch_group": batch, "reference_document_sha256": ci["sha256"],
                                 "historical_document_no": ci["headers"].get("document_no"), "currency": ci["currency"],
                                 "currency_basis": ci.get("currency_basis"), "headers": ci["headers"], "items": candidates,
                                 "replay_down_payment_percent": rules["default_down_payment_percent"], "business_record_created": False})
    for key in sorted(set(global_erp) | set(all_ci)):
        erp_total = _sum(row["quantity"] for _, row in global_erp.get(key, []))
        doc_total = _sum(_value(row["facts"]["quantity"]) for _, row in all_ci.get(key, []))
        compare("all_period:" + key + ":erp_vs_ci_quantity", erp_total, doc_total, True)
    confirmed_totals = []
    for confirmed in rules.get("historical_order_facts", []):
        key = specification_key(confirmed["product_specification_raw"])
        actual = _sum(row["quantity"] for _, row in global_erp.get(key, []))
        compare("confirmed_order_total:" + key, actual, confirmed["order_quantity"], True)
        confirmed_totals.append({**confirmed, "specification_key": key, "erp_observed_total": actual})
    candidate_batches = []
    for erp in erps:
        lines = []
        same_docs = groups.get(erp["batch_group"], {})
        same_ci = {row["specification_key"]: row for row in same_docs.get("ci", {}).get("items", [])}
        for row in erp["items"]:
            key = row["specification_key"]
            doc_row = same_ci.get(key)
            doc_quantity = _value(doc_row["facts"].get("quantity")) if doc_row else "0"
            if decimal_text(doc_quantity) != decimal_text(row["quantity"]):
                boundaries.append({"kind": "historical_document_coverage_boundary", "batch_group": erp["batch_group"], "specification_key": key,
                                   "erp_source_line_id": row["source_line_id"], "erp_quantity": row["quantity"], "same_group_ci_quantity": doc_quantity,
                                   "same_group_ci_source_line_id": doc_row["source_line_id"] if doc_row else None,
                                   "interpretation": "历史单据覆盖与实际ERP分批边界不同；整体已确认总量另行核对，不作为新单据省货或前移依据"})
            candidate = line_candidates.get(key)
            if candidate and candidate["identity_status"] != "unique_with_exact_chinese_finish":
                candidate = None
            lines.append({"erp_source_line_id": row["source_line_id"], "quantity": row["quantity"], "erp_identity_raw": {k: row[k] for k in ("product_name", "specification", "surface_color", "grade", "unit")},
                          "candidate_order_line_id": candidate["candidate_line_id"] if candidate else None,
                          "sales_unit_price_source": candidate["source_facts"]["unit_price"] if candidate else None,
                          "erp_unit_price_preserved_separately": row.get("unit_price"), "source_cells": row["source_cells"], "extra_fields": row["extra_fields"],
                          "packing_allocation_status": "historical_document_values_only; not prorated to actual batch"})
            weight = row.get("extra_fields", {}).get("重量")
            packing_weight = candidate.get("packing_facts", {}).get("net_weight") if candidate else None
            if weight and packing_weight and decimal_text(weight["raw"]) != decimal_text(packing_weight["value"]):
                observations.append({"kind": "erp_weight_and_historical_pl_net_weight_differ", "batch_group": erp["batch_group"], "specification_key": key,
                                     "erp_weight_source": weight, "historical_pl_net_weight_source": packing_weight,
                                     "resolution": "历史单据净重按授权保留PL依据；不覆盖ERP重量，不按比例猜测实际批次净毛重"})
        candidate_batches.append({"candidate_only": True, "batch_group": erp["batch_group"], "source_sha256": erp["sha256"], "source_document_type": erp["source"]["document_type"],
                                  "quantity": erp["total_quantity"], "items": lines, "business_record_created": False, "actual_dispatch_date_inferred": False})
    group_summary = []
    for batch, docs in groups.items():
        pl = docs["pl"]
        carton_facts = [row["facts"].get("carton_numbers") for row in pl["items"]]
        carton_marks = sorted({int(mark) for fact in carton_facts for mark in re.findall(r"#\s*(\d+)", _value(fact))})
        group_summary.append({"batch_group": batch, "historical_group_is_not_actual_dispatch_date": True,
                              "document_line_counts": {kind: len(doc["items"]) for kind, doc in docs.items()},
                              "document_quantity_totals": {kind: doc["totals"].get("quantity") for kind, doc in docs.items()},
                              "erp_quantity_total": _sum(erp["total_quantity"] for erp in erps if erp["batch_group"] == batch),
                              "ci_amount_total": docs["ci"]["totals"].get("amount"), "currency": docs["ci"]["currency"],
                              "pl_net_weight_total": pl["totals"].get("net_weight"), "pl_gross_weight_total": pl["totals"].get("gross_weight"),
                              "pl_small_box_count_total": pl["totals"].get("small_box_count"),
                              "distinct_carton_marks": ["#" + str(mark) for mark in carton_marks],
                              "distinct_carton_mark_count": len(carton_marks), "actual_carton_count_inferred": False,
                              "carton_mark_source_facts": carton_facts,
                              "ci_source_sha256": docs["ci"]["sha256"], "pl_source_sha256": pl["sha256"],
                              "replay_down_payment_percent": rules["default_down_payment_percent"]})
    return {"candidate_orders": candidate_orders, "candidate_batches": candidate_batches, "checks": checks,
            "summary": {"scope": "historical_document_consistency_and_source_coverage_only", "groups": group_summary,
                        "erp_quantity_total": _sum(erp["total_quantity"] for erp in erps),
                        "ci_quantity_total": _sum(doc["totals"]["quantity"] for doc in documents if doc["kind"] == "ci"),
                        "ci_amount_total": _sum(doc["totals"]["amount"] for doc in documents if doc["kind"] == "ci"),
                        "candidate_order_count": len(candidate_orders), "candidate_batch_count": len(candidate_batches),
                        "checks_passed": sum(x["passed"] for x in checks), "checks_failed": sum(not x["passed"] for x in checks),
                        "all_source_lines_preserved": True, "business_records_created": 0, "formal_acceptance_completed": False},
            "coverage_boundaries": boundaries, "confirmed_order_totals": confirmed_totals, "observations": observations,
            "actual_results": {"checks_passed": sum(x["passed"] for x in checks), "checks_failed": sum(not x["passed"] for x in checks),
                               "orders_created": 0, "shipments_created": 0, "receipts_created": 0, "allocations_created": 0, "candidate_only": True,
                               "can_reuse_historical_coverage_for_new_documents": False}}


def build_history_replay(source_root: Path, receipt_root: Path, rules: dict) -> dict:
    source_root, receipt_root = Path(source_root), Path(receipt_root)
    documents = [extract_document(source_root / "excel 单据" / filename, kind, batch) for filename, kind, batch in DOCUMENTS]
    documents += [extract_document(receipt_root / (batch + "发货签收单.xlsx"), "receipt", batch) for batch in ("9.24", "10.8")]
    erps = [extract_erp(source_root / "ERP导出文件" / filename, batch) for filename, batch in ERP_FILES]
    result = reconcile_history(documents, erps, rules)
    return {"format": "tradeflow-history-replay-v1", "status": "historical_candidates_for_review", "source_authority": "仅对调用方提供的本地历史资料作只读回放；不是新成交/发货/签收事件",
            "documents": documents, "erp_sources": erps, **result,
            "source_unchanged": all(item["source_unchanged"] for item in documents + erps), "source_files_read": len(documents) + len(erps)}
