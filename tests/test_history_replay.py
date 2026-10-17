"""Synthetic history; no client prices/weights are hard-coded in tests."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

from tradeflow.history_replay import decimal_text, extract_document, reconcile_history, specification_key


def fact(value):
    return {"value": str(value), "raw": str(value), "sheet": "SYNTHETIC", "cell": "A1", "sha256": "a" * 64}


def corpus():
    docs = []
    for batch, quantity, price in [("SYNTHETIC-A", "8", "0.125"), ("SYNTHETIC-B", "2", "0.125")]:
        for kind in ("contract", "ci", "pi", "pl", "receipt"):
            facts = {"specification": fact("M2*5"), "quantity": fact(quantity), "unit": fact("PCS"), "unit_price": fact(price), "amount": fact(str(decimal_text(quantity) * decimal_text(price))), "surface_color": fact("SYNTHETIC ENGLISH\n合成黑色"), "net_weight": fact("2"), "gross_weight": fact("3"), "small_box_count": fact("4"), "carton_numbers": fact("#1,#2")}
            docs.append({"filename": "SYNTHETIC.xlsx", "kind": kind, "batch_group": batch, "sha256": kind + batch, "currency": "USD", "headers": {"document_no": fact("SYNTHETIC-NO"), "payment_terms": fact("50% down payment" if kind == "pl" else "30% down payment"), "delivery_date": fact("SYNTHETIC DATE")}, "items": [{"source_line_id": kind + batch, "specification_key": "M2x5", "facts": facts}], "totals": {"quantity": quantity, "amount": str(decimal_text(quantity) * decimal_text(price)), "net_weight": "2", "gross_weight": "3", "small_box_count": "4"}})
    erps = []
    # A known historical boundary is deliberate: ERP6+4 vs document8+2.
    for batch, quantity in [("SYNTHETIC-A", "6"), ("SYNTHETIC-B", "4")]:
        row = {"source_line_id": "ERP" + batch, "specification_key": "M2x5", "product_name": "合成产品", "specification": "M2×5(P0.4)", "surface_color": "合成黑色", "grade": "SYNTHETIC-GRADE", "unit": "支", "quantity": quantity, "unit_price": "99", "source_cells": {"quantity": fact(quantity)}, "extra_fields": {}}
        erps.append({"batch_group": batch, "sha256": "ERP" + batch, "items": [row], "source": {"document_type": "sales_order"}, "total_quantity": quantity})
    rules = {"default_down_payment_percent": "30", "payment_terms_source": "SYNTHETIC explicit policy", "historical_order_facts": [{"product_specification_raw": "M2×5", "order_quantity": "10", "source": "SYNTHETIC confirmed total"}]}
    return docs, erps, rules


def test_candidates_preserve_prices_erp_identity_and_historical_boundary():
    docs, erps, rules = corpus()
    before = deepcopy((docs, erps, rules))
    result = reconcile_history(docs, erps, rules)
    assert (docs, erps, rules) == before
    assert result["actual_results"]["orders_created"] == 0
    assert result["actual_results"]["shipments_created"] == 0
    assert result["actual_results"]["receipts_created"] == 0
    assert len(result["coverage_boundaries"]) == 2
    # Same-spec different CI candidate rows cannot be silently linked to one
    # order by diameter alone; this corpus intentionally needs explicit mapping.
    assert all(row["candidate_order_line_id"] is None for batch in result["candidate_batches"] for row in batch["items"])
    assert result["candidate_orders"][0]["items"][0]["unit_price"] == "0.125"
    assert result["candidate_orders"][0]["items"][0]["erp_identity"]["grade"] == "SYNTHETIC-GRADE"
    assert result["candidate_orders"][0]["replay_down_payment_percent"] == "30"
    old_pl = [x for x in result["observations"] if x["kind"] == "payment_terms" and x["document_type"] == "pl"]
    assert old_pl[0]["source"]["raw"] == "50% down payment"
    assert all(not x["original_document_modified"] for x in old_pl)
    assert result["summary"]["erp_quantity_total"] == "10"


@pytest.mark.parametrize("mutation", ["price", "quantity", "packing", "missing_line"])
def test_commercial_or_packing_mismatch_is_reported(mutation):
    docs, erps, rules = corpus()
    pi = next(x for x in docs if x["kind"] == "pi")
    if mutation == "price": pi["items"][0]["facts"]["unit_price"] = fact("0.5")
    if mutation == "quantity": pi["items"][0]["facts"]["quantity"] = fact("1")
    if mutation == "packing": next(x for x in docs if x["kind"] == "receipt")["items"][0]["facts"]["gross_weight"] = fact("4")
    if mutation == "missing_line": pi["items"] = []
    result = reconcile_history(docs, erps, rules)
    assert result["actual_results"]["checks_failed"] >= 1


def test_english_near_match_does_not_replace_chinese_finish_or_choose_grade():
    docs, erps, rules = corpus()
    for doc in docs:
        doc["items"][0]["facts"]["surface_color"] = fact("SYNTHETIC ENGLISH ONLY")
    result = reconcile_history(docs, erps, rules)
    assert all(x["items"][0]["identity_status"] == "needs_explicit_mapping" for x in result["candidate_orders"])
    other = deepcopy(erps[0]["items"][0])
    other["grade"] = "SYNTHETIC OTHER GRADE"
    erps[0]["items"].append(other)
    result = reconcile_history(docs, erps, rules)
    assert result["candidate_orders"][0]["items"][0]["erp_identity"] is None


def test_unverified_identity_never_links_batch_to_commercial_candidate():
    docs, erps, rules = corpus()
    # A single commercial occurrence makes its candidate unique, while the ERP
    # finish remains unverified. Uniqueness cannot turn it into a valid mapping.
    docs = [doc for doc in docs if doc["batch_group"] == "SYNTHETIC-A"]
    for doc in docs:
        doc["items"][0]["facts"]["surface_color"] = fact("SYNTHETIC ENGLISH ONLY")
    result = reconcile_history(docs, erps, rules)
    assert all(row["candidate_order_line_id"] is None and row["sales_unit_price_source"] is None
               for batch in result["candidate_batches"] for row in batch["items"])


def test_same_document_duplicate_spec_rows_fail_closed():
    docs, erps, rules = corpus()
    docs[0]["items"].append(deepcopy(docs[0]["items"][0]))
    with pytest.raises(ValueError, match="同规格多行"):
        reconcile_history(docs, erps, rules)


@pytest.mark.parametrize("raw", ["M2x5", " M2.0 × 5.00 (P0.4) ", "M2*5", "M2X5"])
def test_strict_metric_syntax_normalizes_only_punctuation(raw):
    assert specification_key(raw) == "M2x5"


@pytest.mark.parametrize("raw", ['0-80x0.187"L', "M2x5 approximate", "M2x5-unknown", "M2x5(P?)"])
def test_unrecognized_or_imperial_spec_never_approximately_matches(raw):
    with pytest.raises(ValueError): specification_key(raw)


def test_native_extraction_preserves_raw_formula_hash_cell_and_original(tmp_path):
    target = BytesIO()
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with ZipFile(target, "w") as out:
        out.writestr("xl/workbook.xml", f'<workbook xmlns="{namespace}" xmlns:r="{rel}"><sheets><sheet name="SYNTHETIC" sheetId="1" r:id="r1"/></sheets></workbook>')
        out.writestr("xl/_rels/workbook.xml.rels", f'<Relationships><Relationship Id="r1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        out.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{namespace}"><sheetData><row r="6"><c r="A6" t="inlineStr"><is><t>Commercial Invoice</t></is></c></row><row r="32"><c r="A32"><v>1</v></c><c r="B32" t="inlineStr"><is><t>SYNTHETIC GOODS</t></is></c><c r="E32" t="inlineStr"><is><t>M2*5</t></is></c><c r="H32"><v>3</v></c><c r="I32" t="inlineStr"><is><t>PCS</t></is></c><c r="J32"><v>2.5</v></c><c r="K32"><f>H32*J32</f><v>999</v></c></row><row r="33"><c r="A33" t="inlineStr"><is><t>S.TOTAL</t></is></c></row></sheetData></worksheet>')
    raw = target.getvalue()
    path = tmp_path / "SYNTHETIC-CI.xlsx"
    path.write_bytes(raw)
    extracted = extract_document(path, "ci", "SYNTHETIC-A")
    amount = extracted["items"][0]["facts"]["amount"]
    assert amount["raw"] == "999" and amount["value"] == "7.5"
    assert amount["sha256"] == sha256(raw).hexdigest()
    assert amount["sheet"] == "SYNTHETIC" and amount["cell"] == "K32"
    assert amount["formula"] == "H32*J32" and amount["value_basis"] == "recalculated_in_memory"
    assert path.read_bytes() == raw


def test_cli_existing_output_is_never_overwritten(tmp_path):
    existing = tmp_path / "replay.json"
    existing.write_text("SYNTHETIC EXISTING EVIDENCE", encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "tools" / "replay_history.py"
    response = subprocess.run([sys.executable, str(script), "--source-root", str(tmp_path), "--receipt-root", str(tmp_path), "--output", str(existing)], capture_output=True)
    assert response.returncode == 1
    assert existing.read_text(encoding="utf-8") == "SYNTHETIC EXISTING EVIDENCE"
    assert "error" in json.loads(response.stderr)


def test_actual_issue_date_difference_is_distinguished_from_format_only():
    docs, erps, rules = corpus()
    for doc in docs:
        doc["headers"]["issue_date"] = fact("Date: 24 Sep 2026" if doc["kind"] == "pl" else "Date: 24 September 2026")
    result = reconcile_history(docs, erps, rules)
    assert not any(x["kind"] == "historical_issue_date_text_differs" for x in result["observations"])
    docs[0]["headers"]["issue_date"] = fact("Date: 16 September 2026")
    result = reconcile_history(docs, erps, rules)
    issue = next(x for x in result["observations"] if x["kind"] == "historical_issue_date_text_differs")
    assert issue["normalized_dates"]["contract"] == "2026-09-16"
    assert issue["normalized_dates"]["pl"] == "2026-09-24"
    assert issue["source_facts"]["contract"]["raw"] == "Date: 16 September 2026"
