"""Synthetic source-table analysis through the real API/adapter pipeline."""
from email.message import EmailMessage
import json

from fastapi.testclient import TestClient

from tradeflow.api import create_app


def synthetic_eml():
    message = EmailMessage()
    message["Subject"] = "SYNTHETIC RFQ"
    message.set_content("SYNTHETIC RFQ\nDescription\nReference\nQuantity\nNon-magnetic washer M8\nSYNTHETIC-REF-A\n444\nNon-magnetic washer M6\nSYNTHETIC-REF-B\n1258\n")
    message.add_alternative("<p>SYNTHETIC RFQ</p><table><tr><th>Description</th><th>Reference</th><th>Quantity</th></tr>"
                            "<tr><td>Non-magnetic washer M8</td><td>SYNTHETIC-REF-A</td><td>444</td></tr>"
                            "<tr><td>Non-magnetic washer M6</td><td>SYNTHETIC-REF-B</td><td>1258</td></tr></table>", subtype="html")
    return message.as_bytes()


def test_source_rows_plan_translation_binding_cache_and_original_immutability(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEFLOW_AGENT_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("TRADEFLOW_AGENT_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("TRADEFLOW_AGENT_MODEL", "SYNTHETIC_MODEL")
    calls = []
    def transport(url, payload, headers, timeout):
        source = json.loads(payload["messages"][1]["content"])
        calls.append(source)
        if "rows" in source:
            result = {"translations": [{"row_id": row["row_id"], "description_chinese": row["description"].replace("Non-magnetic washer", "非磁性垫圈")} for row in source["rows"]]}
        else:
            result = {"technical_chinese": "合成询价表头。"}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)}}]}
    monkeypatch.setattr("tradeflow.agent._http_transport", transport)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        original = client.post("/inquiries/upload", files={"file": ("SYNTHETIC.eml", synthetic_eml())}).json()
        path = "/inquiries/" + original["id"]
        plan = client.get(path + "/analysis-plan")
        assert plan.status_code == 200, plan.text
        assert plan.json()["structure_summary"]["row_count"] == 2
        assert sum(chunk["row_count"] for chunk in plan.json()["chunks"]) == 2
        response = client.post(path + "/analyze")
        assert response.status_code == 200, response.text
        saved = response.json()
        assert [row["quantity_text"] for row in saved["payload"]["source_rows"]] == ["444", "1258"]
        assert [row["reference_text"] for row in saved["payload"]["source_rows"]] == ["SYNTHETIC-REF-A", "SYNTHETIC-REF-B"]
        assert saved["payload"]["translation_review"]["status"] == "requires_business_review"
        row_calls = [call for call in calls if "rows" in call]
        assert len(row_calls) == 2
        assert all(set(row) == {"row_id", "description"} for call in row_calls for row in call["rows"])
        assert all("444" not in row["description"] and "1258" not in row["description"] for call in row_calls for row in call["rows"])
        assert app.state.store.get(original["id"]) == original
        count = len(calls)
        assert client.post(path + "/analyze").json()["id"] == saved["id"]
        assert len(calls) == count
