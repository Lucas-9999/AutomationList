"""All business examples in this file are synthetic; no external services."""
from copy import deepcopy
from decimal import Decimal

import pytest

from tradeflow.domain import DOCUMENT_TYPES, STATES, check_document_readiness, normalize_order, transition


@pytest.fixture
def order():
    return {
        "synthetic": True,
        "inquiry_id": "SYNTHETIC-INQUIRY",
        "order_no": "SYNTHETIC-001",
        "buyer": {"name": "合成买方", "address": "合成买方地址"},
        "seller": {"name": "合成卖方", "address": "合成卖方地址"},
        "currency": "usd", "incoterm": "FOB 合成港", "payment_terms": "合成付款条款",
        "destination_country": "合成目的国",
        "items": [{
            "line_id": "SYNTHETIC-LINE-1", "product_name": "合成型材", "specification": "20x20",
            "quantity": "3", "unit": "pcs", "surface_color": "合成银色", "grade": "合成A",
            "unit_price": "0.10", "hs_code": "7604210000", "origin_country": "合成原产国",
            "source": {"cell": "D2", "sha256": "synthetic-hash", "original_text": "3"},
        }],
    }


@pytest.fixture
def shipment(order):
    return {
        "synthetic": True, "shipment_no": "SYNTHETIC-SHIPMENT-1",
        "items": deepcopy(order["items"]),
        "consignor": deepcopy(order["seller"]), "consignee": deepcopy(order["buyer"]),
        "package_count": "1", "package_type": "合成木箱", "gross_weight": "12.5",
        "net_weight": "10.25", "weight_unit": "kg",
    }


def test_exact_amount_and_preserved_provenance_without_mutation(order):
    snapshot = deepcopy(order)
    normalized = normalize_order(order)
    assert normalized["total"] == "0.30"
    assert normalized["items"][0]["amount"] == "0.30"
    assert normalized["currency"] == "USD"
    assert normalized["inquiry_id"] == "SYNTHETIC-INQUIRY"
    assert normalized["items"][0]["source"] == order["items"][0]["source"]
    normalized["items"][0]["source"]["cell"] = "E3"
    assert order == snapshot


def test_decimal_sum_has_no_binary_rounding(order):
    order["items"][0]["quantity"] = "1"
    second = deepcopy(order["items"][0])
    second.update(line_id="SYNTHETIC-LINE-2", unit_price="0.20")
    order["items"].append(second)
    assert normalize_order(order)["total"] == "0.30"


@pytest.mark.parametrize("field,value", [
    ("quantity", "0"), ("quantity", "-1"), ("quantity", "NaN"),
    ("quantity", "Infinity"), ("quantity", None), ("quantity", ""),
    ("quantity", 0.1), ("quantity", True), ("quantity", "1e99999"),
    ("unit_price", "-0.01"), ("unit_price", "sNaN"), ("unit_price", "-Infinity"),
    ("unit_price", None), ("unit_price", "unknown"),
])
def test_invalid_numbers_never_become_zero(order, field, value):
    order["items"][0][field] = value
    with pytest.raises(ValueError):
        normalize_order(order)


def test_zero_price_is_explicit_and_permitted(order):
    order["items"][0]["unit_price"] = "0"
    assert Decimal(normalize_order(order)["total"]) == 0


@pytest.mark.parametrize("field", ["product_name", "specification", "unit", "surface_color", "grade", "line_id"])
def test_missing_item_identity_is_blocking(order, field):
    del order["items"][0][field]
    with pytest.raises(ValueError, match=field):
        normalize_order(order)


def test_duplicate_line_ids_block(order):
    order["items"].append(deepcopy(order["items"][0]))
    with pytest.raises(ValueError, match="重复"):
        normalize_order(order)


@pytest.mark.parametrize("location", ["amount", "total"])
def test_supplied_incorrect_amount_cannot_be_silently_rewritten(order, location):
    if location == "amount":
        order["items"][0][location] = "10"
    else:
        order[location] = "10"
    with pytest.raises(ValueError, match=location):
        normalize_order(order)


def test_draft_parties_can_be_strings_but_documents_need_addresses(order):
    order["buyer"] = "合成买方"
    assert normalize_order(order)["buyer"] == "合成买方"
    result = check_document_readiness(order, None, list(DOCUMENT_TYPES))
    assert not result["per_document"]["pi"]["ready"]
    assert any("order.buyer" in issue for issue in result["issues"])


EVIDENCE = {
    "technical_ready": {"analysis_id": "SYNTHETIC-ANALYSIS", "technical_approved": True},
    "order_confirmed": {"order_id": "SYNTHETIC-ORDER", "customer_confirmation_ref": "SYNTHETIC-CONFIRMATION", "commercial_approved": True},
    "shipment_verified": {"shipment_id": "SYNTHETIC-SHIPMENT", "reconciliation_passed": True},
    "documents_ready": {"document_ids": ["SYNTHETIC-DOCUMENT"], "validation_passed": True},
    "dispatched": {"dispatch_ref": "SYNTHETIC-DISPATCH"},
    "delivered": {"receipt_ref": "SYNTHETIC-SIGNED-RECEIPT", "signed_at": "2026-10-14T13:30:00+08:00"},
    "closed": {"closure_approved": True},
}


def test_legal_route_and_idempotent_retries():
    current = STATES[0]
    for target in STATES[1:]:
        current = transition(current, target, EVIDENCE[target])
        assert transition(current, current, {}) == current
    assert current == "closed"


