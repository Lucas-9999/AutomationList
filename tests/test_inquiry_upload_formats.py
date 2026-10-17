"""Local import/analysis integration using clearly synthetic Word and Excel."""
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from tradeflow.api import create_app
from tradeflow.agent import AgentClient

WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def synthetic_docx(body=None):
    body = body if body is not None else '<w:p><w:r><w:t>SYNTHETIC RFQ: M2x5, 100 PCS.</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>SYNTHETIC BLACK ZINC</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    output = BytesIO()
    with ZipFile(output, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", f'<Relationships xmlns="{PACKAGE_REL}"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<w:document xmlns:w="{WORD}"><w:body>{body}</w:body></w:document>')
    return output.getvalue()


def synthetic_xlsx():
    output = BytesIO()
    with ZipFile(output, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{SHEET}" xmlns:r="{REL}"><sheets><sheet name="SYNTHETIC RFQ" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{PACKAGE_REL}"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{SHEET}"><sheetData><row r="2"><c r="B2" t="inlineStr"><is><t>SYNTHETIC RFQ: M2x5, 100 PCS.</t></is></c></row></sheetData></worksheet>')
    return output.getvalue()


@pytest.mark.parametrize("filename,make,source_type", [("SYNTHETIC.DOCX", synthetic_docx, "inquiry_document"), ("SYNTHETIC.xlsx", synthetic_xlsx, "inquiry_workbook")])
def test_import_preserves_original_and_retry_then_mocked_agent(tmp_path, monkeypatch, filename, make, source_type):
    calls = []
    for key, value in {"TRADEFLOW_AGENT_BASE_URL": "https://synthetic.invalid/v1", "TRADEFLOW_AGENT_API_KEY": "SYNTHETIC_ONLY", "TRADEFLOW_AGENT_MODEL": "SYNTHETIC"}.items():
        monkeypatch.setenv(key, value)
    def transport(url, payload, headers, timeout):
        source = json.loads(payload["messages"][1]["content"])
        calls.append(source)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"technical_chinese": "合成测试：M2x5，100支；尚待核对等级。", "requirements": [{"field": "quantity", "original_quote": "100 PCS", "chinese": "数量100支", "value": "100"}], "questions": ["合成问题：请核对等级"], "warnings": []}, ensure_ascii=False)}}]}
    monkeypatch.setattr("tradeflow.api.AgentClient", lambda: AgentClient(transport=transport))
    content = make()
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/inquiries/upload", files={"file": (filename, content)})
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["payload"]["source_type"] == source_type
        assert saved["payload"]["sha256"] == sha256(content).hexdigest()
        assert "M2x5, 100 PCS" in saved["payload"]["body"]
        assert (tmp_path / "raw" / saved["payload"]["sha256"]).read_bytes() == content
        retry = client.post("/inquiries/upload", files={"file": ("SYNTHETIC-RENAMED" + filename[filename.rfind('.'):], content)})
        assert retry.json()["id"] == saved["id"]
        analyzed = client.post("/inquiries/" + saved["id"] + "/analyze")
        assert analyzed.status_code == 200, analyzed.text
        assert "100支" in analyzed.json()["payload"]["result"]["technical_chinese"]
        assert len(calls) == 1 and "100 PCS" in calls[0]["body"]
        assert client.post("/inquiries/" + saved["id"] + "/analyze").json()["id"] == analyzed.json()["id"]
        assert len(calls) == 1
        assert len(client.get("/records?kind=inquiry").json()) == 1


@pytest.mark.parametrize("body", ['<w:p><w:fldSimple w:instr="SYNTHETIC"><w:r><w:t>100 PCS</w:t></w:r></w:fldSimple></w:p>', '<w:p><w:ins w:id="1"><w:r><w:t>100 PCS</w:t></w:r></w:ins></w:p>', '<w:p/>'])
def test_unverified_word_fails_before_agent_and_preserves_source(tmp_path, monkeypatch, body):
    calls = []
    monkeypatch.setattr("tradeflow.api.AgentClient", lambda: calls.append(True))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        source = client.post("/inquiries/upload", files={"file": ("SYNTHETIC-UNVERIFIED.docx", synthetic_docx(body))})
        assert source.status_code == 200, source.text
        result = client.post("/inquiries/" + source.json()["id"] + "/analyze")
        assert result.status_code == 422 and "未核验" in result.text
        assert not calls and not client.get("/records?kind=analysis").json()
        assert client.get("/audit/" + source.json()["id"]).json()[-1]["action"] == "analysis_failed"


@pytest.mark.parametrize("filename,content", [("SYNTHETIC.docx", b"broken"), ("SYNTHETIC.xlsx", b"broken"), ("SYNTHETIC.doc", b"legacy"), ("SYNTHETIC.xls", b"legacy")])
def test_bad_or_legacy_upload_does_not_create_record_or_original(tmp_path, filename, content):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/inquiries/upload", files={"file": (filename, content)})
        assert response.status_code == 422
        assert not client.get("/records?kind=inquiry").json()
        assert not list((tmp_path / "raw").iterdir())
