from concurrent.futures import ThreadPoolExecutor

import pytest

from tradeflow.store import ConflictError, Store


def test_original_is_content_addressed(tmp_path):
    store = Store(tmp_path)
    digest = store.save_original(b"synthetic source")
    assert store.save_original(b"synthetic source") == digest
    assert (tmp_path / "raw" / digest).read_bytes() == b"synthetic source"


def test_record_retries_and_conflicts_do_not_overwrite(tmp_path):
    store = Store(tmp_path)
    first = store.create_record("order", "SYNTHETIC", {"total": "12"})
    assert store.create_record("order", "SYNTHETIC", {"total": "12"})["id"] == first["id"]
    with pytest.raises(ConflictError):
        store.create_record("order", "SYNTHETIC", {"total": "13"})
    assert store.get(first["id"])["payload"]["total"] == "12"
    assert len(store.history(first["id"])) == 1


def test_atomic_concurrent_retries_create_one_record(tmp_path):
    store = Store(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: store.create_record("inquiry", "same", {"body": "synthetic"}), range(8)))
    assert len({record["id"] for record in records}) == 1
    assert len(store.history(records[0]["id"])) == 1


def test_stale_workflow_update_preserves_newer_version(tmp_path):
    store = Store(tmp_path)
    inquiry = store.create_record("inquiry", "synthetic", {"body": "synthetic"})
    first = store.create_workflow(inquiry["id"])
    assert store.create_workflow(inquiry["id"])["id"] == first["id"]
    next_record = store.update_workflow(first["id"], 1, "technical_ready", {"analysis_id": "synthetic"}, {"technical_approved": True})
    with pytest.raises(ConflictError):
        store.update_workflow(first["id"], 1, "order_confirmed", {}, {})
    assert store.workflow(first["id"])["version"] == 2
    assert next_record["status"] == "technical_ready"
    assert len(store.history(first["id"])) == 2


def test_missing_and_wrong_kind_are_distinct(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(KeyError):
        store.get("missing")
    inquiry = store.create_record("inquiry", "synthetic", {})
    with pytest.raises(ValueError):
        store.get(inquiry["id"], "order")