@pytest.mark.parametrize("index", range(1, len(STATES)))
def test_missing_evidence_blocks_each_transition(index):
    with pytest.raises(ValueError):
        transition(STATES[index - 1], STATES[index], {})


@pytest.mark.parametrize("target,field", [
    ("technical_ready", "technical_approved"), ("order_confirmed", "commercial_approved"),
    ("shipment_verified", "reconciliation_passed"), ("documents_ready", "validation_passed"),
    ("closed", "closure_approved"),
])
def test_numeric_one_is_not_approval(target, field):
    evidence = deepcopy(EVIDENCE[target])
    evidence[field] = 1
    with pytest.raises(ValueError, match=field):
        transition(STATES[STATES.index(target) - 1], target, evidence)


@pytest.mark.parametrize("current,target", [("inquiry_received", "order_confirmed"), ("closed", "delivered"), ("unknown", "unknown")])
def test_illegal_routes(current, target):
    with pytest.raises(ValueError):
        transition(current, target, {})


@pytest.mark.parametrize("signed_at", ["", "2026-10-14", "not-a-date", "2026-99-14T12:00:00"])
def test_unsigned_or_invalid_receipt_does_not_prove_delivery(signed_at):
    with pytest.raises(ValueError):
        transition("dispatched", "delivered", {"receipt_ref": "SYNTHETIC-BLANK-FORM", "signed_at": signed_at})


def test_all_documents_ready_does_not_mutate_or_invent_signed_facts(order, shipment):
    snapshot = deepcopy(shipment)
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert result["ready"]
    assert result["reconciliation"]["passed"]
    assert all(part["ready"] for part in result["per_document"].values())
    assert shipment == snapshot
    assert "signed_at" not in shipment


def test_pi_and_contract_do_not_wait_for_shipment(order):
    result = check_document_readiness(order, None, ["pi", "contract"])
    assert not result["ready"]
    assert result["per_document"]["pi"]["ready"]
    assert result["per_document"]["contract"]["ready"]
    assert not result["reconciliation"]["passed"]


def test_missing_templates_remain_named_issues(order, shipment):
    result = check_document_readiness(order, shipment, [])
    assert not result["ready"]
    assert result["reconciliation"]["passed"]
    for kind in DOCUMENT_TYPES:
        assert any(f"template.{kind}" in issue for issue in result["issues"])


@pytest.mark.parametrize("field", ["product_name", "specification", "quantity", "unit", "surface_color", "grade"])
def test_each_erp_mismatch_blocks_reconciliation(order, shipment, field):
    shipment["items"][0][field] = "2" if field == "quantity" else "合成错误字段"
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert not result["reconciliation"]["passed"]
    assert any(field in issue for issue in result["reconciliation"]["issues"])
    assert result["per_document"]["pi"]["ready"]


def test_missing_row_and_extra_row_block(order, shipment):
    extra = deepcopy(shipment["items"][0])
    extra["line_id"] = "SYNTHETIC-EXTRA"
    shipment["items"].append(extra)
    assert not check_document_readiness(order, shipment, list(DOCUMENT_TYPES))["reconciliation"]["passed"]
    shipment["items"] = []
    assert not check_document_readiness(order, shipment, list(DOCUMENT_TYPES))["reconciliation"]["passed"]


def test_explicit_partial_shipment_is_currently_unsupported(order, shipment):
    shipment["partial_shipment"] = True
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert not result["reconciliation"]["passed"]
    assert any("partial_shipment" in issue for issue in result["issues"])


def test_product_combination_matches_if_erp_has_no_business_line_id(order, shipment):
    del shipment["items"][0]["line_id"]
    assert check_document_readiness(order, shipment, list(DOCUMENT_TYPES))["reconciliation"]["passed"]


def test_ambiguous_product_combination_cannot_be_guessed(order, shipment):
    second = deepcopy(order["items"][0])
    second["line_id"] = "SYNTHETIC-LINE-2"
    order["items"].append(second)
    shipment["items"].append(deepcopy(second))
    for row in shipment["items"]:
        del row["line_id"]
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert not result["reconciliation"]["passed"]
    assert any("重复" in issue for issue in result["reconciliation"]["issues"])


@pytest.mark.parametrize("field", ["package_count", "package_type", "gross_weight", "net_weight", "weight_unit", "consignor", "consignee", "shipment_no"])
def test_missing_packaging_and_parties_block_documents_but_not_reconciliation(order, shipment, field):
    del shipment[field]
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert result["reconciliation"]["passed"]
    assert not result["per_document"]["pl"]["ready"]
    assert any(field in issue for issue in result["issues"])


def test_bad_weights_and_fractional_packages_block(order, shipment):
    shipment.update(gross_weight="5", net_weight="10", package_count="1.5")
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert any("毛重" in issue for issue in result["issues"])
    assert any("整数" in issue for issue in result["issues"])


def test_customs_fields_are_never_inferred(order, shipment):
    del order["items"][0]["hs_code"]
    del order["items"][0]["origin_country"]
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert result["per_document"]["ci"]["ready"]
    assert not result["per_document"]["customs"]["ready"]
    assert "hs_code" not in order["items"][0]


def test_invalid_order_readiness_returns_explicit_issues(order, shipment):
    del order["items"][0]["quantity"]
    result = check_document_readiness(order, shipment, list(DOCUMENT_TYPES))
    assert not result["ready"]
    assert not result["reconciliation"]["passed"]
    assert any("quantity" in issue for issue in result["issues"])
