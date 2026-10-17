"""Synthetic chunk endpoint integration, never calls an external service."""
from collections import Counter

from fastapi.testclient import TestClient

from tradeflow.api import create_app


BODY = "\n\n".join(f"SYNTHETIC LOT {index}: 100 PCS of M2x5 screws. Do not substitute stainless steel. " + "SYNTHETIC CONTEXT " * 30 for index in range(8))


def test_chunk_plan_partial_failure_and_resumed_final_aggregation(tmp_path, monkeypatch):
    calls = []
    class SyntheticAgent:
        configuration_fingerprint = "SYNTHETIC-CHUNK-API"
        failed = False
        def analyze_inquiry(self, inquiry):
            index = inquiry["chunk_context"]["index"]
            calls.append(index)
            if index == 2 and not self.failed:
                SyntheticAgent.failed = True
                raise ValueError("Agent 响应截断或未正常结束；结果不接受")
            return {"technical_chinese": f"合成第{index}块，保留原始要求。", "requirements": [], "questions": [], "warnings": []}
    monkeypatch.setattr("tradeflow.api.AgentClient", SyntheticAgent)
    with TestClient(create_app(tmp_path)) as client:
        inquiry = client.post("/inquiries/upload", files={"file": ("SYNTHETIC-CHUNKS.txt", BODY.encode())}).json()
        url = "/inquiries/" + inquiry["id"]
        plan = client.get(url + "/analysis-plan").json()
        assert plan["chunk_count"] >= 3
        assert plan["chunks"][0]["start"] == 0 and plan["chunks"][-1]["end"] == len(BODY)
        assert all("text" not in row and not row["cached"] for row in plan["chunks"])
        first, second = plan["chunks"][:2]
        endpoint = lambda chunk: url + "/analysis-chunks/" + chunk["id"]
        body = {"plan_id": plan["plan_id"]}
        saved = client.post(endpoint(first), json=body)
        assert saved.status_code == 200, saved.text
        assert not client.get("/records?kind=analysis").json()
        failed = client.post(endpoint(second), json=body)
        assert failed.status_code == 503 and "截断" in failed.text and "未配置" not in failed.text
        audit = client.get("/audit/" + inquiry["id"]).json()[-1]
        assert audit["detail"]["reason"] == "agent_output_incomplete" and audit["detail"]["chunk_id"] == second["id"]
        resumed = client.get(url + "/analysis-plan").json()
        assert resumed["chunks"][0]["cached"] and not resumed["chunks"][1]["cached"]
        assert client.post(endpoint(first), json=body).json()["id"] == saved.json()["id"]
        assert calls == [1, 2]
        assert client.post(endpoint(first), json={"plan_id": "SYNTHETIC-STALE"}).status_code == 409
        assert client.post(url + "/analysis-chunks/SYNTHETIC-UNKNOWN", json=body).status_code == 404
        final = client.post(url + "/analyze")
        assert final.status_code == 200, final.text
        assert Counter(calls) == Counter({index: 2 if index == 2 else 1 for index in range(1, plan["chunk_count"] + 1)})
        previous = len(calls)
        assert client.post(url + "/analyze").json()["id"] == final.json()["id"]
        assert len(calls) == previous
        assert len(client.get("/records?kind=analysis").json()) == 1
        assert len(client.get("/records?kind=analysis_chunk").json()) == plan["chunk_count"]


def test_unknown_failure_never_exposes_raw_exception(tmp_path, monkeypatch):
    class SyntheticAgent:
        configuration_fingerprint = "SYNTHETIC-UNSAFE-ERROR"
        def analyze_inquiry(self, inquiry):
            raise ValueError("SYNTHETIC_PRIVATE_CLIENT sk-SYNTHETIC_SECRET https://private.invalid/key")
    monkeypatch.setattr("tradeflow.api.AgentClient", SyntheticAgent)
    with TestClient(create_app(tmp_path)) as client:
        inquiry = client.post("/inquiries/upload", files={"file": ("SYNTHETIC.txt", b"SYNTHETIC inquiry")}).json()
        response = client.post("/inquiries/" + inquiry["id"] + "/analyze")
        assert response.status_code == 503
        assert "SYNTHETIC_PRIVATE_CLIENT" not in response.text and "sk-" not in response.text and "https://" not in response.text
        audit = client.get("/audit/" + inquiry["id"]).json()[-1]
        assert audit["detail"]["reason"] == "analysis_failed"
        assert "SYNTHETIC_SECRET" not in str(audit)
        assert not client.get("/records?kind=analysis").json()
