"""User-authored HS declaration formats; no automatic tariff classification."""
from copy import deepcopy
from hashlib import sha256
import json
import re
from string import Formatter
from uuid import uuid4

from .store import ConflictError, now

PLACEHOLDERS = {"product_name", "specification", "material", "grade", "surface_color", "standard", "customer_part_no"}


def normalize_code(value):
    from .customs import normalize_hs_code
    return normalize_hs_code(value)


def _validate_format(value, *, required):
    if not isinstance(value, str) or len(value) > 4000 or (required and not value.strip()):
        raise ValueError("申报格式须为明确文本，名称规格格式不能为空，每项不超过4000字")
    try:
        fields = list(Formatter().parse(value))
    except ValueError:
        raise ValueError("申报格式的大括号不完整") from None
    for _, key, spec, conversion in fields:
        if key is not None and (key not in PLACEHOLDERS or spec or conversion):
            raise ValueError("申报格式仅支持已列出的ERP字段占位符，不支持表达式")
    return value


def _public(record):
    return {"id": record["id"], **deepcopy(record["payload"])}


def list_formats(store):
    latest = {}
    for record in store.list_records("hs_format", limit=10000):
        code = record["payload"]["hs_code"]
        if code not in latest or record["payload"]["version"] > latest[code]["payload"]["version"]:
            latest[code] = record
    return [_public(latest[key]) for key in sorted(latest)]


def save_format(store, data):
    code = normalize_code(data.get("hs_code"))
    name = data.get("name", "")
    reason = data.get("change_reason", "")
    expected = data.get("expected_version", 0)
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise ValueError("请填写格式名称和本次保存说明")
    if type(expected) is not int or expected < 0:
        raise ValueError("申报格式版本无效")
    content = {"hs_code": code, "name": name.strip(),
               "description_template": _validate_format(data.get("description_template"), required=True),
               "declaration_elements_template": _validate_format(data.get("declaration_elements_template", ""), required=False)}
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = [store._record(row) for row in con.execute("SELECT * FROM records WHERE kind='hs_format'")]
        versions = [row for row in rows if row["payload"]["hs_code"] == code]
        previous = max(versions, key=lambda row: row["payload"]["version"]) if versions else None
        if previous and all(previous["payload"][key] == value for key, value in content.items()):
            return _public(previous)
        version = previous["payload"]["version"] if previous else 0
        if expected != version:
            raise ConflictError("此HS申报格式已有新版本，请重新载入后保存")
        payload = {**content, "hs_code_raw": data["hs_code"], "version": version + 1,
                   "change_reason": reason.strip(), "authority": "user_defined_not_official", "previous_id": previous["id"] if previous else None}
        identity = "hs_format_" + uuid4().hex
        con.execute("INSERT INTO records VALUES (?,?,?,?,?)", (identity, "hs_format", code + ":" + str(version + 1), json.dumps(payload, ensure_ascii=False, allow_nan=False), now()))
        store._audit(con, identity, "hs_format_saved", {"hs_code": code, "version": version + 1, "previous_id": payload["previous_id"]})
        return _public(store._record(con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()))


def apply_formats(store, conversion, extras):
    """Fill only absent declaration text, preserving every explicitly supplied value."""
    output, bindings = deepcopy(extras), {}
    formats = {row["hs_code"]: row for row in list_formats(store)}
    for row in conversion["items"]:
        values = output.get(row["line_id"], {})
        if not values.get("hs_code"):
            continue
        code = normalize_code(values["hs_code"])
        profile = formats.get(code)
        if not profile:
            continue
        source = {key: row.get(key) or "" for key in PLACEHOLDERS}
        used = {}
        for target, key in (("description", "description_template"), ("declaration_elements_text", "declaration_elements_template")):
            template = profile[key]
            if target in values or not template:
                continue
            needed = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
            if any(not source[name] for name in needed):
                # Missing ERP facts stay missing; never silently remove an element.
                continue
            values[target] = template.format_map(source)
            used[target] = profile["id"]
        if used:
            output[row["line_id"]] = values
            bindings[row["line_id"]] = {"hs_code": code, "format_id": profile["id"], "version": profile["version"], "fields": used,
                                          "template_hash": sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    return output, bindings
