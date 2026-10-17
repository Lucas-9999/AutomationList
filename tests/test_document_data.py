"""Synthetic business-to-template binding, no physical workbook output."""
import pytest

from tradeflow.document_data import decimal_items, prepare_document
from tradeflow.domain import normalize_order


def facts():
    return normalize_order({"order_no": "SYNTHETIC-001", "buyer": {"name": "Synthetic Buyer", "address": "Synthetic Address"}, "seller": "Synthetic Seller", "currency": "USD", "payment_terms": "30% down payment", "items": [{"line_id": "A", "product_name": "合成螺钉", "description_en": "Synthetic Screw", "specification": "M2x4", "unit": "PCS", "surface_color": "Black Zinc", "grade": "12.9", "quantity": "3", "unit_price": "0.10"}]})


def profile():
    return {"document_type": "ci", "template_sha256": "synthetic-sha", "fields": [{"field": "buyer.name"}, {"field": "payment_terms"}, {"field": "issue_date"}], "item_slots": [{"cells": {k: k for k in ["number", "description", "specification", "quantity", "unit", "unit_price", "amount", "remark"]}}]}


def test_auto_maps_persisted_values_and_reports_only_missing():
    plan = prepare_document(profile(), facts(), {}, {})
    assert plan["missing"] == ["fields.issue_date", "items.A.remark"]
    assert plan["items"][0]["amount"] == "0.30"
    assert plan["items"][0]["description"] == "Synthetic Screw"
    complete = prepare_document(profile(), facts(), {"issue_date": "2026-10-15"}, {"A": {"remark": ""}})
    assert complete["ready_for_review_draft"] is True
    assert str(decimal_items(complete)[0]["amount"]) == "0.30"


def test_empty_optional_order_date_is_unknown_and_can_be_supplemented():
    business, template = facts(), profile()
    business.update(delivery_date="", consignee_text="")
    template["fields"].extend([{"field": "delivery_date"}, {"field": "consignee_text"}])
    plan = prepare_document(template, business, {"issue_date": "SYNTHETIC", "delivery_date": "SYNTHETIC DATE", "consignee_text": "SYNTHETIC CONSIGNEE"}, {"A": {"remark": ""}})
    assert plan["ready_for_review_draft"]
    assert plan["fields"]["delivery_date"] == "SYNTHETIC DATE"


@pytest.mark.parametrize("extras", [{"quantity": "999"}, {"unit_price": "1.23"}, {"amount": "99"}, {"unit": "KG"}])
def test_item_supplements_cannot_override_saved_order_facts(extras):
    with pytest.raises(ValueError, match="不能覆盖"):
        prepare_document(profile(), facts(), {}, {"A": extras})


def test_header_conflict_unknown_line_currency_and_capacity():
    with pytest.raises(ValueError, match="不能覆盖"):
        prepare_document(profile(), facts(), {"buyer.name": "Other Buyer"}, {})
    with pytest.raises(ValueError, match="不存在"):
        prepare_document(profile(), facts(), {}, {"missing": {}})
    other = facts()
    other["currency"] = "EUR"
    with pytest.raises(ValueError, match="USD"):
        prepare_document(profile(), other, {}, {})
    template = profile()
    template["item_slots"] = []
    assert "template_capacity" in prepare_document(template, facts(), {}, {})["missing"][-1]


def test_trade_terms_cannot_hide_a_conflicting_saved_incoterm():
    business = facts()
    business["incoterm"] = "FOB"
    template = profile()
    template["fields"].append({"field": "trade_terms"})
    with pytest.raises(ValueError, match="incoterm"):
        prepare_document(template, business, {"trade_terms": "EXW SYNTHETIC PLACE"}, {})
