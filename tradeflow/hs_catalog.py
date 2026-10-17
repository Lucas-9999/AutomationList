"""HSCIQ reference catalog, HTTPS-only; classification always stays a user decision.

Provider JSON is untrusted. Only normalized allowlisted fields and response hashes
are retained, never credentials, request URLs, or raw response/error bodies.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener
from uuid import uuid4

from .customs import normalize_hs_code
from .store import now

TTL = timedelta(hours=24)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MESSAGES = {
    "not_configured": "尚未配置 HSCIQ API Key；仍可手动填写 HS 编码。",
    "unsupported_provider": "海关编码服务商配置不受支持。",
    "unsupported_operation": "当前海关编码接口不支持此操作；MCP 模式请按需刷新编码详情。",
    "invalid_input": "海关编码查询参数无效。",
    "authentication_failed": "海关编码服务鉴权失败，请检查本机 API Key 或接口权限。",
    "rate_limited": "海关编码接口调用额度或频率已达限制，请稍后重试。",
    "timeout": "海关编码服务请求超时，请重试。",
    "redirect_blocked": "海关编码服务发生重定向，已停止请求以保护凭证。",
    "unavailable": "海关编码服务暂时不可用，请重试。",
    "malformed_response": "海关编码服务返回格式未通过校验，请使用手动方式并稍后重试。",
    "unsafe_response": "海关编码服务响应未通过安全校验，未保存返回内容。",
    "not_found": "海关编码详情不存在或未返回匹配的十位编码。",
}


class CatalogError(ValueError):
    def __init__(self, code):
        self.code = code
        self.message = MESSAGES.get(code, MESSAGES["unavailable"])
        super().__init__(self.message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _https_transport(url, timeout):
    # The fixed origin and redirect handler prevent credential forwarding.
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "Tradeflow-HS-Catalog/1"})
    with build_opener(_NoRedirect()).open(req, timeout=timeout) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
        return response.status, dict(response.headers), content


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y/%m/%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _text(value, *, limit=4000, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise CatalogError("malformed_response")
    return value.strip()


def _status(value):
    return "active" if value in (1, "1") and type(value) is not bool else "expired" if value in (2, "2") else "unknown"


def _code(value):
    try:
        return normalize_hs_code(value)
    except (TypeError, ValueError):
        raise CatalogError("invalid_input") from None


def _response_code(value):
    try:
        return normalize_hs_code(value)
    except (TypeError, ValueError):
        raise CatalogError("malformed_response") from None


def _reporting(value):
    value = _text(value, limit=16000)
    if not value:
        return []
    elements, seen = [], set()
    for segment in value.split("$"):
        parts = segment.split(":")
        if len(parts) not in (2, 3) or not re.fullmatch(r"[0-9]{1,4}", parts[0]) or (len(parts) == 3 and parts[2] not in ("0", "1")):
            raise CatalogError("malformed_response")
        order = int(parts[0])
        if order in seen:
            raise CatalogError("malformed_response")
        seen.add(order)
        elements.append({"order": order, "name": _text(parts[1], limit=300, required=True), "required": parts[2] == "1" if len(parts) == 3 else None})
    return sorted(elements, key=lambda item: item["order"])


class HsciqProvider:
    name = "hsciq"
    protocol = "tax_rest"
    capabilities = {"search": True, "detail": True, "updates": True}

    def __init__(self, *, transport=None):
        # Credentials are loaded exclusively from the process environment.
        self._key = os.environ.get("TRADEFLOW_HSCIQ_API_KEY", "").strip()
        self._transport = transport or _https_transport

    @property
    def configured(self):
        return bool(self._key)

    def _get(self, path, params=None):
        if not self.configured:
            raise CatalogError("not_configured")
        url = "https://api.hsciq.com/" + path.replace("{key}", quote(self._key, safe=""))
        if params:
            url += "?" + urlencode(params)
        try:
            status_code, headers, body = self._transport(url, 15)
        except HTTPError as error:
            status_code, headers, body = error.code, {}, b""
        except (TimeoutError, socket.timeout):
            raise CatalogError("timeout") from None
        except URLError as error:
            raise CatalogError("timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "unavailable") from None
        except Exception:
            raise CatalogError("unavailable") from None
        if 300 <= status_code < 400:
            raise CatalogError("redirect_blocked")
        if status_code in (401, 403):
            raise CatalogError("authentication_failed")
        if status_code == 429:
            raise CatalogError("rate_limited")
        if status_code != 200:
            raise CatalogError("unavailable")
        if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise CatalogError("malformed_response")
        try:
            data = json.loads(body.decode("utf-8-sig"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise CatalogError("malformed_response") from None
        # Also inspect decoded JSON so escaped credential echoes cannot escape detection.
        rendered = json.dumps(data, ensure_ascii=False)
        if self._key in rendered or quote(self._key, safe="") in rendered or re.search(r"https?://|hsapi_", rendered, re.I):
            raise CatalogError("unsafe_response")
        if isinstance(data, dict) and any(key.casefold() in ("error", "errors", "message") for key in data):
            # Never propagate a provider's free-form error or account information.
            raise CatalogError("authentication_failed")
        if isinstance(data, str):
            raise CatalogError("authentication_failed")
        return data, sha256(body).hexdigest()

    def search(self, keywords, page):
        data, digest = self._get("SearchCode/{key}/" + str(page), {"keywords": keywords})
        if not isinstance(data, list) or len(data) > 1000:
            raise CatalogError("malformed_response")
        items = []
        for row in data:
            if not isinstance(row, dict):
                raise CatalogError("malformed_response")
            items.append({"hs_code": _response_code(row.get("HSCode", row.get("Code"))), "name": _text(row.get("Name"), required=True), "remark": _text(row.get("Remark")), "units": _text(row.get("Units"), limit=300), "status": _status(row.get("Status"))})
        return items, digest

    def detail(self, code):
        data, digest = self._get("Code/{key}/" + code)
        if not isinstance(data, dict):
            raise CatalogError("malformed_response")
        if not data:
            raise CatalogError("not_found")
        result_code = _response_code(data.get("HSCode", data.get("Code")))
        if result_code != code:
            raise CatalogError("not_found")
        normalized = {"hs_code": code, "name": _text(data.get("Name"), required=True), "remark": _text(data.get("Remark")),
                      "unit1": _text(data.get("Unit1"), limit=100), "unit2": _text(data.get("Unit2"), limit=100),
                      "reporting_elements": _reporting(data.get("Reporting")), "status": _status(data.get("Status")),
                      "provider_updated_at": _text(data.get("UpdateTime"), limit=80), "provider": self.name}
        return normalized, digest

    def updates(self, range):
        data, digest = self._get("CodeUpdates/{key}", {"range": range})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list) or len(data["items"]) > 10000 or type(data.get("totalCount")) is not int or data["totalCount"] < len(data["items"]):
            raise CatalogError("malformed_response")
        items = []
        for row in data["items"]:
            if not isinstance(row, dict):
                raise CatalogError("malformed_response")
            item = {"hs_code": _response_code(row.get("hsCode")), "field": _text(row.get("field"), limit=200, required=True), "change_type": _text(row.get("changeType"), limit=30), "data_type": _text(row.get("dataType"), limit=100), "before": _text(row.get("before"), limit=16000), "after": _text(row.get("after"), limit=16000), "changed_at": _text(row.get("createTime"), limit=80)}
            items.append(item)
        return items, data["totalCount"], digest


def _provider():
    if os.environ.get("TRADEFLOW_HS_PROVIDER", "hsciq").strip().lower() != "hsciq":
        raise CatalogError("unsupported_provider")
    protocol = os.environ.get("TRADEFLOW_HSCIQ_PROTOCOL", "tax_rest").strip().lower()
    if protocol == "mcp":
        from .hs_catalog_mcp import HsciqMcpProvider
        return HsciqMcpProvider()
    if protocol != "tax_rest":
        raise CatalogError("unsupported_provider")
    return HsciqProvider()


def status():
    try:
        provider = _provider()
        return {"provider": provider.name, "protocol": provider.protocol, "capabilities": deepcopy(provider.capabilities), "configured": provider.configured, "https_only": True, "note": "按需查询并保留版本；供应商更新时间不代表海关实时同步。未配置时可手动填写。"}
    except CatalogError:
        return {"provider": "unsupported", "protocol": "unsupported", "capabilities": {"search": False, "detail": False, "updates": False}, "configured": False, "https_only": True, "note": MESSAGES["unsupported_provider"]}


def _public(store, record):
    value = deepcopy(record["payload"])
    checked = value["fetched_at"]
    last_checked_seq = -1
    events = store.history(record["id"])
    for event in events:
        if event["action"] == "hs_catalog_checked":
            checked = event["detail"]["fetched_at"]
            last_checked_seq = event["seq"]
    check_time = _timestamp(checked)
    stale = check_time is None or datetime.now(timezone.utc) - check_time > TTL
    requires_refresh = False
    for event in events:
        if event["action"] == "hs_catalog_update_seen" and event["seq"] > last_checked_seq:
            changed = _timestamp(event["detail"].get("changed_at"))
            if changed is None or check_time is None or changed > check_time:
                requires_refresh = True
    value.update(snapshot_id=record["id"], fetched_at=checked, cache_hit=True, stale=stale or requires_refresh, requires_refresh=requires_refresh)
    return value


def get_snapshot(store, identity):
    return _public(store, store.get(identity, "hs_catalog_snapshot"))


def latest_snapshot(store, code):
    code = _code(code)
    provider_name = _provider().name
    with store.connect() as con:
        rows = con.execute("SELECT * FROM records WHERE kind='hs_catalog_snapshot'").fetchall()
    matches = [store._record(row) for row in rows if json.loads(row["payload"]).get("hs_code") == code and json.loads(row["payload"]).get("provider") == provider_name]
    if not matches:
        return None
    return _public(store, max(matches, key=lambda record: record["payload"]["version"]))


def search(store, keywords, page=1):
    if not isinstance(keywords, str) or not keywords.strip() or len(keywords) > 100 or type(page) is not int or not 1 <= page <= 10000:
        raise CatalogError("invalid_input")
    keywords = keywords.strip()
    if keywords.isascii() and keywords.isdigit() and len(keywords) not in (2, 4, 6, 8, 10):
        raise CatalogError("invalid_input")
    if not re.fullmatch(r"[\u3400-\u9fff0-9A-Za-z\s、，,\-（）()]{1,100}", keywords):
        raise CatalogError("invalid_input")
    provider = _provider()
    items, digest = provider.search(keywords, page)
    fetched = now()
    store.audit("hs_catalog", "hs_catalog_search", {"provider": provider.name, "page": page, "count": len(items), "response_sha256": digest, "fetched_at": fetched})
    result = {"provider": provider.name, "items": items, "page": page, "fetched_at": fetched}
    if getattr(provider, "last_search_has_more", None) is not None:
        result["has_more"] = provider.last_search_has_more
    return result


def lookup(store, code, refresh=False):
    code = _code(code)
    cached = latest_snapshot(store, code)
    if cached and not cached["stale"] and not refresh:
        return cached
    provider = _provider()
    try:
        content, digest = provider.detail(code)
    except CatalogError as error:
        store.audit("hs_catalog", "hs_catalog_lookup_failed", {"provider": provider.name, "hs_code": code, "code": error.code})
        raise
    fetched = now()
    content_hash = sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        records = [store._record(row) for row in con.execute("SELECT * FROM records WHERE kind='hs_catalog_snapshot'")]
        matches = [row for row in records if row["payload"]["hs_code"] == code and row["payload"]["provider"] == provider.name]
        latest = max(matches, key=lambda row: row["payload"]["version"]) if matches else None
        # A return to a previous content revision is a new version, not a rollback.
        if latest and latest["payload"]["content_hash"] == content_hash:
            identity = latest["id"]
        else:
            version = latest["payload"]["version"] + 1 if latest else 1
            identity = "hs_catalog_snapshot_" + uuid4().hex
            payload = {**content, "content_hash": content_hash, "response_sha256": digest, "fetched_at": fetched, "version": version, "previous_id": latest["id"] if latest else None, "authority": "provider_reference_not_official_classification"}
            con.execute("INSERT INTO records VALUES (?,?,?,?,?)", (identity, "hs_catalog_snapshot", provider.name + ":" + code + ":" + str(version), json.dumps(payload, ensure_ascii=False, allow_nan=False), fetched))
            store._audit(con, identity, "hs_catalog_snapshot_saved", {"hs_code": code, "version": version, "content_hash": content_hash})
        store._audit(con, identity, "hs_catalog_checked", {"fetched_at": fetched, "response_sha256": digest})
    result = get_snapshot(store, identity)
    result["cache_hit"] = False
    return result


def updates(store, range="30d"):
    if range not in ("7d", "30d", "3m"):
        raise CatalogError("invalid_input")
    provider = _provider()
    items, total, digest = provider.updates(range)
    fetched = now()
    codes = {item["hs_code"] for item in items}
    with store.connect() as con:
        records = [store._record(row) for row in con.execute("SELECT * FROM records WHERE kind='hs_catalog_snapshot'")]
        for record in records:
            if record["payload"]["provider"] != provider.name or record["payload"]["hs_code"] not in codes:
                continue
            for item in items:
                if item["hs_code"] == record["payload"]["hs_code"]:
                    store._audit(con, record["id"], "hs_catalog_update_seen", {"changed_at": item["changed_at"], "fetched_at": fetched})
        store._audit(con, "hs_catalog", "hs_catalog_updates_checked", {"range": range, "fetched_at": fetched, "count": len(items), "total_count": total, "response_sha256": digest})
    return {"provider": provider.name, "items": items, "total_count": total, "fetched_at": fetched, "complete": total == len(items)}
