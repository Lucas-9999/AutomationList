"""Integration tests use synthetic data and mocked translations only."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from tradeflow.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def order_payload():
    return {"order_no": "SYNTHETIC-001", "buyer": "合成买方", "seller": "合成卖方", "currency": "USD", "items": [{"line_id": "1", "product_name": "合成螺钉", "specification": "M2x5", "quantity": "20", "unit": "PCS", "surface_color": "黑锌", "grade": "12.9", "unit_price": "0.10"}]}


def inquiry(client):
    response = client.post("/inquiries/upload", files={"file": ("synthetic.txt", b"Please quote M2x5, 20 PCS.", "text/plain")})
    assert response.status_code == 200, response.text
    return response.json()


def test_health_and_api_schema(client):
    assert client.get("/health").json()["document_generation"] is False
    assert client.get("/openapi.json").status_code == 200


def test_upload_and_workflow_retries_are_idempotent(client):
    first, second = inquiry(client), inquiry(client)
    assert first["id"] == second["id"]
    workflow = client.post("/workflows", json={"inquiry_id": first["id"]}).json()
    assert client.post("/workflows", json={"inquiry_id": first["id"]}).json()["id"] == workflow["id"]
    assert len(client.get("/audit/" + workflow["id"]).json()) == 1


def test_agent_unconfigured_records_failure_without_fake_translation(client, monkeypatch):
    for key in ("TRADEFLOW_AGENT_BASE_URL", "TRADEFLOW_AGENT_API_KEY", "TRADEFLOW_AGENT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    source = inquiry(client)
    response = client.post("/inquiries/" + source["id"] + "/analyze")
    assert response.status_code == 503
    assert client.get("/audit/" + source["id"]).json()[-1]["action"] == "analysis_failed"


def test_validated_analysis_and_order_flow_with_stale_update(client, monkeypatch):
    source = inquiry(client)
    calls = []

    class SyntheticAgent:
        configuration_fingerprint = "synthetic-configuration-001"

        def analyze_inquiry(self, email):
            calls.append(email)
            return {"technical_chinese": "合成测试：M2x5螺钉20支", "requirements": [], "questions": ["请确认表色"], "warnings": []}

    monkeypatch.setattr("tradeflow.api.AgentClient", SyntheticAgent)
    analysis = client.post("/inquiries/" + source["id"] + "/analyze").json()
    assert client.post("/inquiries/" + source["id"] + "/analyze").json()["id"] == analysis["id"]
    assert len(calls) == 1
    workflow = client.post("/workflows", json={"inquiry_id": source["id"]}).json()
    url = "/workflows/" + workflow["id"] + "/advance"
    approved = client.post(url, json={"target": "technical_ready", "expected_version": 1, "evidence": {"analysis_id": analysis["id"], "technical_approved": True}})
    assert approved.status_code == 200, approved.text
    assert client.post(url, json={"target": "technical_ready", "expected_version": 1, "evidence": {"analysis_id": analysis["id"], "technical_approved": True}}).status_code == 409
    order = client.post("/orders", json=order_payload()).json()
    evidence = {"order_id": order["id"], "customer_confirmation_ref": "synthetic customer PO ref", "commercial_approved": True}
    confirmed = client.post(url, json={"target": "order_confirmed", "expected_version": 2, "evidence": evidence})
    assert confirmed.status_code == 200, confirmed.text
    shipment_payload = {"items": copy.deepcopy(order_payload()["items"]), "source": {"synthetic": True}}
    shipment = client.app.state.store.create_record("shipment", "synthetic-good", shipment_payload)
    result = client.post(url, json={"target": "shipment_verified", "expected_version": 3, "evidence": {"shipment_id": shipment["id"]}})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "shipment_verified"
    blocked = client.post(url, json={"target": "documents_ready", "expected_version": 4, "evidence": {"document_ids": ["fake"], "validation_passed": True}})
    assert blocked.status_code == 422
    history = client.get("/audit/" + workflow["id"]).json()
    assert len([event for event in history if event["action"] != "transition_rejected"]) == 4
    assert history[-1]["action"] == "transition_rejected"


def test_analysis_cache_is_bound_to_provider_configuration(client, monkeypatch):
    source = inquiry(client)
    calls = []

    class SyntheticAgent:
        configuration_fingerprint = "synthetic-first-model"

        def analyze_inquiry(self, email):
            calls.append(self.configuration_fingerprint)
            return {"technical_chinese": "合成测试说明", "requirements": [], "questions": [], "warnings": []}

    monkeypatch.setattr("tradeflow.api.AgentClient", SyntheticAgent)
    url = "/inquiries/" + source["id"] + "/analyze"
    first = client.post(url).json()
    assert client.post(url).json()["id"] == first["id"]
    SyntheticAgent.configuration_fingerprint = "synthetic-second-model"
    second = client.post(url).json()
    assert first["id"] != second["id"]
    assert calls == ["synthetic-first-model", "synthetic-second-model"]


def test_shipment_mismatch_cannot_be_bypassed_by_true_flag(client):
    source = inquiry(client)
    workflow = client.post("/workflows", json={"inquiry_id": source["id"]}).json()
    order = client.post("/orders", json=order_payload()).json()
    client.app.state.store.update_workflow(workflow["id"], 1, "order_confirmed", {"order_id": order["id"]}, {"synthetic_setup": True})
    payload = {"items": copy.deepcopy(order_payload()["items"])}
    payload["items"][0]["quantity"] = "19"
    shipment = client.app.state.store.create_record("shipment", "synthetic-bad", payload)
    result = client.post("/workflows/" + workflow["id"] + "/advance", json={"target": "shipment_verified", "expected_version": 2, "evidence": {"shipment_id": shipment["id"], "reconciliation_passed": True}})
    assert result.status_code == 422
    assert client.get("/workflows/" + workflow["id"]).json()["status"] == "order_confirmed"


def test_sales_order_does_not_automatically_prove_shipment(client):
    source = inquiry(client)
    workflow = client.post("/workflows", json={"inquiry_id": source["id"]}).json()
    order = client.post("/orders", json=order_payload()).json()
    client.app.state.store.update_workflow(workflow["id"], 1, "order_confirmed", {"order_id": order["id"]}, {"synthetic_setup": True})
    shipment = client.app.state.store.create_record("shipment", "synthetic-sales-order", {"items": copy.deepcopy(order_payload()["items"]), "source": {"document_type": "sales_order"}})
    url = "/workflows/" + workflow["id"] + "/advance"
    assert client.post(url, json={"target": "shipment_verified", "expected_version": 2, "evidence": {"shipment_id": shipment["id"]}}).status_code == 422
    approved = client.post(url, json={"target": "shipment_verified", "expected_version": 2, "evidence": {"shipment_id": shipment["id"], "shipment_facts_confirmed": True, "shipment_confirmation_ref": "synthetic physical goods report"}})
    assert approved.status_code == 200, approved.text



def test_unreviewed_template_cannot_make_documents_ready(client):
    template = client.post("/templates/ci/upload", files={"file": ("synthetic.pdf", b"synthetic unverified bytes", "application/pdf")})
    assert template.status_code == 200
    assert template.json()["payload"]["format_accepted"] is False
    order = client.post("/orders", json=order_payload()).json()
    readiness = client.post("/documents/readiness", json={"order_id": order["id"]})
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready"] is False
    assert readiness.json()["generation_available"] is False


def test_invalid_upload_and_mapping_return_errors(client):
    assert client.post("/inquiries/upload", files={"file": ("empty.txt", b"")}).status_code == 422
    assert client.post("/inquiries/upload", files={"file": ("unknown.msg", b"synthetic")}).status_code == 422
    assert client.post("/shipments/upload", data={"mapping_json": "not-json"}, files={"file": ("synthetic.xlsx", b"synthetic")}).status_code == 422
    assert client.get("/records/missing").status_code == 404


def test_business_key_conflict_keeps_original_order(client):
    original = client.post("/orders", json=order_payload())
    changed = order_payload()
    changed["items"][0]["quantity"] = "21"
    assert client.post("/orders", json=changed).status_code == 409
    assert client.get("/records/" + original.json()["id"]).json()["payload"]["total"] == "2.00"


def test_explicit_order_inquiry_link_cannot_be_attached_to_another_workflow(client):
    first = inquiry(client)
    second = client.post("/inquiries/upload", files={"file": ("synthetic-second.txt", b"SYNTHETIC SECOND INQUIRY")}).json()
    business = client.post("/orders", json={**order_payload(), "inquiry_id": first["id"]}).json()
    workflow = client.post("/workflows", json={"inquiry_id": second["id"]}).json()
    client.app.state.store.update_workflow(workflow["id"], 1, "technical_ready", {}, {"synthetic_setup": True})
    response = client.post("/workflows/" + workflow["id"] + "/advance", json={"target": "order_confirmed", "expected_version": 2, "evidence": {"order_id": business["id"], "commercial_approved": True, "customer_confirmation_ref": "SYNTHETIC REF"}})
    assert response.status_code == 422
    assert client.post("/orders", json={**order_payload(), "order_no": "SYNTHETIC-MISSING", "inquiry_id": "missing"}).status_code == 404
