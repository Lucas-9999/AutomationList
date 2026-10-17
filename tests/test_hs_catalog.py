"""SYNTHETIC catalog fixtures; no authenticated external API calls or business HS claims."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import pytest

from tradeflow import hs_catalog as catalog
from tradeflow.store import Store

KEY = "hsapi_SYNTHETIC_NEVER_REAL_CREDENTIAL"
CODE = "0000000000"


def detail(**changes):
    return {"HSCode": CODE, "Name": "SYNTHETIC CATEGORY MANY PRODUCTS", "Remark": "SYNTHETIC ONLY", "Unit1": "千克", "Unit2": "个", "Reporting": "0:合成品牌类型:1$1:合成规格:1$2:合成可选项:0$3:合成旧格式", "Status": 1, "UpdateTime": "2026/1/1", **changes}


class Transport:
    def __init__(self, data=None, status=200):
        self.data = data if data is not None else detail()
        self.status = status
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        return self.status, {"content-type": "application/json"}, json.dumps(self.data, ensure_ascii=False).encode()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEFLOW_HSCIQ_API_KEY", KEY)
    monkeypatch.setenv("TRADEFLOW_HS_PROVIDER", "hsciq")
    transport = Transport()
    monkeypatch.setattr(catalog, "_provider", lambda: catalog.HsciqProvider(transport=transport))
    return Store(tmp_path), transport


def test_documented_detail_schema_and_security(setup):
    store, transport = setup
    result = catalog.lookup(store, "00000000.00")
    assert result["hs_code"] == CODE
    assert result["status"] == "active" and result["version"] == 1
    assert result["cache_hit"] is False and result["stale"] is False
    assert result["provider_updated_at"] == "2026/1/1"
    assert result["reporting_elements"] == [
        {"order": 0, "name": "合成品牌类型", "required": True},
        {"order": 1, "name": "合成规格", "required": True},
        {"order": 2, "name": "合成可选项", "required": False},
        {"order": 3, "name": "合成旧格式", "required": None},
    ]
    assert transport.calls[0][0] == f"https://api.hsciq.com/Code/{KEY}/{CODE}"
    assert transport.calls[0][1] == 15
    assert KEY not in json.dumps(result)
    assert KEY.encode() not in store.db.read_bytes()
    assert catalog.latest_snapshot(store, CODE)["snapshot_id"] == result["snapshot_id"]
    assert catalog.get_snapshot(store, result["snapshot_id"])["cache_hit"] is True
    assert catalog.lookup(store, CODE)["snapshot_id"] == result["snapshot_id"]
    assert len(transport.calls) == 1


def test_immutable_same_refresh_updates_checked_timestamp_but_not_version(setup, monkeypatch):
    store, transport = setup
    monkeypatch.setattr(catalog, "now", lambda: "2026-01-01T00:00:00+00:00")
    first = catalog.lookup(store, CODE)
    immutable = deepcopy(store.get(first["snapshot_id"])["payload"])
    monkeypatch.setattr(catalog, "now", lambda: "2026-10-17T02:00:00+00:00")
    second = catalog.lookup(store, CODE, refresh=True)
    assert second["snapshot_id"] == first["snapshot_id"] and second["version"] == 1
    assert second["fetched_at"] == "2026-10-17T02:00:00+00:00"
    assert store.get(first["snapshot_id"])["payload"] == immutable
    assert len(store.list_records("hs_catalog_snapshot")) == 1
    assert second["content_hash"] == first["content_hash"]


def test_content_versions_and_return_to_prior_content_are_new_versions(setup):
    store, transport = setup
    first = catalog.lookup(store, CODE)
    transport.data = detail(Reporting="0:合成新要素:1", Status=2)
    second = catalog.lookup(store, CODE, refresh=True)
    assert second["version"] == 2 and second["previous_id"] == first["snapshot_id"]
    assert second["status"] == "expired" and first["status"] == "active"
    transport.data = detail()
    third = catalog.lookup(store, CODE, refresh=True)
    assert third["version"] == 3 and third["previous_id"] == second["snapshot_id"]
    assert third["content_hash"] == first["content_hash"]


def test_no_key_status_and_existing_cache_manual_access(setup, monkeypatch):
    store, transport = setup
    first = catalog.lookup(store, CODE)
    monkeypatch.delenv("TRADEFLOW_HSCIQ_API_KEY")
    assert catalog.status()["configured"] is False
    assert catalog.lookup(store, CODE)["snapshot_id"] == first["snapshot_id"]
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE, refresh=True)
    assert caught.value.code == "not_configured"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("value", [None, 0, "731815", "73181590900", "73181590.9", "7318159090.0", "abcdefghij", "../1234567"])
def test_invalid_codes_never_request_or_persist(setup, value):
    store, transport = setup
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, value)
    assert caught.value.code == "invalid_input"
    assert transport.calls == [] and store.list_records("hs_catalog_snapshot") == []


@pytest.mark.parametrize("status,expected", [(301, "redirect_blocked"), (302, "redirect_blocked"), (307, "redirect_blocked"), (401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (500, "unavailable")])
def test_http_errors_are_safe_and_not_cached(setup, status, expected):
    store, transport = setup
    transport.status = status
    transport.data = {"error": KEY, "url": "https://api.hsciq.com/private/" + KEY}
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == expected
    assert KEY not in str(caught.value) and "https://" not in str(caught.value)
    assert not store.list_records("hs_catalog_snapshot")
    assert KEY.encode() not in store.db.read_bytes()


@pytest.mark.parametrize("payload,expected", [({"error": "API Key 错误!"}, "authentication_failed"), ("SYNTHETIC API ERROR", "authentication_failed"), (detail(Remark=KEY), "unsafe_response"), (detail(Remark="https://synthetic.invalid/private"), "unsafe_response"), (detail(Reporting="0:品牌:2"), "malformed_response"), (detail(Reporting="0:品牌:1$0:重复:1"), "malformed_response"), (detail(HSCode="0000000001"), "not_found"), (detail(HSCode=0), "malformed_response"), (detail(Name=None), "malformed_response"), ([], "malformed_response"), ({}, "not_found")])
def test_malformed_sensitive_mismatch_or_auth_payload_rejected(setup, payload, expected):
    store, transport = setup
    transport.data = payload
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(store, CODE)
    assert caught.value.code == expected
    assert not store.list_records("hs_catalog_snapshot")
    assert KEY.encode() not in store.db.read_bytes()


def test_secret_unicode_escape_is_detected(setup):
    store, transport = setup
    escaped = json.dumps(detail(Remark=KEY)).replace("hsapi_", "\\u0068sapi_")
    monkeypatch_transport = lambda url, timeout: (200, {}, escaped.encode())
    provider = catalog.HsciqProvider(transport=monkeypatch_transport)
    with pytest.raises(catalog.CatalogError, match="安全校验"):
        provider.detail(CODE)


def test_timeout_network_unexpected_error_never_echo_url_or_key(setup, monkeypatch):
    store, transport = setup
    for error, expected in [(TimeoutError(KEY), "timeout"), (URLError(TimeoutError(KEY)), "timeout"), (URLError(KEY), "unavailable"), (RuntimeError(KEY), "unavailable"), (HTTPError("https://synthetic.invalid/" + KEY, 302, KEY, {}, None), "redirect_blocked")]:
        def fail(url, timeout):
            raise error
        monkeypatch.setattr(catalog, "_provider", lambda: catalog.HsciqProvider(transport=fail))
        with pytest.raises(catalog.CatalogError) as caught:
            catalog.lookup(store, CODE)
        assert caught.value.code == expected and KEY not in str(caught.value)


def test_real_transport_does_not_follow_redirect(monkeypatch):
    handler = catalog._NoRedirect()
    assert handler.redirect_request(None, None, 302, "SYNTHETIC", {}, "http://synthetic.invalid/") is None


def test_search_prefix_and_complete_results(setup):
    store, transport = setup
    transport.data = [detail(Units="千克/个")]
    result = catalog.search(store, "00", page=2)
    assert result["items"][0]["hs_code"] == CODE
    assert result["items"][0]["units"] == "千克/个"
    assert transport.calls[0][0] == f"https://api.hsciq.com/SearchCode/{KEY}/2?keywords=00"
    catalog.search(store, "合成螺钉")
    assert "%E5%90%88" in transport.calls[1][0]
    assert store.list_records("hs_catalog_snapshot") == []
    transport.data = [detail(HSCode="000000")]
    with pytest.raises(catalog.CatalogError):
        catalog.search(store, "00")


@pytest.mark.parametrize("keywords,page", [("0", 1), ("000", 1), ("", 1), ("../", 1), ("https://synthetic.invalid", 1), ("00", 0), ("00", True)])
def test_invalid_search_never_requests(setup, keywords, page):
    store, transport = setup
    with pytest.raises(catalog.CatalogError):
        catalog.search(store, keywords, page)
    assert not transport.calls


def change(changed_at="2099-01-01T00:00:00Z"):
    return {"hsCode": CODE, "field": "Reporting", "changeType": "修改", "dataType": "CNCode", "before": "SYNTHETIC OLD", "after": "SYNTHETIC NEW", "createTime": changed_at}


def test_updates_mark_all_cached_versions_stale_until_successful_detail_check(setup):
    store, transport = setup
    first = catalog.lookup(store, CODE)
    transport.data = detail(Remark="SYNTHETIC NEXT")
    second = catalog.lookup(store, CODE, refresh=True)
    transport.data = {"items": [change()], "totalCount": 2}
    changes = catalog.updates(store, "7d")
    assert changes["complete"] is False and changes["items"][0]["hs_code"] == CODE
    assert catalog.get_snapshot(store, first["snapshot_id"])["stale"] is True
    assert catalog.latest_snapshot(store, CODE)["requires_refresh"] is True
    transport.data = {"error": "SYNTHETIC FAILURE"}
    with pytest.raises(catalog.CatalogError):
        catalog.lookup(store, CODE, refresh=True)
    assert catalog.latest_snapshot(store, CODE)["stale"] is True
    transport.data = detail(Remark="SYNTHETIC NEXT")
    checked = catalog.lookup(store, CODE, refresh=True)
    assert checked["snapshot_id"] == second["snapshot_id"]
    assert checked["requires_refresh"] is False and checked["stale"] is False


def test_past_changes_do_not_invalidate_more_recent_detail_check(setup):
    store, transport = setup
    catalog.lookup(store, CODE)
    transport.data = {"items": [change("2000-01-01T00:00:00Z")], "totalCount": 1}
    catalog.updates(store)
    assert catalog.latest_snapshot(store, CODE)["stale"] is False


def test_unknown_change_time_requires_refresh(setup):
    store, transport = setup
    catalog.lookup(store, CODE)
    transport.data = {"items": [change("")], "totalCount": 1}
    catalog.updates(store)
    assert catalog.latest_snapshot(store, CODE)["requires_refresh"] is True


def test_raw_response_hash_preserved_without_raw_response(setup):
    store, transport = setup
    result = catalog.lookup(store, CODE)
    assert result["response_sha256"] == sha256(json.dumps(transport.data, ensure_ascii=False).encode()).hexdigest()
    assert "raw_response" not in store.get(result["snapshot_id"])["payload"]


def test_unsupported_provider_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEFLOW_HS_PROVIDER", "SYNTHETIC_INVALID_PROVIDER")
    assert catalog.status()["configured"] is False
    assert catalog.status()["provider"] == "unsupported"
    with pytest.raises(catalog.CatalogError) as caught:
        catalog.lookup(Store(tmp_path), CODE)
    assert caught.value.code == "unsupported_provider"
