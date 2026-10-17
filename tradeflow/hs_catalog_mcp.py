"""Read-only HSCIQ MCP adapter (stateless JSON-RPC, JSON or bounded SSE).

Only search_code and get_code_detail are callable. No private consultations,
classification submissions, or implicit transport/credential fallbacks exist.
"""
from hashlib import sha256
import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener
from uuid import uuid4

from .hs_catalog import CatalogError, MAX_RESPONSE_BYTES, _NoRedirect, _code, _response_code, _text

ENDPOINT = "https://www.hsciq.com/mcp/rpc"
ERROR_CODES = {
    "missing_api_key": "authentication_failed", "invalid_api_key": "authentication_failed",
    "api_key_expired": "authentication_failed", "api_key_revoked": "authentication_failed",
    "daily_quota_exceeded": "rate_limited", "daily_guilei_quota_exceeded": "rate_limited",
    "invalid_params": "invalid_input", "forbidden_keyword": "invalid_input",
    "unknown_tool": "unsupported_operation", "country_not_supported": "unsupported_operation",
    "not_found": "not_found", "internal_error": "unavailable",
}


def _https_transport(request, timeout):
    with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read(MAX_RESPONSE_BYTES + 1)


def _field(data, *names, default=None):
    if not isinstance(data, dict):
        raise CatalogError("malformed_response")
    matches = [data[name] for name in names if name in data]
    if matches and any(value != matches[0] for value in matches[1:]):
        raise CatalogError("malformed_response")
    return matches[0] if matches else default


def _json(value):
    try:
        return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError):
        raise CatalogError("malformed_response") from None


def _check_safe(value, key):
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (ValueError, RecursionError):
        raise CatalogError("malformed_response") from None
    if key in rendered or quote(key, safe="") in rendered or re.search(r"https?://|hsapi_|\\+u0068sapi_", rendered, re.I):
        raise CatalogError("unsafe_response")


def _envelope(body, headers, identity):
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise CatalogError("malformed_response")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CatalogError("malformed_response") from None
    content_type = next((str(value).lower() for key, value in headers.items() if key.lower() == "content-type"), "")
    if "text/event-stream" in content_type or text.lstrip().startswith(("data:", "event:", ":")):
        messages, lines = [], []
        # SSE joins consecutive data fields with LF; comments and event metadata
        # are transport framing, not JSON or model instructions.
        for line in text.splitlines() + [""]:
            if line == "":
                if lines:
                    messages.append(_json("\n".join(lines)))
                    lines = []
            elif line.startswith("data:"):
                value = line[5:]
                lines.append(value[1:] if value.startswith(" ") else value)
            if len(messages) > 64:
                raise CatalogError("malformed_response")
    else:
        messages = [_json(text)]
    matching = []
    for message in messages:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise CatalogError("malformed_response")
        if message.get("id") == identity:
            matching.append(message)
    if len(matching) != 1:
        raise CatalogError("malformed_response")
    return matching[0]


def _safe_error(value):
    # Recognize only documented machine codes. Never surface free-form messages.
    texts = []
    if isinstance(value, dict):
        code = value.get("code")
        if code in (-32601,):
            return "unsupported_operation"
        if code in (-32600, -32602):
            return "invalid_input"
        if isinstance(code, str):
            texts.append(code)
        message = value.get("message")
        if isinstance(message, str):
            texts.append(message)
        content = value.get("content")
        if isinstance(content, list):
            texts.extend(row.get("text", "") for row in content if isinstance(row, dict) and row.get("type") == "text" and isinstance(row.get("text"), str))
    for text in texts:
        found = re.match(r"\s*([a-z_]+)(?::|\s|$)", text)
        if found and found[1] in ERROR_CODES:
            return ERROR_CODES[found[1]]
    return "unavailable"


def _status(value):
    if type(value) is bool:
        return "unknown"
    return "active" if value in (1, "1") else "expired" if value in (2, "2", 3, "3") else "unknown"


