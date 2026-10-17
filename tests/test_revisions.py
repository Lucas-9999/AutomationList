"""Order revision tests use explicit synthetic business records only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from tradeflow.domain import normalize_order
from tradeflow.revisions import create_order_revision, order_revision_status
from tradeflow.store import ConflictError, Store


def synthetic_order(quantity="2", *, inquiry=None):
    payload = {"order_no": "SYNTHETIC-ORDER", "buyer": "SYNTHETIC BUYER",
               "seller": "SYNTHETIC SELLER", "currency": "USD",
               "items": [{"line_id": "SYNTHETIC-LINE", "product_name": "合成测试螺钉",
                          "specification": "SYNTHETIC-M2", "unit": "PCS",
                          "surface_color": "合成表色", "grade": "合成等级",
                          "quantity": quantity, "unit_price": "3"}],
               "original_text": "SYNTHETIC TEST DATA", "provenance": {"source_cell": "Synthetic!A1"}}
    if inquiry is not None:
        payload["inquiry_id"] = inquiry
    return payload


def setup_store(tmp_path, *, inquiry=None):
    store = Store(tmp_path)
    payload = normalize_order(synthetic_order(inquiry=inquiry))
    root = store.create_record("order", payload["order_no"], payload)
    return store, root


def test_immutable_successor_lineage_and_two_way_atomic_audit(tmp_path):
    store, root = setup_store(tmp_path)
    before = deepcopy(root)
    revised = create_order_revision(store, root["id"], synthetic_order("5"), "  合成测试数量修订  ")
    assert store.get(root["id"]) == before
    assert revised["kind"] == "order"
    assert revised["source_key"].startswith("order-revision-v1:" + root["id"])
    assert revised["payload"]["items"][0]["quantity"] == "5"
    assert revised["payload"]["total"] == "15"
    assert revised["payload"]["provenance"] == root["payload"]["provenance"]
    assert revised["payload"]["revision_reason"] == "合成测试数量修订"
    assert order_revision_status(store, root["id"]) == {"root_order_id": root["id"], "latest_order_id": revised["id"], "revision_no": 1, "is_current": False}
    assert order_revision_status(store, revised["id"])["revision_no"] == 2
    assert order_revision_status(store, revised["id"])["is_current"] is True
    old_event, new_event = store.history(root["id"])[-1], store.history(revised["id"])[-1]
    assert old_event["action"] == "order_superseded"
    assert new_event["action"] == "order_revision_created"
    assert old_event["detail"] == new_event["detail"]
    assert len(old_event["detail"]["new_payload_sha256"]) == 64


def test_retry_reuses_direct_successor_even_if_later_revision_or_allocation_exists(tmp_path):
    store, root = setup_store(tmp_path)
    second = create_order_revision(store, root["id"], synthetic_order("3"), "合成修订")
    third = create_order_revision(store, second["id"], synthetic_order("4"), "合成再次修订")
    store.create_record("allocation", "SYNTHETIC-SHIPMENT", {"order_id": third["id"], "test_data": True})
    assert create_order_revision(store, root["id"], synthetic_order("3"), " 合成修订 ") == second
    assert order_revision_status(store, second["id"])["latest_order_id"] == third["id"]
    assert len(store.list_records("order")) == 3
    assert len(store.history(root["id"])) == 2
    with pytest.raises(ConflictError):
        create_order_revision(store, root["id"], synthetic_order("3"), "另一个理由")
    with pytest.raises(ConflictError):
        create_order_revision(store, root["id"], synthetic_order("8"), "合成修订")


def test_parallel_identical_requests_create_exactly_one_successor(tmp_path):
    store, root = setup_store(tmp_path)
    barrier = Barrier(8)
    def create(_):
        barrier.wait(timeout=10)
        return create_order_revision(store, root["id"], synthetic_order("7"), "并发合成测试")
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(create, range(8)))
    assert len({record["id"] for record in records}) == 1
    assert len(store.list_records("order")) == 2
    assert len(store.history(root["id"])) == 2
    assert len(store.history(records[0]["id"])) == 1


def test_parallel_different_requests_have_one_winner_and_one_conflict(tmp_path):
    store, root = setup_store(tmp_path)
    barrier = Barrier(2)
    def create(quantity):
        barrier.wait(timeout=10)
        try:
            return create_order_revision(store, root["id"], synthetic_order(quantity), "并发合成测试")
        except ConflictError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("7", "8")))
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert len(store.list_records("order")) == 2


@pytest.mark.parametrize("attempt", range(4))
def test_revision_and_real_allocation_transactions_cannot_both_succeed(tmp_path, attempt):
    store, root = setup_store(tmp_path)
    source_item = deepcopy(root["payload"]["items"][0])
    source_item["quantity"] = "1"
    source = store.create_record("shipment", "SYNTHETIC-SOURCE", {"items": [source_item], "sha256": "SYNTHETIC-HASH", "source": {"sheet": "SYNTHETIC"}})
    barrier = Barrier(2)
    def act(operation):
        barrier.wait(timeout=10)
        try:
            if operation == "revision":
                return create_order_revision(store, root["id"], synthetic_order("3"), "并发合成修订")
            return store.allocate(root["id"], source["id"], [{"order_line_id": "SYNTHETIC-LINE", "shipment_item_index": 1}], "SYNTHETIC ACTUAL SHIPMENT CONFIRMATION")
        except ConflictError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(act, ("revision", "allocation")))
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert len(store.list_records("order")) + len(store.list_records("allocation")) == 2


def test_store_allocation_rejects_superseded_order_version(tmp_path):
    store, root = setup_store(tmp_path)
    revised = create_order_revision(store, root["id"], synthetic_order("3"), "合成修订")
    source_item = deepcopy(root["payload"]["items"][0])
    source = store.create_record("shipment", "SYNTHETIC-SOURCE", {"items": [source_item], "sha256": "SYNTHETIC-HASH"})
    with pytest.raises(ConflictError):
        store.allocate(root["id"], source["id"], [{"order_line_id": "SYNTHETIC-LINE", "shipment_item_index": 1}], "合成出货确认")
    assert order_revision_status(store, revised["id"])["is_current"] is True


@pytest.mark.parametrize("field", ["order_root_id", "revision_no", "supersedes_order_id", "revision_reason", "root_order_id", "latest_order_id", "is_current"])
def test_callers_cannot_supply_revision_metadata(tmp_path, field):
    store, root = setup_store(tmp_path)
    payload = synthetic_order("5")
    payload[field] = "SYNTHETIC FORGED METADATA"
    with pytest.raises(ValueError, match="版本元数据"):
        create_order_revision(store, root["id"], payload, "合成测试")
    assert len(store.list_records("order")) == 1


@pytest.mark.parametrize("existing,new", [(None, "SYNTHETIC-INQUIRY"), ("SYNTHETIC-INQUIRY", None), ("SYNTHETIC-INQUIRY", "SYNTHETIC-OTHER")])
def test_original_inquiry_association_cannot_change(tmp_path, existing, new):
    store, root = setup_store(tmp_path, inquiry=existing)
    with pytest.raises(ConflictError, match="询价关联"):
        create_order_revision(store, root["id"], synthetic_order("5", inquiry=new), "合成测试")


def test_same_inquiry_is_preserved_but_order_number_cannot_change(tmp_path):
    store, root = setup_store(tmp_path, inquiry="SYNTHETIC-INQUIRY")
    payload = synthetic_order("5", inquiry="SYNTHETIC-INQUIRY")
    payload["order_no"] = "SYNTHETIC-OTHER-ORDER"
    with pytest.raises(ConflictError, match="订单号"):
        create_order_revision(store, root["id"], payload, "合成测试")
    payload["order_no"] = root["payload"]["order_no"]
    result = create_order_revision(store, root["id"], payload, "合成测试")
    assert result["payload"]["inquiry_id"] == "SYNTHETIC-INQUIRY"


@pytest.mark.parametrize("allocated_family_member", ["root", "latest"])
def test_any_family_allocation_blocks_new_revision_without_detaching(tmp_path, allocated_family_member):
    store, root = setup_store(tmp_path)
    second = create_order_revision(store, root["id"], synthetic_order("3"), "合成修订")
    allocated = root if allocated_family_member == "root" else second
    allocation = store.create_record("allocation", "SYNTHETIC-SHIPMENT", {"order_id": allocated["id"], "test_data": True})
    with pytest.raises(ConflictError, match="出货分配"):
        create_order_revision(store, second["id"], synthetic_order("4"), "合成测试")
    assert store.get(allocation["id"]) == allocation
    assert len(store.list_records("order")) == 2
    assert order_revision_status(store, second["id"])["is_current"] is True


def test_audit_failure_rolls_back_new_record_and_both_audits(tmp_path, monkeypatch):
    store, root = setup_store(tmp_path)
    original = store._audit
    def fail(con, identity, action, detail):
        original(con, identity, action, detail)
        if action == "order_revision_created":
            raise RuntimeError("SYNTHETIC simulated audit failure")
    monkeypatch.setattr(store, "_audit", fail)
    with pytest.raises(RuntimeError):
        create_order_revision(store, root["id"], synthetic_order("3"), "合成测试")
    assert len(store.list_records("order")) == 1
    assert len(store.history(root["id"])) == 1
    assert order_revision_status(store, root["id"])["is_current"] is True


@pytest.mark.parametrize("reason", [None, "", "   ", 2, "x" * 4001])
def test_reason_is_explicit_and_complete_business_payload_is_required(tmp_path, reason):
    store, root = setup_store(tmp_path)
    with pytest.raises(ValueError):
        create_order_revision(store, root["id"], synthetic_order("3"), reason)
    with pytest.raises(ValueError):
        create_order_revision(store, root["id"], {"quantity": "3"}, "合成测试")


def test_missing_wrong_kind_and_malformed_existing_chain_fail_closed(tmp_path):
    store, root = setup_store(tmp_path)
    with pytest.raises(KeyError):
        order_revision_status(store, "SYNTHETIC-MISSING")
    other = store.create_record("inquiry", "SYNTHETIC-INQUIRY", {"test_data": True})
    with pytest.raises(ValueError, match="类型不匹配"):
        order_revision_status(store, other["id"])
    malformed = {**synthetic_order(), "order_root_id": root["id"], "revision_no": 3,
                 "supersedes_order_id": root["id"], "revision_reason": "SYNTHETIC MALFORMED CHAIN"}
    store.create_record("order", "SYNTHETIC-MALFORMED", malformed)
    with pytest.raises(ConflictError, match="分叉、断档"):
        order_revision_status(store, root["id"])
