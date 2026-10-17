"""Synthetic ERP fixtures only; no real customer data or currency API calls."""
from copy import deepcopy
from decimal import Decimal

import pytest

from tradeflow.fx import convert_erp_prices, project_erp_source


def synthetic_record(*, quantity="3", price="1.2345", amount="3.7035"):
    fields = dict(product_name="合成螺钉", specification="M2×5", quantity=quantity,
                  unit="支", surface_color="合成黑锌", grade="12.9", unit_price=price)
    facts = {key: {"sheet": "Synthetic", "cell": chr(65 + i) + "7", "raw": value, "formula": False}
             for i, (key, value) in enumerate(fields.items())}
    extras = {"材质": {"sheet": "Synthetic", "cell": "I7", "raw": "合成材料"},
              "重量": {"sheet": "Synthetic", "cell": "J7", "raw": "88"},
              "包规": {"sheet": "Synthetic", "cell": "K7", "raw": "合成包装说明"}}
    if amount is not None:
        extras["金额"] = {"sheet": "Synthetic", "cell": "H7", "raw": amount}
    return {"id": "erp_synthetic", "kind": "shipment", "payload": {
        "sha256": "a" * 64, "source": {"sheet": "Synthetic", "filename": "synthetic.xlsx",
        "preamble_cells": [{"sheet": "Synthetic", "cell": "B1", "raw": "合成卖方-销售订单"},
                           {"sheet": "Synthetic", "cell": "B2", "raw": "客户名称："},
                           {"sheet": "Synthetic", "cell": "D2", "raw": "合成买方"},
                           {"sheet": "Synthetic", "cell": "L2", "raw": "单据编号:"},
                           {"sheet": "Synthetic", "cell": "M2", "raw": "SYNTHETIC-001"},
                           {"sheet": "Synthetic", "cell": "L3", "raw": "单据日期:"},
                           {"sheet": "Synthetic", "cell": "M3", "raw": "2026.10.16"}]},
        "items": [{"source_row": 7, "source_cells": facts, "extra_fields": extras, **fields}]}}


def test_projection_reads_confirmed_cny_and_located_headers_without_mutation():
    record = synthetic_record()
    before = deepcopy(record)
    p = project_erp_source(record)
    assert record == before
    assert p["source_currency"] == "CNY"
    assert p["source_currency_basis"] == "user_confirmed"
    assert p["header"] == {"order_no": "SYNTHETIC-001", "order_date": "2026.10.16", "buyer_name": "合成买方", "seller_name": "合成卖方"}
    assert p["header_sources"]["buyer_name"][1]["cell"] == "D2"
    item = p["items"][0]
    assert item["line_id"] == "erp-7"
    assert item["unit_price_cny"] == "1.2345"
    assert item["amount_cny"] == "3.7035"
    assert item["material"] == "合成材料"
    assert item["standard"] is None
    assert item["source_cells"]["amount_cny"]["cell"] == "H7"
    assert "gross_weight" not in item and "net_weight" not in item and "packages" not in item
    assert p["totals"] == {"quantity": "3", "total_cny": "3.7035"}
    p["items"][0]["extra_fields"]["重量"]["raw"] = "changed"
    assert record == before


def test_unknown_or_ambiguous_headers_are_null():
    record = synthetic_record()
    record["payload"]["source"]["preamble_cells"] = []
    assert all(value is None for value in project_erp_source(record)["header"].values())
    record = synthetic_record()
    record["payload"]["source"]["preamble_cells"].append({"sheet": "Synthetic", "cell": "B4", "raw": "客户名称：另一合成买方"})
    assert project_erp_source(record)["header"]["buyer_name"] is None


def test_missing_amount_is_unknown_and_does_not_become_zero():
    p = project_erp_source(synthetic_record(amount=None))
    assert p["items"][0]["amount_cny"] is None
    assert p["totals"]["total_cny"] is None
    result = convert_erp_prices(p, "7")
    assert result["totals"]["total_usd"] == "0.53"
    assert result["totals"]["direct_total_usd"] is None
    assert result["totals"]["rounding_difference_usd"] is None


def test_half_up_unit_then_line_amount_and_total_difference_are_explicit():
    p = project_erp_source(synthetic_record(quantity="10000", price="0.1008", amount="1008"))
    before = deepcopy(p)
    result = convert_erp_prices(p, "7")
    assert p == before
    assert result["items"][0]["unit_price_usd"] == "0.0144"
    assert result["items"][0]["amount_usd"] == "144.00"
    assert result["totals"]["direct_total_usd"] == "144.00"
    assert result["rounding_policy"]["unit_price_decimals"] == 4
    result = convert_erp_prices(p, "7.2")
    assert result["items"][0]["unit_price_usd"] == "0.0140"
    assert result["totals"]["total_usd"] == "140.00"
    p = project_erp_source(synthetic_record(quantity="100000", price="0.1008", amount="10080"))
    result = convert_erp_prices(p, "7.1")
    assert result["totals"]["total_usd"] == "1420.00"
    assert result["totals"]["direct_total_usd"] == "1419.72"
    assert result["totals"]["rounding_difference_usd"] == "0.28"