def _reporting(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 200:
        raise CatalogError("malformed_response")
    result, seen = [], set()
    for row in value:
        order = _field(row, "key", "Key", "order", "Order", "code", "Code")
        if type(order) is int:
            order = str(order)
        if not isinstance(order, str) or not re.fullmatch(r"[0-9]{1,4}", order) or int(order) in seen:
            raise CatalogError("malformed_response")
        order = int(order)
        seen.add(order)
        required = _field(row, "isRequired", "IsRequired", "required", "Required")
        if required is not None and type(required) is not bool:
            raise CatalogError("malformed_response")
        result.append({"order": order, "name": _text(_field(row, "value", "Value", "name", "Name"), limit=300, required=True), "required": required})
    return sorted(result, key=lambda item: item["order"])


class HsciqMcpProvider:
    name = "hsciq_mcp"
    protocol = "mcp"
    capabilities = {"search": True, "detail": True, "updates": False}

    def __init__(self, *, transport=None):
        self._key = os.environ.get("TRADEFLOW_HSCIQ_API_KEY", "").strip()
        self._transport = transport or _https_transport
        self.last_search_has_more = None

    @property
    def configured(self):
        return bool(self._key)

    def _call(self, tool, arguments):
        if tool not in ("search_code", "get_code_detail"):
            raise CatalogError("unsupported_operation")
        if not self.configured:
            raise CatalogError("not_configured")
        if len(self._key) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in self._key):
            raise CatalogError("authentication_failed")
        identity = uuid4().hex
        payload = {"jsonrpc": "2.0", "id": identity, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        try:
            request = Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST", headers={
                "Accept": "application/json, text/event-stream", "Content-Type": "application/json", "X-API-Key": self._key,
                "User-Agent": "Tradeflow-HS-Catalog/2"})
            status, headers, body = self._transport(request, 15)
        except HTTPError as error:
            status, headers, body = error.code, {}, b""
        except (TimeoutError, socket.timeout):
            raise CatalogError("timeout") from None
        except URLError as error:
            raise CatalogError("timeout" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "unavailable") from None
        except Exception:
            raise CatalogError("unavailable") from None
        if 300 <= status < 400:
            raise CatalogError("redirect_blocked")
        if status in (401, 403):
            raise CatalogError("authentication_failed")
        if status == 429:
            raise CatalogError("rate_limited")
        if status == 404:
            raise CatalogError("not_found")
        if status != 200:
            raise CatalogError("unavailable")
        envelope = _envelope(body, headers, identity)
        _check_safe(envelope, self._key)
        if "error" in envelope:
            raise CatalogError(_safe_error(envelope["error"]))
        result = envelope.get("result")
        if not isinstance(result, dict) or type(result.get("isError", False)) is not bool:
            raise CatalogError("malformed_response")
        if result.get("isError"):
            raise CatalogError(_safe_error(result))
        data = result.get("structuredContent")
        if data is None:
            content = result.get("content")
            if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get("type") != "text" or not isinstance(content[0].get("text"), str):
                raise CatalogError("malformed_response")
            data = _json(content[0]["text"])
        if not isinstance(data, dict):
            raise CatalogError("malformed_response")
        _check_safe(data, self._key)
        return data, sha256(body).hexdigest()

    def detail(self, code):
        code = _code(code)
        data, digest = self._call("get_code_detail", {"country": "CN", "code": code})
        if _field(data, "country", "Country") != "CN":
            raise CatalogError("malformed_response")
        if _response_code(_field(data, "code", "Code")) != code:
            raise CatalogError("not_found")
        extensions = _field(data, "extensions", "Extensions", default={})
        if extensions is None:
            extensions = {}
        cn = _field(extensions, "cn", "Cn", "CN", default={})
        if cn is None:
            cn = {}
        normalized = {"hs_code": code, "name": _text(_field(data, "name", "Name"), required=True),
                      "remark": _text(_field(data, "remark", "Remark")), "unit1": _text(_field(data, "unit1", "Unit1"), limit=100),
                      "unit2": _text(_field(data, "unit2", "Unit2"), limit=100),
                      "reporting_elements": _reporting(_field(cn, "reporting", "Reporting")),
                      "status": _status(_field(data, "status", "Status")), "provider_updated_at": _text(_field(data, "updateTime", "UpdateTime"), limit=80), "provider": self.name}
        return normalized, digest

    def search(self, keywords, page):
        data, digest = self._call("search_code", {"country": "CN", "keywords": keywords, "pageIndex": page, "pageSize": 10})
        rows = _field(data, "items", "Items")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise CatalogError("malformed_response")
        next_page = _field(data, "isNextPage", "IsNextPage")
        if next_page is not None and type(next_page) is not bool:
            raise CatalogError("malformed_response")
        self.last_search_has_more = next_page
        items = []
        for row in rows:
            country = _field(row, "country", "Country", default="CN")
            if country != "CN":
                raise CatalogError("malformed_response")
            units = [_text(_field(row, "unit1", "Unit1"), limit=100), _text(_field(row, "unit2", "Unit2"), limit=100)]
            items.append({"hs_code": _response_code(_field(row, "code", "Code")), "name": _text(_field(row, "name", "Name"), required=True),
                          "remark": _text(_field(row, "remark", "Remark")), "units": "/".join(value for value in units if value), "status": _status(_field(row, "status", "Status"))})
        return items, digest

    def updates(self, range):
        raise CatalogError("unsupported_operation")
