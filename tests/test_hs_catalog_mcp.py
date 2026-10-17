"""SYNTHETIC MCP protocol fixtures. No real credentials or external calls."""
from copy import deepcopy
import json
from urllib.error import HTTPError, URLError

import pytest

from tradeflow import hs_catalog as catalog
from tradeflow.hs_catalog_mcp import HsciqMcpProvider
from tradeflow.store import Store

KEY = "hsapi_SYNTHETIC_MCP_NOT_REAL"
CODE = "0000000000"


def detail(**changes):
    return {"country": "CN", "code": CODE, "name": "SYNTHETIC CATEGORY", "status": 1,
            "updateTime": "2026-10-17T00:00:00Z", "unit1": "千克", "unit2": "无", "remark": "SYNTHETIC ONLY",
            "extensions": {"cn": {"reporting": [{"key": "1", "value": "SYNTHETIC 品牌", "isRequired": True}, {"key": "0", "value": "SYNTHETIC 规格"}, {"key": "2", "value": "SYNTHETIC 可选", "isRequired": False}]}}, **changes}


class Transport:
    def __init__(self, data=None, *, sse=True):
        self.data = data if data is not None else detail()
        self.sse = sse
        self.calls = []
        self.error = None
        self.http_status = 200
        self.wrong_id = False
        self.raw_body = None

    def __call__(self, req, timeout):
        self.calls.append((req, timeout))
        payload = json.loads(req.data)
        envelope = {"jsonrpc": "2.0", "id": "SYNTHETIC-WRONG" if self.wrong_id else payload["id"],
                    "result": {"isError": self.error is not None, "structuredContent": self.data,
                               "content": [{"type": "text", "text": self.error or "SYNTHETIC response"}]}}
        rendered = json.dumps(envelope, ensure_ascii=False, indent=2)
        body = (": SYNTHETIC heartbeat\r\nevent: message\r\n" + "\r\n".join("data: " + line for line in rendered.splitlines()) + "\r\n\r\n").encode() if self.sse else rendered.encode()
        return self.http_status, {"Content-Type": "text/event-stream" if self.sse else "application/json"}, self.raw_body if self.raw_body is not None else body


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEFLOW_HSCIQ_API_KEY", KEY)
    monkeypatch.setenv("TRADEFLOW_HSCIQ_PROTOCOL", "mcp")
    monkeypatch.setenv("TRADEFLOW_HS_PROVIDER", "hsciq")
    transport = Transport()
    monkeypatch.setattr(catalog, "_provider", lambda: HsciqMcpProvider(transport=transport))
    return Store(tmp_path), transport


def test_reproduce_rest_rejects_key_mcp_accepts_same_key(setup):
    store, transport = setup
    rest = catalog.HsciqProvider(transport=lambda url, timeout: (200, {}, b'{"error":"API Key error"}'))
    with pytest.raises(catalog.CatalogError) as caught:
        rest.detail(CODE)
    assert caught.value.code == "authentication_failed"
    result = catalog.lookup(store, CODE)
    assert result["provider"] == "hsciq_mcp" and result["status"] == "active"
    req, timeout = transport.calls[0]
    assert req.full_url == "https://www.hsciq.com/mcp/rpc" and KEY not in req.full_url
    assert req.get_method() == "POST" and dict((key.lower(), value) for key, value in req.header_items())["x-api-key"] == KEY
    payload = json.loads(req.data)
    assert payload["method"] == "tools/call"
    assert payload["params"] == {"name": "get_code_detail", "arguments": {"country": "CN", "code": CODE}}
    assert KEY not in req.data.decode() and timeout == 15
    assert result["reporting_elements"] == [{"order": 0, "name": "SYNTHETIC 规格", "required": None}, {"order": 1, "name": "SYNTHETIC 品牌", "required": True}, {"order": 2, "name": "SYNTHETIC 可选", "required": False}]
    assert KEY.encode() not in store.db.read_bytes()


def test_json_pascal_documented_detail_compatible(setup):
    store, transport = setup
    transport.sse = False
    raw = detail()
    raw["extensions"] = {"Cn": {"Reporting": [{"Key": "0", "Value": "SYNTHETIC 品牌", "IsRequired": True}]}}
    transport.data = {key[0].upper() + key[1:]: value for key, value in raw.items()}
    result = catalog.lookup(store, CODE)
    assert result["unit1"] == "千克" and result["reporting_elements"][0]["required"] is True


@pytest.mark.parametrize("reporting", [[{"code": "0", "name": "SYNTHETIC CODE ORDER", "required": True}], [{"order": 2, "name": "SYNTHETIC INT ORDER", "required": None}]])
def test_reporting_explicit_order_aliases_and_unknown_required(setup, reporting):
    store, transport = setup
    transport.data["extensions"]["cn"]["reporting"] = reporting
    result = catalog.lookup(store, CODE)
    assert result["reporting_elements"][0]["order"] in (0, 2)


