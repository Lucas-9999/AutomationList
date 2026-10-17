"""Synthetic inquiry responses; tests never call an external API."""
import copy
import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, build_opener as real_build_opener
from urllib.response import addinfourl

import pytest

from tradeflow.agent import AgentClient
from tradeflow import agent as agent_module


EMAIL = {"body": "Synthetic inquiry: 100 screws M2x5, black zinc.", "warnings": [], "attachments": []}
RESULT = {"technical_chinese": "合成询价：100 支 M2x5 螺钉，黑锌。", "requirements": [
    {"field": "quantity", "original_quote": "100 screws", "chinese": "数量为 100 支", "value": "100"}],
    "questions": ["请确认等级"], "warnings": []}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("TRADEFLOW_AGENT_BASE_URL", "https://synthetic.example.invalid/v1")
    monkeypatch.setenv("TRADEFLOW_AGENT_API_KEY", "SYNTHETIC_SECRET_KEY")
    monkeypatch.setenv("TRADEFLOW_AGENT_MODEL", "synthetic-model")
    for key in ("TRADEFLOW_AGENT_RESPONSE_FORMAT", "TRADEFLOW_AGENT_THINKING", "TRADEFLOW_AGENT_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)


def response(result=None, **kwargs):
    return {"choices": [{"finish_reason": kwargs.get("finish_reason", "stop"), "message": {
        "refusal": kwargs.get("refusal"), "content": kwargs.get("content", json.dumps(RESULT if result is None else result, ensure_ascii=False))}}]}


def test_strict_schema_mock_transport_and_source_separation():
    captured = {}
    def transport(url, payload, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return response()
    result = AgentClient(transport=transport).analyze_inquiry(EMAIL)
    assert result == RESULT
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["payload"]["response_format"]["json_schema"]["strict"] is True
    assert "不可信" in captured["payload"]["messages"][0]["content"]
    assert "Synthetic inquiry" not in captured["payload"]["messages"][0]["content"]
    assert captured["headers"]["Authorization"] == "Bearer SYNTHETIC_SECRET_KEY"


def test_json_object_configuration_sends_schema_and_retains_local_validation(monkeypatch):
    monkeypatch.setenv("TRADEFLOW_AGENT_RESPONSE_FORMAT", "json_object")
    monkeypatch.setenv("TRADEFLOW_AGENT_THINKING", "disabled")
    monkeypatch.setenv("TRADEFLOW_AGENT_MAX_TOKENS", "4096")
    captured = []
    def transport(url, payload, headers, timeout):
        captured.append(payload)
        return response()
    assert AgentClient(transport=transport).analyze_inquiry(EMAIL) == RESULT
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[0]["max_tokens"] == 4096
    assert json.dumps(agent_module.SCHEMA, ensure_ascii=False) in captured[0]["messages"][0]["content"]
    bad = copy.deepcopy(RESULT)
    bad["requirements"][0]["original_quote"] = "SYNTHETIC INVENTED QUOTE"
    with pytest.raises(ValueError, match="原文引用"):
        AgentClient(transport=lambda *args: response(bad)).analyze_inquiry(EMAIL)


@pytest.mark.parametrize("key,value", [("TRADEFLOW_AGENT_RESPONSE_FORMAT", "text"), ("TRADEFLOW_AGENT_THINKING", "yes"), ("TRADEFLOW_AGENT_MAX_TOKENS", "0"), ("TRADEFLOW_AGENT_MAX_TOKENS", "32769"), ("TRADEFLOW_AGENT_MAX_TOKENS", "1.5"), ("TRADEFLOW_AGENT_MAX_TOKENS", "１２")])
def test_unsupported_generation_configuration_fails_before_transport(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        AgentClient()


@pytest.mark.parametrize("key,value", [("TRADEFLOW_AGENT_RESPONSE_FORMAT", "json_object"), ("TRADEFLOW_AGENT_THINKING", "disabled"), ("TRADEFLOW_AGENT_MAX_TOKENS", "4096")])
def test_generation_configuration_changes_analysis_cache_fingerprint(monkeypatch, key, value):
    original = AgentClient().configuration_fingerprint
    monkeypatch.setenv(key, value)
    assert AgentClient().configuration_fingerprint != original


@pytest.mark.parametrize("field", ["TRADEFLOW_AGENT_BASE_URL", "TRADEFLOW_AGENT_API_KEY", "TRADEFLOW_AGENT_MODEL"])
def test_missing_configuration_fails(monkeypatch, field):
    monkeypatch.delenv(field)
    with pytest.raises(ValueError, match="未配置"):
        AgentClient()


@pytest.mark.parametrize("url", ["http://remote.example.invalid/v1", "https://" + "name:secret@example.invalid", "https://example.invalid?key=secret", "file:///x"])
def test_invalid_url_fails(monkeypatch, url):
    monkeypatch.setenv("TRADEFLOW_AGENT_BASE_URL", url)
    with pytest.raises(ValueError):
        AgentClient()


@pytest.mark.parametrize("reply,pattern", [(response(refusal="refused"), "拒绝"), (response(finish_reason="length"), "截断"), (response(content="not json"), "无效 JSON"), ({"error": {"message": "SYNTHETIC_SECRET_KEY"}}, "错误"), ({"choices": []}, "choices")])
def test_refusal_truncation_invalid_responses(reply, pattern):
    with pytest.raises(ValueError, match=pattern) as error:
        AgentClient(transport=lambda *args: reply).analyze_inquiry(EMAIL)
    assert "SYNTHETIC_SECRET_KEY" not in str(error.value)


@pytest.mark.parametrize("change", [
    lambda r: r.update(technical_chinese="English only"),
    lambda r: r.update(warnings=[1]),
    lambda r: r.update(questions="not an array"),
    lambda r: r["requirements"][0].update(value=100),
    lambda r: r["requirements"][0].update(original_quote="invented quote"),
    lambda r: r["requirements"][0].update(value="999"),
    lambda r: r["requirements"][0].update(chinese="no Chinese"),
    lambda r: r.update(unexpected=True),
])
def test_schema_and_evidence_validation(change):
    result = copy.deepcopy(RESULT)
    change(result)
    with pytest.raises(ValueError):
        AgentClient(transport=lambda *args: response(result)).analyze_inquiry(EMAIL)


@pytest.mark.parametrize("error", [URLError("SYNTHETIC_SECRET_KEY"), TimeoutError("SYNTHETIC_SECRET_KEY"), RuntimeError("SYNTHETIC_SECRET_KEY"), HTTPError("url", 400, "SYNTHETIC_SECRET_KEY", None, None)])
def test_network_errors_redact_secrets(error):
    def transport(*args):
        raise error
    with pytest.raises(ValueError) as caught:
        AgentClient(transport=transport).analyze_inquiry(EMAIL)
    assert "SYNTHETIC_SECRET_KEY" not in str(caught.value)


def test_attachment_warning_is_preserved_even_if_model_omits():
    email = {**EMAIL, "attachments": [{"filename": "synthetic.bin"}], "warnings": ["合成来源告警"]}
    result = AgentClient(transport=lambda *args: response()).analyze_inquiry(email)
    assert "合成来源告警" in result["warnings"]
    assert any("附件" in item for item in result["warnings"])


def test_chunk_context_is_sent_separately_and_cannot_supply_evidence():
    captured = []
    context = {"index": 2, "count": 3, "start": 100, "end": 100 + len(EMAIL["body"]), "context_before": "SYNTHETIC PREVIOUS CONTEXT ONLY", "context_after": "SYNTHETIC NEXT CONTEXT ONLY"}
    def transport(url, payload, headers, timeout):
        captured.append(json.loads(payload["messages"][1]["content"]))
        return response()
    assert AgentClient(transport=transport).analyze_inquiry({**EMAIL, "chunk_context": context}) == RESULT
    assert captured[0]["body"] == EMAIL["body"] and captured[0]["chunk_context"] == context
    result = copy.deepcopy(RESULT)
    result["requirements"][0].update(original_quote="SYNTHETIC PREVIOUS CONTEXT ONLY", value="SYNTHETIC")
    with pytest.raises(ValueError, match="原文引用"):
        AgentClient(transport=lambda *args: response(result)).analyze_inquiry({**EMAIL, "chunk_context": context})


def test_chunk_prose_does_not_receive_global_subject_as_local_evidence():
    captured = []
    def transport(url, payload, headers, timeout):
        captured.append(json.loads(payload["messages"][1]["content"]))
        return response()
    AgentClient(transport=transport).analyze_inquiry({**EMAIL, "subject": "SYNTHETIC GLOBAL SUBJECT", "sender": "SYNTHETIC GLOBAL SENDER",
                                                    "chunk_context": {"index": 2, "count": 2}})
    assert not {"subject", "sender", "message_id"} & set(captured[0])
    assert captured[0]["body"] == EMAIL["body"]


@pytest.mark.parametrize("source", [{"text_analysis_available": False}, {"text_analysis_available": True, "analysis_blockers": ["SYNTHETIC unverified field"]}])
def test_word_source_blockers_stop_direct_agent_transport(source):
    calls = []
    client = AgentClient(transport=lambda *args: calls.append(True))
    with pytest.raises(ValueError, match="未核验"):
        client.analyze_inquiry({**EMAIL, "source_type": "inquiry_document", "source": source})
    assert calls == []


def test_duplicate_json_keys_blocked():
    duplicate = json.dumps(RESULT, ensure_ascii=False)[:-1] + ', "technical_chinese":"重复合成值"}'
    with pytest.raises(ValueError, match="重复字段"):
        AgentClient(transport=lambda *args: response(content=duplicate)).analyze_inquiry(EMAIL)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("destination", ["https://synthetic.example.invalid/v1/new", "https://other.synthetic.invalid/steal?token=SYNTHETIC_SECRET_KEY"])
def test_http_redirect_never_sends_second_request(monkeypatch, status, destination):
    """Exercise urllib's real handler chain, replacing only HTTPS I/O."""
    sent_requests = []

    class SyntheticHTTPS(HTTPSHandler):
        def https_open(self, req):
            sent_requests.append((req.full_url, req.get_header("Authorization")))
            headers = Message()
            headers["Location"] = destination
            reply = addinfourl(io.BytesIO(b""), headers, req.full_url, status)
            reply.msg = "SYNTHETIC_SECRET_KEY"
            return reply

    monkeypatch.setattr(agent_module, "build_opener", lambda *handlers: real_build_opener(*handlers, SyntheticHTTPS()))
    with pytest.raises(ValueError, match="重定向被拒绝") as caught:
        AgentClient().analyze_inquiry(EMAIL)
    assert sent_requests == [("https://synthetic.example.invalid/v1/chat/completions", "Bearer SYNTHETIC_SECRET_KEY")]
    assert "SYNTHETIC_SECRET_KEY" not in str(caught.value)
    assert destination not in str(caught.value)


def test_configuration_fingerprint_stable_readonly_and_excludes_key(monkeypatch):
    client = AgentClient()
    original = client.configuration_fingerprint
    assert len(original) == 64
    assert original == AgentClient().configuration_fingerprint
    monkeypatch.setenv("TRADEFLOW_AGENT_API_KEY", "OTHER_SYNTHETIC_SECRET")
    assert AgentClient().configuration_fingerprint == original
    with pytest.raises(AttributeError):
        client.configuration_fingerprint = "cannot overwrite"


@pytest.mark.parametrize("name,value", [("TRADEFLOW_AGENT_BASE_URL", "https://other.synthetic.invalid/v1"), ("TRADEFLOW_AGENT_MODEL", "other-synthetic-model")])
def test_configuration_fingerprint_changes_with_service_or_model(monkeypatch, name, value):
    original = AgentClient().configuration_fingerprint
    monkeypatch.setenv(name, value)
    assert AgentClient().configuration_fingerprint != original


@pytest.mark.parametrize("attribute,value", [("PROMPT_SCHEMA_VERSION", "inquiry-analysis-synthetic-v2"), ("SYSTEM_PROMPT", "合成替换指令"), ("SCHEMA", {"type": "object", "synthetic": True})])
def test_configuration_fingerprint_changes_with_prompt_schema(monkeypatch, attribute, value):
    client = AgentClient()
    original = client.configuration_fingerprint
    monkeypatch.setattr(agent_module, attribute, value)
    assert client.configuration_fingerprint != original
