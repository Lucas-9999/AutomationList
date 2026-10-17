"""Local records with atomic audit history and optimistic workflow updates."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class ConflictError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "raw").mkdir(exist_ok=True)
        self.db = self.root / "tradeflow.sqlite3"
        with self.connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    source_key TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(kind, source_key)
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY, inquiry_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, version INTEGER NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL, action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def connect(self):
        con = sqlite3.connect(self.db, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def save_original(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "raw" / digest
        # Exclusive creation; retries with identical contents reuse the original.
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            pass
        return digest

    @staticmethod
    def _record(row):
        if row is None:
            raise KeyError("记录不存在")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def create_record(self, kind: str, key: str, payload: dict) -> dict:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM records WHERE kind=? AND source_key=?", (kind, key)).fetchone()
            if old:
                result = self._record(old)
                if result["payload"] != payload:
                    raise ConflictError("相同业务标识已有不同数据，请先核对；禁止覆盖原记录")
                return result
            identity = f"{kind}_{uuid4().hex}"
            timestamp = now()
            con.execute("INSERT INTO records VALUES (?,?,?,?,?)", (identity, kind, key, json.dumps(payload, ensure_ascii=False, allow_nan=False), timestamp))
            self._audit(con, identity, "created", {"kind": kind, "source_key": key})
            return self._record(con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone())

    def get(self, identity: str, kind: str | None = None) -> dict:
        with self.connect() as con:
            row = con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone()
        result = self._record(row)
        if kind and result["kind"] != kind:
            raise ValueError("记录类型不匹配")
        return result

    def find(self, kind: str, key: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM records WHERE kind=? AND source_key=?", (kind, key)).fetchone()
        return self._record(row) if row else None

    def allocate(self, order_id: str, shipment_id: str, lines: list, confirmation_ref: str) -> dict:
        """Serialize all allocations to prevent concurrent over-allocation."""
        from .allocations import allocate_batch

        if not isinstance(confirmation_ref, str) or not confirmation_ref.strip():
            raise ValueError("分批分配需要实际出货确认依据 confirmation_ref")
        if not isinstance(lines, list) or not all(isinstance(x, dict) and set(x) == {"order_line_id", "shipment_item_index"} for x in lines):
            raise ValueError("分配明细格式无效")
        # Sorting by a validated integer index also makes equivalent retries
        # independent of request ordering.
        if any(type(x["shipment_item_index"]) is not int for x in lines):
            raise ValueError("shipment_item_index 必须是整数")
        request = {"order_id": order_id, "shipment_id": shipment_id,
                   "lines": sorted(lines, key=lambda x: x["shipment_item_index"]),
                   "confirmation_ref": confirmation_ref.strip()}
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM records WHERE kind='allocation' AND source_key=?", (shipment_id,)).fetchone()
            if old:
                record = self._record(old)
                if any(record["payload"].get(k) != v for k, v in request.items()):
                    raise ConflictError("此 ERP 记录已有不同分配；禁止重复使用或覆盖")
                return record
            order = self._record(con.execute("SELECT * FROM records WHERE id=? AND kind='order'", (order_id,)).fetchone())
            from .revisions import _status_in_transaction
            revision = _status_in_transaction(con, self, order_id)
            if not revision["is_current"]:
                raise ConflictError("订单已有修订版本，不能对旧版本新建出货分配")
            shipment = self._record(con.execute("SELECT * FROM records WHERE id=? AND kind='shipment'", (shipment_id,)).fetchone())
            all_allocations = [self._record(row)["payload"] for row in con.execute("SELECT * FROM records WHERE kind='allocation'")]
            source_hash = shipment["payload"].get("sha256")
            source_sheet = shipment["payload"].get("source", {}).get("sheet")
            if source_hash and any(x.get("source_sha256") == source_hash and x.get("source_sheet") == source_sheet for x in all_allocations):
                raise ConflictError("同一 ERP 原件工作表已分配；改变映射不能再次占用原始出货数量")
            previous = [x for x in all_allocations if x.get("order_id") == order_id]
            result = allocate_batch(order["payload"], shipment["payload"], lines, previous)
            payload = {**result, **request, "source_sha256": source_hash, "source_sheet": source_sheet, "source_type": shipment["payload"].get("source", {}).get("document_type")}
            identity = "allocation_" + uuid4().hex
            con.execute("INSERT INTO records VALUES (?,?,?,?,?)", (identity, "allocation", shipment_id, json.dumps(payload, ensure_ascii=False, allow_nan=False), now()))
            self._audit(con, identity, "batch_allocated", {**request, "source_sha256": payload["source_sha256"], "quantities": result["quantities"]})
            self._audit(con, order_id, "batch_allocated", {"allocation_id": identity, "shipment_id": shipment_id})
            return self._record(con.execute("SELECT * FROM records WHERE id=?", (identity,)).fetchone())

    def list_records(self, kind: str, limit: int = 100) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM records WHERE kind=? ORDER BY created_at DESC LIMIT ?", (kind, limit)).fetchall()
        return [self._record(row) for row in rows]

    def create_workflow(self, inquiry_id: str) -> dict:
        self.get(inquiry_id, "inquiry")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflows WHERE inquiry_id=?", (inquiry_id,)).fetchone()
            if row:
                return self._record(row)
            identity = f"workflow_{uuid4().hex}"
            con.execute("INSERT INTO workflows VALUES (?,?,?,?,?,?)", (identity, inquiry_id, "inquiry_received", 1, "{}", now()))
            self._audit(con, identity, "workflow_created", {"inquiry_id": inquiry_id})
            return self._record(con.execute("SELECT * FROM workflows WHERE id=?", (identity,)).fetchone())

    def workflow(self, identity: str) -> dict:
        with self.connect() as con:
            return self._record(con.execute("SELECT * FROM workflows WHERE id=?", (identity,)).fetchone())

    def update_workflow(self, identity: str, expected_version: int, status: str, payload: dict, evidence: dict) -> dict:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM workflows WHERE id=?", (identity,)).fetchone()
            current = self._record(row)
            if current["version"] != expected_version:
                raise ConflictError("流程已发生变化，请刷新后重试")
            con.execute("UPDATE workflows SET status=?, version=version+1, payload=?, updated_at=? WHERE id=?", (status, json.dumps(payload, ensure_ascii=False, allow_nan=False), now(), identity))
            self._audit(con, identity, "transition", {"from": current["status"], "to": status, "evidence": evidence, "version": expected_version + 1})
            return self._record(con.execute("SELECT * FROM workflows WHERE id=?", (identity,)).fetchone())

    @staticmethod
    def _audit(con, identity: str, action: str, detail: dict):
        con.execute("INSERT INTO audit(entity_id,action,detail,created_at) VALUES (?,?,?,?)", (identity, action, json.dumps(detail, ensure_ascii=False, allow_nan=False), now()))

    def audit(self, identity: str, action: str, detail: dict):
        with self.connect() as con:
            self._audit(con, identity, action, detail)

    def history(self, identity: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM audit WHERE entity_id=? ORDER BY seq", (identity,)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]
