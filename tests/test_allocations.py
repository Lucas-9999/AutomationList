"""Synthetic split-shipment and concurrent allocation evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tradeflow.allocations import allocate_batch
from tradeflow.api import create_app
from tradeflow.store import Store


def order():
    return {"order_no": "SYNTHETIC-SPLIT", "buyer": "合成买方", "seller": "合成卖方", "currency": "USD", "items": [{"line_id": "A", "product_name": "合成螺钉", "specification": "M1.6x4", "unit": "PCS", "surface_color": "黑锌", "grade": "12.9", "quantity": "300", "unit_price": "0.0123456789"}]}


def shipment(quantity):
    row = deepcopy(order()["items"][0])
    row.pop("line_id")
    row["quantity"] = quantity
    row["source_cells"] = {"quantity": "Sheet1!F7"}
    return {"items": [row], "sha256": "synthetic-hash", "source": {"document_type": "sales_order"}}


LINES = [{"order_line_id": "A", "shipment_item_index": 1}]


def test_two_batches_exact_total_and_source_provenance():
    first = allocate_batch(order(), shipment("290"), LINES, [])
    second = allocate_batch(order(), shipment("10"), LINES, [first])
    assert first["fully_allocated"] is False
    assert second["fully_allocated"] is True
    assert second["balance_after_allocation"][0]["remaining"] == "0"
    assert first["batch_order"]["total"] == "3.5802468810"
    assert second["batch_order"]["items"][0]["allocation_sources"][0]["source_cells"]["quantity"] == "Sheet1!F7"
    with pytest.raises(ValueError, match="超过"):
        allocate_batch(order(), shipment("1"), LINES, [first, second])


def test_partial_batch_never_inherits_full_order_weights_or_cartons():
    business = order()
    business.update(net_weight="100", gross_weight="110", package_count="20")
    business["items"][0].update(net_weight="100", gross_weight="110", small_box_count="20", carton_numbers="1-20", legal_quantity="100")
    original = deepcopy(business)
    result = allocate_batch(business, shipment("10"), LINES, [])
    assert "net_weight" not in result["batch_order"]
    assert "gross_weight" not in result["batch_order"]["items"][0]
    assert "carton_numbers" not in result["batch_order"]["items"][0]
    assert "gross_weight" not in result["shipment_view"]["items"][0]
    assert "items.A.net_weight" in result["unbound_order_packing_fields"]
    assert business == original
    full = allocate_batch(business, shipment("300"), LINES, [])
    assert full["batch_order"]["items"][0]["gross_weight"] == "110"
    assert not full["unbound_order_packing_fields"]


@pytest.mark.parametrize("changes", [{"quantity": 1.1}, {"quantity": "NaN"}, {"quantity": "0"}, {"surface_color": "镍"}, {"grade": "8.8"}, {"unit": "KG"}])
def test_inexact_or_mismatched_rows_block(changes):
    source = shipment("10")
    source["items"][0].update(changes)
    with pytest.raises(ValueError):
        allocate_batch(order(), source, LINES, [])


def test_no_source_row_can_be_omitted_or_reused():
    source = shipment("10")
    source["items"].append(deepcopy(source["items"][0]))
    with pytest.raises(ValueError, match="覆盖"):
        allocate_batch(order(), source, LINES, [])
    with pytest.raises(ValueError, match="重复"):
        allocate_batch(order(), source, LINES * 2, [])
    result = allocate_batch(order(), source, [*LINES, {"order_line_id": "A", "shipment_item_index": 2}], [])
    assert result["batch_order"]["items"][0]["quantity"] == "20"


def seed(store, quantity="200"):
    business = store.create_record("order", "synthetic-order", order())
    first = store.create_record("shipment", "synthetic-batch1", {**shipment(quantity), "sha256": "synthetic-hash-batch1"})
    second = store.create_record("shipment", "synthetic-batch2", {**shipment(quantity), "sha256": "synthetic-hash-batch2"})
    return business, first, second


def test_concurrent_allocations_cannot_overbook(tmp_path):
    store = Store(tmp_path)
    business, first, second = seed(store)

    def assign(source):
        try:
            return store.allocate(business["id"], source["id"], LINES, "合成出货确认")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(assign, [first, second]))
    assert sum(x is not None for x in result) == 1
    assert len(store.list_records("allocation")) == 1
    assert len(store.history(business["id"])) == 2


def test_same_original_cannot_be_allocated_twice_via_changed_mapping(tmp_path):
    store = Store(tmp_path)
    business, source, _ = seed(store, "10")
    remapped = store.create_record("shipment", "synthetic-remapped-same-physical-file", source["payload"])
    first = store.allocate(business["id"], source["id"], LINES, "合成实际出货确认")
    assert store.allocate(business["id"], source["id"], LINES, "合成实际出货确认")["id"] == first["id"]
    with pytest.raises(ValueError, match="同一 ERP 原件"):
        store.allocate(business["id"], remapped["id"], LINES, "合成不同映射")
    assert len(store.list_records("allocation")) == 1


def test_api_allocation_retry_balance_and_readiness(tmp_path):
    app = create_app(tmp_path)
    business, first, second = seed(app.state.store, "150")
    with TestClient(app) as client:
        url = "/orders/" + business["id"] + "/allocations"
        request = {"shipment_id": first["id"], "lines": LINES, "confirmation_ref": "合成实际出货依据"}
        allocation = client.post(url, json=request)
        assert allocation.status_code == 200, allocation.text
        assert client.post(url, json=request).json()["id"] == allocation.json()["id"]
        request["shipment_id"] = second["id"]
        assert client.post(url, json=request).status_code == 200
        assert client.get(url).json()["fully_allocated"] is True
        ready = client.post("/documents/readiness", json={"order_id": business["id"], "allocation_id": allocation.json()["id"]})
        assert ready.status_code == 200, ready.text
        assert ready.json()["reconciliation"]["passed"] is True
        assert ready.json()["generation_available"] is False
        assert client.post(url, json={**request, "confirmation_ref": ""}).status_code == 422
