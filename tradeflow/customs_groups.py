"""User-confirmed declaration categories and lossless ERP-to-customs grouping.

Categories are local business rules, never automatic tariff classification. Monetary
values are calculated with Decimal and serialized as decimal strings, like fx.py.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP, localcontext
from hashlib import sha256
import json
import re
import unicodedata
from uuid import uuid4

from .fx import _number
from .hs_formats import normalize_code
from .store import ConflictError, now


GROUP_FIELDS = {"hs_code", "description", "legal_quantity", "legal_unit", "origin_country",
                "destination_country", "domestic_origin", "levy_method", "declaration_elements_text"}
CATEGORY_FIELDS = {"name", "hs_code", "product_name", "material", "grade", "diameter_min", "diameter_max",
                   "min_inclusive", "max_inclusive", "description", "declaration_elements_text", "origin_country",
                   "destination_country", "domestic_origin", "levy_method", "legal_unit", "catalog_snapshot_id",
                   "change_reason", "expected_version"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")


def _text(value, label, *, required=False, limit=4000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{label}须为明确文本，长度不超过{limit}")
    return value.strip()


def _key(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def _snapshot(value):
    if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
        raise ValueError("目录快照ID无效")
    return value


def _public(record):
    return {"id": record["id"], **deepcopy(record["payload"])}


def list_categories(store):
    latest = {}
    for record in store.list_records("customs_category", limit=10000):
        key = _key(record["payload"]["name"])
        if key not in latest or record["payload"]["version"] > latest[key]["payload"]["version"]:
            latest[key] = record
    return [_public(latest[key]) for key in sorted(latest)]


def save_category(store, data):
    if not isinstance(data, dict) or set(data) - CATEGORY_FIELDS:
        raise ValueError("申报类别含有不支持的字段")
    expected = data.get("expected_version", 0)
    if type(expected) is not int or expected < 0:
        raise ValueError("申报类别版本无效")
    reason = _text(data.get("change_reason", ""), "保存说明", required=True, limit=1000)
    content = {key: _text(data.get(key, ""), key, required=True, limit=120)
               for key in ("name", "product_name", "material", "grade")}
    content["hs_code"] = normalize_code(data.get("hs_code"))
    for bound in ("diameter_min", "diameter_max"):
        value = data.get(bound)
        content[bound] = None if value is None or value == "" else format(_number(value, bound, positive=True), "f")
    if content["diameter_min"] is None and content["diameter_max"] is None:
        raise ValueError("类别至少需要一个明确杆径边界")
    for flag in ("min_inclusive", "max_inclusive"):
        value = data.get(flag, flag == "min_inclusive")
        if type(value) is not bool:
            raise ValueError("杆径边界包含条件须为布尔值")
        content[flag] = value
    lower, upper = content["diameter_min"], content["diameter_max"]
    if lower is not None and upper is not None and (Decimal(lower) > Decimal(upper) or
            (Decimal(lower) == Decimal(upper) and not (content["min_inclusive"] and content["max_inclusive"]))):
        raise ValueError("杆径范围为空或上下界颠倒")
    for field in GROUP_FIELDS - {"hs_code", "legal_quantity"}:
        content[field] = _text(data.get(field, "KG" if field == "legal_unit" else ""), field)
    content["catalog_snapshot_id"] = _snapshot(data.get("catalog_snapshot_id"))
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = [store._record(row) for row in con.execute("SELECT * FROM records WHERE kind='customs_category'")]
        versions = [row for row in rows if _key(row["payload"]["name"]) == _key(content["name"])]
        previous = max(versions, key=lambda row: row["payload"]["version"]) if versions else None
        if previous and all(previous["payload"][key] == value for key, value in content.items()):
            return _public(previous)
        version = previous["payload"]["version"] if previous else 0
        if expected != version:
            raise ConflictError("申报类别已有新版本，请重新载入后保存")
        identity = "customs_category_" + uuid4().hex
        payload = {**content, "version": version + 1, "previous_id": previous["id"] if previous else None,
                   "change_reason": reason, "hs_code_raw": data["hs_code"], "authority": "user_defined_not_official"}
        con.execute("INSERT INTO records VALUES (?,?,?,?,?)", (identity, "customs_category",
                    _key(content["name"]) + ":" + str(version + 1), json.dumps(payload, ensure_ascii=False, allow_nan=False), now()))
        store._audit(con, identity, "customs_category_saved", {"version": version + 1, "previous_id": payload["previous_id"]})
        return _public(store._record(con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()))


def _diameter(item):
    spec = unicodedata.normalize("NFKC", item.get("specification") or "").strip()
    match = re.match(r"[Mm]\s*([0-9]+(?:\.[0-9]+)?)(?=$|[xX×*＊\s(（])", spec)
    if not match:
        return None
    try:
        return _number(match[1], "规格杆径", positive=True)
    except ValueError:
        # An unfamiliar/invalid specification is evidence to review, never a
        # reason to invent a range or discard the ERP line.
        return None


def _matches(category, item):
    if any(_key(category[field]) != _key(item.get(field)) for field in ("product_name", "material", "grade")):
        return False
    diameter = _diameter(item)
    if diameter is None:
        return False
    for bound, inclusive, lower in (("diameter_min", "min_inclusive", True), ("diameter_max", "max_inclusive", False)):
        value = category.get(bound)
        if value is None:
            continue
        edge = Decimal(value)
        if (lower and diameter < edge) or (not lower and diameter > edge) or (diameter == edge and not category[inclusive]):
            return False
    return True


def _bucket(item):
    diameter = _diameter(item)
    if diameter is None:
        return ("unknown", _key(item.get("specification")))
    return ("under6",) if diameter < 6 else ("exact", format(diameter.normalize(), "f"))


def _bounds(category, member):
    if category:
        return {key: category[key] for key in ("diameter_min", "diameter_max", "min_inclusive", "max_inclusive")}
    bucket = _bucket(member)
    if bucket[0] == "under6":
        return dict(diameter_min=None, diameter_max="6", min_inclusive=False, max_inclusive=False)
    exact = bucket[1] if bucket[0] == "exact" else None
    return dict(diameter_min=exact, diameter_max=exact, min_inclusive=bool(exact), max_inclusive=bool(exact))


def _diameter_label(bounds, member):
    low, high = bounds["diameter_min"], bounds["diameter_max"]
    if low is None and high is None:
        return "杆径未识别：" + (member.get("specification") or "空规格")
    if low == high:
        return "杆径=" + low + " mm"
    return "杆径 " + ((low + (" ≤ " if bounds["min_inclusive"] else " < ")) if low else "") + "d" + (((" ≤ " if bounds["max_inclusive"] else " < ") + high) if high else "") + " mm"


def _fields(values):
    if not isinstance(values, dict) or set(values) - GROUP_FIELDS:
        raise ValueError("类别仅可填写申报资料，禁止覆盖ERP数量、价格或产品事实")
    result = {}
    for key, value in values.items():
        value = _text(value, key)
        if key == "hs_code" and value:
            value = normalize_code(value)
        if key == "legal_quantity" and value:
            value = format(_number(value, "法定数量", positive=True), "f")
        result[key] = value
    return result


def _suggest(items, categories):
    buckets = {}
    for item in items:
        matches = [category for category in categories if _matches(category, item)]
        # Ambiguous rules remain unselected. Never silently choose a tariff code.
        chosen = matches[0] if len(matches) == 1 else None
        signature = (chosen["id"], _key(item.get("unit"))) if chosen else (
            None, *(_key(item.get(field)) for field in ("product_name", "material", "grade", "unit")), _bucket(item))
        buckets.setdefault(signature, {"member_line_ids": [], "category_id": chosen["id"] if chosen else None, "fields": {}})["member_line_ids"].append(item["line_id"])
    return list(buckets.values())


def prepare_groups(store, conversion, requests):
    """Return customs-only grouped conversion; never mutate source or CI/PI data."""
    if requests is None:
        return None
    items = conversion.get("items") if isinstance(conversion, dict) else None
    if not isinstance(items, list) or not items or any(not isinstance(row, dict) for row in items):
        raise ValueError("报关分组缺少ERP明细")
    ids = [row.get("line_id") for row in items]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("ERP行标识无效或重复")
    if not isinstance(requests, list) or len(requests) > 1000:
        raise ValueError("申报类别须为列表，最多1000组")
    categories = list_categories(store)
    latest = {_key(category["name"]): category for category in categories}
    by_id = {row["line_id"]: row for row in items}
    requests = _suggest(items, categories) if not requests else deepcopy(requests)
    if len(requests) > 1000:
        raise ValueError("申报类别最多1000组")
    seen, group_ids, public, extras, catalog_bindings, category_bindings = set(), set(), [], {}, {}, []
    grouped = deepcopy(conversion)
    grouped["items"] = []
    for request in requests:
        if not isinstance(request, dict) or set(request) - {"group_id", "member_line_ids", "category_id", "fields", "catalog_snapshot_id"}:
            raise ValueError("申报类别请求含不支持的字段")
        member_ids = request.get("member_line_ids")
        if not isinstance(member_ids, list) or not member_ids or any(not isinstance(value, str) for value in member_ids):
            raise ValueError("每个申报类别必须包含明确ERP行")
        if len(set(member_ids)) != len(member_ids) or seen.intersection(member_ids) or set(member_ids) - set(by_id):
            raise ValueError("ERP行重复分组或含外来行")
        seen.update(member_ids)
        # Canonical source order makes fingerprints stable when selection order changes.
        members = [deepcopy(row) for row in items if row["line_id"] in set(member_ids)]
        member_ids = [row["line_id"] for row in members]
        group_id = request.get("group_id") or "cg-" + sha256(json.dumps(sorted(member_ids), ensure_ascii=False).encode()).hexdigest()[:16]
        if not isinstance(group_id, str) or not _ID.fullmatch(group_id) or group_id in group_ids:
            raise ValueError("申报类别标识无效或重复")
        group_ids.add(group_id)
        first = members[0]
        if any(any(_key(row.get(field)) != _key(first.get(field)) for field in ("product_name", "material", "grade", "unit")) for row in members):
            raise ValueError("同一申报类别的品名、材质、等级和成交单位必须一致")
        category_id = request.get("category_id")
        category, stale, newest = None, False, None
        if category_id is not None:
            if not isinstance(category_id, str) or not _ID.fullmatch(category_id):
                raise ValueError("申报类别版本ID无效")
            category = _public(store.get(category_id, "customs_category"))
            if any(not _matches(category, row) for row in members):
                raise ValueError("所选类别与ERP品名、材质、等级或杆径范围不符")
            newest = latest[_key(category["name"])]["id"]
            stale = newest != category["id"]
        elif len({_bucket(row) for row in members}) != 1:
            raise ValueError("未选择已保存类别时不能合并不同杆径范围")
        fields = {key: category[key] for key in GROUP_FIELDS - {"legal_quantity"} if category and category.get(key)}
        fields.update(_fields(request.get("fields", {})))
        if category and fields.get("hs_code") and normalize_code(fields["hs_code"]) != category["hs_code"]:
            raise ValueError("所填HS编码与选中类别不同，请修改类别或取消类别选择")
        snapshot = _snapshot(request.get("catalog_snapshot_id", category.get("catalog_snapshot_id") if category else None))
        warnings = []
        ambiguous = [row["line_id"] for row in members if len([cat for cat in categories if _matches(cat, row)]) > 1]
        if not category and ambiguous:
            warnings.append("存在重叠申报类别，请明确选择后再确认：" + "、".join(ambiguous))
        if stale:
            warnings.append("所选申报类别已有新版本，请重新应用最新版本并核对")
        if any(_diameter(row) is None for row in members):
            warnings.append("未安全识别杆径，仅按原规格保守分组，请核对")
        if not fields.get("hs_code"):
            warnings.append("HS编码待明确，系统不自动猜测")
        with localcontext() as context:
            context.prec = 200
            quantity = sum((_number(row.get("quantity"), "ERP数量", positive=True) for row in members), Decimal(0))
            amount = sum((_number(row.get("amount_usd"), "美元金额") for row in members), Decimal(0))
            exact = amount / quantity
            price = exact.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            difference = (amount - quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        bounds = _bounds(category, first)
        surfaces = list(dict.fromkeys(row.get("surface_color") or "" for row in members))
        row = {"group_id": group_id, "line_id": group_id, "name": category["name"] if category else first.get("product_name", ""),
               "product_name": first.get("product_name"), "material": first.get("material"), "grade": first.get("grade"),
               **bounds, "diameter_label": _diameter_label(bounds, first), "surface_colors": surfaces,
               "member_line_ids": member_ids, "members": members, "quantity": format(quantity, "f"), "unit": first.get("unit"),
               "amount_usd": format(amount, ".2f"), "unit_price_usd": format(price, ".4f"), "unit_price_usd_exact": format(exact, "f"),
               "rounding_difference_usd": format(difference, ".2f"), "category_id": category_id,
               "category_version": category["version"] if category else None, "category_stale": stale,
               "newest_category_id": newest, "fields": fields, "catalog_snapshot_id": snapshot, "warnings": warnings}
        public.append(row)
        extras[group_id] = deepcopy(fields)
        if snapshot:
            catalog_bindings[group_id] = snapshot
        if category:
            category_bindings.append({"group_id": group_id, "category_id": category_id, "version": category["version"],
                                      "category_stale": stale, "newest_category_id": newest,
                                      "category_hash": sha256(json.dumps(category, sort_keys=True, ensure_ascii=False).encode()).hexdigest()})
        grouped["items"].append({**deepcopy(row), "grouped": True, "specification": "；".join(dict.fromkeys(member.get("specification") or "" for member in members)),
                                 "surface_color": "；".join(surfaces), "source_row": None,
                                 "source_rows": [member.get("source_row") for member in members], "source_cells": {}})
    if seen != set(by_id):
        raise ValueError("ERP明细必须恰好归属一个申报类别，不可遗漏")
    return {"groups": public, "conversion": grouped, "extras": extras, "catalog_bindings": catalog_bindings, "category_bindings": category_bindings}
