"""Explicit, source-linked batch allocation; no inferred batch quantities."""
from copy import deepcopy
from decimal import Decimal, localcontext

from .domain import ITEM_FIELDS, _decimal, normalize_order

PACKING_FIELDS = {"net_weight", "gross_weight", "small_box_count", "carton_numbers", "legal_quantity", "package_count", "package_count_text"}


def bind_batch_packaging(batch_order: dict, full_order: dict) -> tuple[dict, list]:
    """A full-order packing fact cannot silently become a partial-batch fact."""
    batch = deepcopy(batch_order)
    source = {row["line_id"]: row for row in full_order["items"]}
    excluded = []
    entire_order = len(batch["items"]) == len(source)
    for row in batch["items"]:
        whole_line = Decimal(row["quantity"]) == Decimal(source[row["line_id"]]["quantity"])
        entire_order = entire_order and whole_line
        if not whole_line:
            for field in sorted(PACKING_FIELDS & row.keys()):
                row.pop(field)
                excluded.append("items." + row["line_id"] + "." + field)
    if not entire_order:
        for field in sorted(PACKING_FIELDS & batch.keys()):
            batch.pop(field)
            excluded.append("fields." + field)
    return batch, excluded


def allocate_batch(order: dict, shipment: dict, lines: list, previous: list) -> dict:
    """Allocate every source row once and enforce the whole order's remaining qty.

    Source item indices are one based. Multiple ERP rows may map to one order
    line, but their identifying fields must agree exactly. The complete immutable
    ERP record is allocated to a single order in this version.
    """
    normalized = normalize_order(order)
    source = shipment.get("items")
    if not isinstance(source, list) or not source or not all(isinstance(x, dict) for x in source):
        raise ValueError("出货明细为空或无效")
    if not isinstance(lines, list) or len(lines) != len(source):
        raise ValueError("分配必须覆盖每一条 ERP 明细，不能省略实际货物")
    order_rows = {x["line_id"]: x for x in normalized["items"]}
    used, quantities, source_refs, canonical_lines = set(), {}, {}, []
    with localcontext() as context:
        context.prec = 1024
        for line in lines:
            if not isinstance(line, dict) or set(line) != {"order_line_id", "shipment_item_index"}:
                raise ValueError("分配行仅包含 order_line_id 和 shipment_item_index")
            identity, index = line["order_line_id"], line["shipment_item_index"]
            if not isinstance(identity, str) or identity not in order_rows:
                raise ValueError("分配引用了不存在的订单行")
            if type(index) is not int or index < 1 or index > len(source) or index in used:
                raise ValueError("ERP 明细索引必须从 1 开始且不能重复")
            used.add(index)
            actual, expected = source[index - 1], order_rows[identity]
            for field in ITEM_FIELDS:
                if not isinstance(actual.get(field), str) or actual[field].strip() != expected[field]:
                    raise ValueError(f"ERP 第 {index} 行 {field} 与订单不一致")
            quantity = _decimal(actual.get("quantity"), "shipment.quantity", positive=True)
            quantities[identity] = quantities.get(identity, Decimal(0)) + quantity
            source_refs.setdefault(identity, []).append({"shipment_item_index": index, "quantity": str(quantity), "source_cells": deepcopy(actual.get("source_cells", actual.get("source", {})))})
            canonical_lines.append({"order_line_id": identity, "shipment_item_index": index})
        allocated = {identity: Decimal(0) for identity in order_rows}
        for prior in previous:
            for row in prior["quantities"]:
                allocated[row["order_line_id"]] += _decimal(row["quantity"], "previous.quantity", positive=True)
        remaining, batch_items = [], []
        for identity, row in order_rows.items():
            quantity = quantities.get(identity, Decimal(0))
            total = allocated[identity] + quantity
            ordered = _decimal(row["quantity"], "order.quantity", positive=True)
            if total > ordered:
                raise ValueError(f"订单行 {identity} 累计分配 {total} 超过订单数量 {ordered}")
            remaining.append({"order_line_id": identity, "ordered": str(ordered), "allocated": str(total), "remaining": str(ordered - total)})
            if quantity:
                item = deepcopy(row)
                item.update(quantity=str(quantity), amount=str(quantity * Decimal(row["unit_price"])), allocation_sources=source_refs[identity])
                batch_items.append(item)
        batch_order = deepcopy(normalized)
        batch_order.pop("total", None)
        batch_order["items"] = batch_items
        batch_order["partial_shipment"] = False
        batch_order = normalize_order(batch_order)
        batch_order, excluded_packing = bind_batch_packaging(batch_order, normalized)
        # A separate reconciled view preserves ERP provenance while attaching the
        # explicitly chosen order IDs. Original ERP payload remains immutable.
        shipment_view = deepcopy(shipment)
        shipment_view.update(items=deepcopy(batch_order["items"]), partial_shipment=False)
        return {
            "lines": sorted(canonical_lines, key=lambda x: x["shipment_item_index"]),
            "quantities": [{"order_line_id": k, "quantity": str(v)} for k, v in quantities.items()],
            "balance_after_allocation": remaining,
            "fully_allocated": all(Decimal(x["remaining"]) == 0 for x in remaining),
            "unbound_order_packing_fields": excluded_packing,
            "batch_order": batch_order, "shipment_view": shipment_view,
        }