@pytest.mark.parametrize("changes", [{"country": "US"}, {"country": None}, {"code": "000000"}, {"code": "0000000001"}, {"name": None}, {"extensions": []}, {"extensions": {"cn": {"reporting": [{"key": "a", "value": "SYNTHETIC"}]}}}, {"extensions": {"cn": {"reporting": [{"key": "0", "value": "SYNTHETIC"}, {"key": "0", "value": "SYNTHETIC DUP"}]}}}])
def test_detail_schema_invalid_never_cached(setup, changes):
    store, transport = setup
    transport.data = detail(**changes)
    with pytest.raises(catalog.CatalogError):
        catalog.lookup(store, CODE)
    assert not store.list_records("hs_catalog_snapshot")


@pytest.mark.parametrize("error,expected", [("invalid_api_key: SYNTHETIC", "authentication_failed"), ("api_key_expired: SYNTHETIC", "authentication_failed"), ("api_key_revoked: SYNTHETIC", "authentication_failed"), ("missing_api_key: SYNTHETIC", "authentication_failed"), ("daily_quota_exceeded: SYNTHETIC", "rate_limited"), ("daily_guilei_quota_exceeded: SYNTHETIC", "rate_limited"), ("not_found: SYNTHETIC", "not_found"), ("unknown_tool: SYNTHETIC", "unsupported_operation"), ("country_not_supported: SYNTHETIC", "unsupported_operation"), ("invalid_params: SYNTHETIC", "invalid_input"), ("forbidden_keyword: SYNTHETIC", "invalid_input"), ("internal_error: SYNTHETIC", "unavailable"), ("unexpected_failure: SYNTHETIC", "unavailable")])
def test_tool_error_codes_are_semantic_and_safe(setup, error, expected):
    store, transport = setup
    transport.error = error
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == expected
    assert "SYNTHETIC" not in str(caught.value)
    assert not store.list_records("hs_catalog_snapshot")


@pytest.mark.parametrize("status,expected", [(301, "redirect_blocked"), (307, "redirect_blocked"), (401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (404, "not_found"), (500, "unavailable")])
def test_http_failures(setup, status, expected):
    store, transport = setup
    transport.http_status = status
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == expected


@pytest.mark.parametrize("remark", [KEY, "https://synthetic.invalid/private", "\\u0068sapi_SYNTHETIC_OTHER"])
def test_secret_or_url_echo_never_persisted(setup, remark):
    store, transport = setup
    transport.data["remark"] = remark
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == "unsafe_response"
    assert KEY.encode() not in store.db.read_bytes()


def test_sse_id_mismatch_rejected(setup):
    store, transport = setup
    transport.wrong_id = True
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == "malformed_response"


@pytest.mark.parametrize("body", [b'data: bad json\n\n', b'\xff', b': only heartbeat\n\n', b'data: []\n\n', b'x' * (catalog.MAX_RESPONSE_BYTES + 1)], ids=["bad-json", "bad-utf8", "heartbeat-only", "array", "oversized"])
def test_malformed_or_unbounded_sse_rejected(setup, body):
    store, transport = setup
    transport.raw_body = body
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == "malformed_response"


def test_status_paused_is_expired_blockable(setup):
    store, transport = setup
    transport.data["status"] = 3
    assert catalog.lookup(store, CODE)["status"] == "expired"


def test_updates_explicitly_unsupported_does_not_make_network_call(setup):
    store, transport = setup
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.updates(store)
    assert caught.value.code == "unsupported_operation" and not transport.calls
    assert catalog.status()["protocol"] == "mcp"
    assert catalog.status()["capabilities"] == {"search": True, "detail": True, "updates": False}


def test_search_normalization_pagination_and_readonly_allowlist(setup):
    store, transport = setup
    transport.data = {"items": [detail()], "pageIndex": 2, "pageSize": 10, "totalPageCount": 3, "totalItemCount": 25, "isNextPage": True}
    result = catalog.search(store, "00", 2)
    assert result["has_more"] is True and result["items"][0]["hs_code"] == CODE
    req = transport.calls[0][0]
    assert json.loads(req.data)["params"] == {"name": "search_code", "arguments": {"country": "CN", "keywords": "00", "pageIndex": 2, "pageSize": 10}}
    provider = HsciqMcpProvider(transport=transport)
    with pytest.raises(catalog.CatalogError) as caught:
        provider._call("create_guilei_form", {})
    assert caught.value.code == "unsupported_operation" and len(transport.calls) == 1


def test_protocol_selection_and_provider_isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEFLOW_HSCIQ_API_KEY", KEY)
    monkeypatch.setenv("TRADEFLOW_HS_PROVIDER", "hsciq")
    monkeypatch.delenv("TRADEFLOW_HSCIQ_PROTOCOL", raising=False)
    assert isinstance(catalog._provider(), catalog.HsciqProvider)
    rest = catalog.HsciqProvider(transport=lambda url, timeout: (200, {}, json.dumps({"HSCode": CODE, "Name": "SYNTHETIC REST", "Status": 1}).encode()))
    original = catalog._provider
    monkeypatch.setattr(catalog, "_provider", lambda: rest)
    store = Store(tmp_path)
    first = catalog.lookup(store, CODE)
    transport = Transport()
    monkeypatch.setattr(catalog, "_provider", lambda: HsciqMcpProvider(transport=transport))
    assert catalog.latest_snapshot(store, CODE) is None
    second = catalog.lookup(store, CODE)
    assert second["version"] == 1 and second["snapshot_id"] != first["snapshot_id"]
    assert catalog.latest_snapshot(store, CODE)["snapshot_id"] == second["snapshot_id"]
    monkeypatch.setattr(catalog, "_provider", original)
    monkeypatch.setenv("TRADEFLOW_HSCIQ_PROTOCOL", "mcp")
    assert isinstance(catalog._provider(), HsciqMcpProvider)
    monkeypatch.setenv("TRADEFLOW_HSCIQ_PROTOCOL", "SYNTHETIC_UNKNOWN")
    with pytest.raises(catalog.CatalogError):
        catalog._provider()


def test_jsonrpc_protocol_error_codes_and_notifications(setup):
    store, transport = setup
    def response(req, timeout):
        identity = json.loads(req.data)["id"]
        notification = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        error = {"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": "SYNTHETIC arbitrary text"}}
        return 200, {"Content-Type": "text/event-stream"}, ("data: " + json.dumps(notification) + "\n\ndata: " + json.dumps(error) + "\n\n").encode()
    with pytest.raises(catalog.CatalogError) as caught:
        HsciqMcpProvider(transport=response).detail(CODE)
    assert caught.value.code == "unsupported_operation"


