"""Explicit per-line references to shared HS catalog snapshots, never classification."""
from copy import deepcopy

from .hs_formats import normalize_code


def validate_bindings(request):
    bindings = request.get("hs_catalog_bindings", {})
    if not isinstance(bindings, dict) or len(bindings) > 1000 or any(
        not isinstance(line, str) or not line or len(line) > 160 or
        not isinstance(snapshot, str) or not snapshot or len(snapshot) > 160
        for line, snapshot in bindings.items()
    ):
        raise ValueError("HS目录引用无效，请重新选择查询结果")
    return bindings


def review_bindings(store, request, line_ids):
    from .hs_catalog import get_snapshot, latest_snapshot, status
    bindings = validate_bindings(request)
    if set(bindings) - line_ids:
        raise ValueError("HS目录引用了其他ERP文件的商品行")
    if not bindings:
        return [], []
    current_provider = status()["provider"]
    reviews, context = [], []
    for line, identity in sorted(bindings.items()):
        snapshot = get_snapshot(store, identity)
        code = normalize_code(request.get("item_extras", {}).get(line, {}).get("hs_code"))
        if snapshot["hs_code"] != code:
            raise ValueError("商品HS编码与所选目录记录不一致，请重新应用或取消目录引用")
        current = latest_snapshot(store, code) or snapshot
        provider_changed = snapshot["provider"] != current_provider
        changed = current["snapshot_id"] != identity
        first_unit = current.get("unit1")
        unit_unsupported = bool(first_unit and first_unit not in {"KG", "kg", "千克"})
        blocked = provider_changed or changed or current["status"] == "expired" or current.get("stale", True) or unit_unsupported
        review = {"line_id": line, "hs_code": code, "snapshot_id": identity,
                  "current_snapshot_id": current["snapshot_id"], "changed": changed,
                  "status": current["status"], "stale": current.get("stale", True),
                  "unit_unsupported": unit_unsupported,
                  "provider_changed": provider_changed, "current_provider": current_provider,
                  "blocked": blocked, "provider": snapshot["provider"],
                  "provider_updated_at": snapshot.get("provider_updated_at"),
                  "fetched_at": snapshot["fetched_at"], "name": snapshot["name"],
                  "unit1": snapshot.get("unit1"), "unit2": snapshot.get("unit2"),
                  "reporting_elements": deepcopy(snapshot["reporting_elements"])}
        reviews.append(review)
        # A successful identical refresh does not change the business reference.
        context.append({key: value for key, value in review.items() if key != "fetched_at"})
    return reviews, context