def test_exact_half_unit_and_line_round_up():
    p = project_erp_source(synthetic_record(quantity="1", price="1.23445", amount="1.23445"))
    result = convert_erp_prices(p, "1")
    assert result["items"][0]["unit_price_usd"] == "1.2345"
    p = project_erp_source(synthetic_record(quantity="0.5", price="1.01", amount="0.505"))
    assert convert_erp_prices(p, "1")["items"][0]["amount_usd"] == "0.51"


def test_all_line_amounts_sum_after_rounding():
    record = synthetic_record(quantity="1", price="0.005", amount="0.005")
    row = deepcopy(record["payload"]["items"][0])
    row["source_row"] = 8
    for facts in (row["source_cells"], row["extra_fields"]):
        for fact in facts.values():
            fact["cell"] = fact["cell"][:-1] + "8"
    record["payload"]["items"].append(row)
    result = convert_erp_prices(project_erp_source(record), "1")
    assert [r["amount_usd"] for r in result["items"]] == ["0.01", "0.01"]
    assert result["totals"]["total_usd"] == "0.02"
    assert result["totals"]["direct_total_usd"] == "0.01"
    assert result["totals"]["rounding_difference_usd"] == "0.01"


def test_tiny_unit_zero_is_flagged_not_hidden():
    p = project_erp_source(synthetic_record(quantity="1000000", price="0.000001", amount="1"))
    result = convert_erp_prices(p, "7")
    assert result["items"][0]["unit_price_usd"] == "0.0000"
    assert result["totals"]["rounding_difference_usd"] == "-0.14"
    assert any("舍入为零" in warning for warning in result["warnings"])


def test_large_decimal_arithmetic_does_not_use_default_context_or_float():
    q = "123456789012345678901234567890"
    p = project_erp_source(synthetic_record(quantity=q, price="2", amount="246913578024691357802469135780"))
    result = convert_erp_prices(p, "2")
    assert result["items"][0]["unit_price_usd"] == "1.0000"
    assert result["items"][0]["amount_usd"] == q + ".00"
    assert result["totals"]["rounding_difference_usd"] == "0.00"


@pytest.mark.parametrize("rate", [None, 7, 7.1, True, Decimal("7"), "", "0", "-1", "NaN", "Infinity", "1e3", "7,1", "9" * 65, "1." + "1" * 19])
def test_invalid_rate_rejected(rate):
    with pytest.raises(ValueError):
        convert_erp_prices(project_erp_source(synthetic_record()), rate)


def test_original_formatting_and_independent_runs_are_stable():
    record = synthetic_record(quantity="3.000", price="1.234500", amount=" 3.703500 ")
    p = project_erp_source(record)
    assert p["items"][0]["quantity"] == "3.000"
    assert p["items"][0]["source_cells"]["amount_cny"]["raw"] == " 3.703500 "
    assert project_erp_source(deepcopy(record)) == p
    assert convert_erp_prices(p, "7.0000") == convert_erp_prices(p, "7.0000")
    assert convert_erp_prices(p, "7")["items"][0]["unit_price_usd"] != convert_erp_prices(p, "8")["items"][0]["unit_price_usd"]


def test_source_numeric_format_can_differ_without_overwriting_raw_text():
    record = synthetic_record()
    record["payload"]["items"][0]["source_cells"]["quantity"]["raw"] = " +003.000 "
    p = project_erp_source(record)
    assert p["items"][0]["quantity"] == "3"
    assert p["items"][0]["source_cells"]["quantity"]["raw"] == " +003.000 "


def test_source_amount_mismatch_rejected_but_source_two_place_rounding_allowed():
    with pytest.raises(ValueError, match="金额"):
        project_erp_source(synthetic_record(amount="100"))
    assert project_erp_source(synthetic_record(amount="3.70"))["items"][0]["amount_cny"] == "3.70"
    p = project_erp_source(synthetic_record())
    p["items"][0]["amount_cny"] = "100"
    with pytest.raises(ValueError, match="金额"):
        convert_erp_prices(p, "7")


@pytest.mark.parametrize("mutation", ["hash", "duplicate", "missing_price", "quantity_float", "wrong_raw", "wrong_row", "ambiguous_amount"])
def test_malformed_source_cannot_silently_convert(mutation):
    record = synthetic_record()
    row = record["payload"]["items"][0]
    if mutation == "hash":
        record["payload"]["sha256"] = "unknown"
    elif mutation == "duplicate":
        record["payload"]["items"].append(deepcopy(row))
    elif mutation == "missing_price":
        row["source_cells"].pop("unit_price")
    elif mutation == "quantity_float":
        row["quantity"] = 3.0
    elif mutation == "wrong_raw":
        row["source_cells"]["quantity"]["raw"] = "4"
    elif mutation == "wrong_row":
        row["source_cells"]["quantity"]["cell"] = "C8"
    else:
        row["extra_fields"]["销售金额"] = deepcopy(row["extra_fields"]["金额"])
    with pytest.raises(ValueError):
        project_erp_source(record)
