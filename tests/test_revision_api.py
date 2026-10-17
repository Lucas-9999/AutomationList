"""Synthetic order revisions invalidate app access to earlier drafts."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tradeflow.api import create_app


def payload():
    return {"order_no": "SYNTHETIC-REVISION", "buyer": "合成买方", "seller": "合成卖方", "currency": "USD", "items": [{"line_id": "A", "product_name": "合成螺钉", "specification": "M2x4", "unit": "PCS", "surface_color": "黑锌", "grade": "12.9", "quantity": "10", "unit_price": "0.10"}]}


def test_revision_invalidates_draft_access_and_old_order_operations(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        original = client.post("/orders", json=payload()).json()
        draft = app.state.store.create_record("draft", "synthetic-review-draft", {"request": {"order_id": original["id"]}, "status": "review_draft"})
        changed = deepcopy(payload())
        changed["items"][0]["unit_price"] = "0.20"
        request = {"payload": changed, "reason": "SYNTHETIC TEST: corrected confirmed price"}
        response = client.post("/orders/" + original["id"] + "/revisions", json=request)
        assert response.status_code == 200, response.text
        current = response.json()
        assert current["payload"]["total"] == "2.00"
        assert client.post("/orders/" + original["id"] + "/revisions", json=request).json()["id"] == current["id"]
        status = client.get("/orders/" + original["id"] + "/revision-status").json()
        assert status["is_current"] is False
        assert status["latest_order_id"] == current["id"]
        old = client.get("/records/" + original["id"]).json()
        assert old["payload"]["total"] == "1.00"
        assert client.get("/records/" + draft["id"]).json()["effective_status"] == "superseded"
        assert client.get("/documents/drafts/" + draft["id"] + "/download").status_code == 409
        assert client.post("/documents/readiness", json={"order_id": original["id"]}).status_code == 409
        assert client.post("/documents/prepare", json={"order_id": original["id"], "template_id": "synthetic"}).status_code == 409
        assert client.post("/documents/drafts", json={"order_id": original["id"], "template_id": "synthetic"}).status_code == 409
        shipment = app.state.store.create_record("shipment", "synthetic-row", {"items": payload()["items"]})
        allocate = client.post("/orders/" + original["id"] + "/allocations", json={"shipment_id": shipment["id"], "lines": [{"order_line_id": "A", "shipment_item_index": 1}], "confirmation_ref": "SYNTHETIC ONLY"})
        assert allocate.status_code == 409


@pytest.mark.parametrize("key,value", [("order_root_id", "fake"), ("revision_no", 99), ("supersedes_order_id", "fake"), ("revision_reason", "fake")])
def test_initial_order_cannot_forge_revision_metadata(tmp_path, key, value):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/orders", json={**payload(), key: value}).status_code == 422