def test_mcp_network_exceptions_are_safe(setup):
    store, transport = setup
    for error, expected in [(TimeoutError(KEY), "timeout"), (URLError(TimeoutError(KEY)), "timeout"), (URLError(KEY), "unavailable"), (RuntimeError(KEY), "unavailable"), (HTTPError("https://synthetic.invalid/" + KEY, 302, KEY, {}, None), "redirect_blocked")]:
        def fail(req, timeout):
            raise error
        with pytest.raises(catalog.CatalogError) as caught:
            HsciqMcpProvider(transport=fail).detail(CODE)
        assert caught.value.code == expected and KEY not in str(caught.value)


def test_text_content_json_fallback_and_escaped_secret_rejection(setup, monkeypatch):
    store, transport = setup
    credential = "SYNTHETIC_CREDENTIAL_WITHOUT_PROVIDER_PREFIX"
    monkeypatch.setenv("TRADEFLOW_HSCIQ_API_KEY", credential)
    for secret in (False, True):
        data = detail(remark="SYNTHETIC")
        if secret:
            data["remark"] = credential
        inner = json.dumps(data, ensure_ascii=False)
        if secret:
            inner = inner.replace(credential, "".join("\\u%04x" % ord(char) for char in credential))
        def response(req, timeout):
            envelope = {"jsonrpc": "2.0", "id": json.loads(req.data)["id"], "result": {"isError": False, "content": [{"type": "text", "text": inner}]}}
            return 200, {"content-type": "application/json"}, json.dumps(envelope).encode()
        provider = HsciqMcpProvider(transport=response)
        if secret:
            with pytest.raises(catalog.CatalogError) as caught:
                provider.detail(CODE)
            assert caught.value.code == "unsafe_response"
        else:
            assert provider.detail(CODE)[0]["hs_code"] == CODE


def test_fresh_cached_mcp_lookup_is_idempotent_and_refresh_same_content_keeps_snapshot(setup):
    store, transport = setup
    first = catalog.lookup(store, CODE)
    cached = catalog.lookup(store, CODE)
    assert cached["cache_hit"] is True and len(transport.calls) == 1
    refreshed = catalog.lookup(store, CODE, refresh=True)
    assert len(transport.calls) == 2
    assert refreshed["snapshot_id"] == first["snapshot_id"] and refreshed["version"] == 1
    assert len(store.list_records("hs_catalog_snapshot")) == 1
    assert refreshed["response_sha256"] == first["response_sha256"]


def test_no_key_cannot_issue_mcp_network_request(setup, monkeypatch):
    store, transport = setup
    monkeypatch.delenv("TRADEFLOW_HSCIQ_API_KEY")
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == "not_configured" and transport.calls == []


@pytest.mark.parametrize("embedded", ["https://synthetic.invalid/private", "hsapi_SYNTHETIC_OTHER_CREDENTIAL"])
def test_embedded_json_decoded_urls_and_generic_api_keys_rejected(setup, embedded):
    store, transport = setup
    data = detail(remark=embedded)
    inner = json.dumps(data).replace(embedded, "".join("\\u%04x" % ord(char) for char in embedded))
    def response(req, timeout):
        envelope = {"jsonrpc": "2.0", "id": json.loads(req.data)["id"], "result": {"isError": False, "content": [{"type": "text", "text": inner}]}}
        return 200, {"content-type": "application/json"}, json.dumps(envelope).encode()
    with pytest.raises(catalog.CatalogError) as caught:
        HsciqMcpProvider(transport=response).detail(CODE)
    assert caught.value.code == "unsafe_response"
