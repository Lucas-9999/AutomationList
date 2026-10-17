"""Immutable, transactionally serialized order revisions with original lineage."""
from __future__ import annotations

from hashlib import sha256
import json
from uuid import uuid4

from .domain import normalize_order
from .store import ConflictError, now

REVISION_FIELDS = frozenset({"order_root_id", "revision_no", "supersedes_order_id", "revision_reason"})
RESERVED_FIELDS = REVISION_FIELDS | {"root_order_id", "latest_order_id", "is_current"}


def _family_in_transaction(con, store, order_id):
    orders = {row["id"]: store._record(row) for row in con.execute("SELECT * FROM records WHERE kind='order'")}
    if order_id not in orders:
        row = con.execute("SELECT kind FROM records WHERE id=?", (order_id,)).fetchone()
        if row is not None:
            raise ValueError("记录类型不匹配：需要订单")
        raise KeyError("订单不存在")
    target = orders[order_id]
    metadata = REVISION_FIELDS & target["payload"].keys()
    root_id = target["payload"].get("order_root_id") if metadata else order_id
    if not isinstance(root_id, str) or root_id not in orders:
        raise ConflictError("订单修订链根记录缺失或无效")
    root = orders[root_id]
    if REVISION_FIELDS & root["payload"].keys():
        raise ConflictError("根订单版本元数据无效")
    family = [root]
    revisions = [record for record in orders.values() if record["payload"].get("order_root_id") == root_id]
    for record in revisions:
        payload = record["payload"]
        if (REVISION_FIELDS - payload.keys() or type(payload.get("revision_no")) is not int
                or payload["revision_no"] < 2
                or not isinstance(payload.get("revision_reason"), str) or not payload["revision_reason"].strip()):
            raise ConflictError("订单修订链元数据不完整或无效")
    revisions.sort(key=lambda record: record["payload"]["revision_no"])
    for record in revisions:
        payload = record["payload"]
        if (payload["revision_no"] != len(family) + 1
                or payload["supersedes_order_id"] != family[-1]["id"]
                or payload.get("order_no") != root["payload"].get("order_no")
                or payload.get("inquiry_id") != root["payload"].get("inquiry_id")):
            raise ConflictError("订单修订链发生分叉、断档或业务关联变化")
        family.append(record)
    if not any(record["id"] == order_id for record in family):
        raise ConflictError("订单未能解析到有效修订链")
    return family


def _status_in_transaction(con, store, order_id: str) -> dict:
    """Read current lineage using the caller's existing SQLite transaction.

    Allocation mutations must call this after BEGIN IMMEDIATE, before insertion,
    so revision creation and batch allocation cannot both win a race.
    """
    family = _family_in_transaction(con, store, order_id)
    index = next(index for index, record in enumerate(family) if record["id"] == order_id)
    return {"root_order_id": family[0]["id"], "latest_order_id": family[-1]["id"],
            "revision_no": index + 1, "is_current": order_id == family[-1]["id"]}


def order_revision_status(store, order_id: str) -> dict:
    """Return a snapshot; mutation callers must use the transactional helper."""
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id 必须为非空字符串")
    with store.connect() as con:
        return _status_in_transaction(con, store, order_id)


def _json(payload):
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError):
        raise ValueError("订单附加字段必须可保存为标准 JSON；不允许非有限数值") from None


def create_order_revision(store, previous_order_id: str, payload: dict, reason: str) -> dict:
    """Create a full immutable successor, or reuse the identical direct retry.

    Root orders remain revision 1 without injected metadata. Input is a complete
    business payload, not a patch. Existing drafts remain historical evidence.
    """
    if not isinstance(previous_order_id, str) or not previous_order_id.strip():
        raise ValueError("previous_order_id 必须为非空字符串")
    if not isinstance(payload, dict) or RESERVED_FIELDS & payload.keys():
        raise ValueError("修订输入必须为完整订单业务对象，不得提供版本元数据")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("修订需要非空 reason，最多 4000 字符")
    reason = reason.strip()
    normalized = normalize_order(payload)
    _json(normalized)
    with store.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        family = _family_in_transaction(con, store, previous_order_id)
        previous_index = next(index for index, record in enumerate(family) if record["id"] == previous_order_id)
        previous = family[previous_index]
        root = family[0]
        if normalized["order_no"] != root["payload"]["order_no"]:
            raise ConflictError("修订不得改变订单号")
        if normalized.get("inquiry_id") != root["payload"].get("inquiry_id"):
            raise ConflictError("修订不得新增、移除或改变原始询价关联")
        if previous_index + 1 < len(family):
            successor = family[previous_index + 1]
            successor_business = {key: value for key, value in successor["payload"].items() if key not in REVISION_FIELDS}
            if successor_business == normalized and successor["payload"]["revision_reason"] == reason:
                return successor
            raise ConflictError("上一版本已不是当前版本；请载入最新订单后修订")
        family_ids = {record["id"] for record in family}
        for row in con.execute("SELECT payload FROM records WHERE kind='allocation'"):
            if json.loads(row["payload"]).get("order_id") in family_ids:
                raise ConflictError("订单家族已有出货分配，需先建立分配冲销流程，暂不能修订")
        revision_no = len(family) + 1
        revised = {**normalized, "order_root_id": root["id"], "revision_no": revision_no,
                   "supersedes_order_id": previous_order_id, "revision_reason": reason}
        serialized = _json(revised)
        source_key = f"order-revision-v1:{root['id']}:{revision_no}"
        if con.execute("SELECT 1 FROM records WHERE kind='order' AND source_key=?", (source_key,)).fetchone():
            raise ConflictError("修订版本业务键冲突，禁止覆盖现有记录")
        identity = "order_" + uuid4().hex
        con.execute("INSERT INTO records VALUES (?,?,?,?,?)", (identity, "order", source_key, serialized, now()))
        detail = {"root_order_id": root["id"], "previous_order_id": previous_order_id,
                  "new_order_id": identity, "revision_no": revision_no, "reason": reason,
                  "previous_payload_sha256": sha256(_json(previous["payload"]).encode("utf-8")).hexdigest(),
                  "new_payload_sha256": sha256(serialized.encode("utf-8")).hexdigest()}
        store._audit(con, previous_order_id, "order_superseded", detail)
        store._audit(con, identity, "order_revision_created", detail)
        return store._record(con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone())
