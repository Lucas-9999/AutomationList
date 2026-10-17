"""SYNTHETIC customs categories and Decimal aggregation; no live records or API."""
from copy import deepcopy
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tradeflow.customs_groups import list_categories, save_category, prepare_groups
from tradeflow.store import Store, ConflictError


def category(**changes):
    return {"name": "SYNTHETIC SMALL SCREW", "hs_code": "73181590.90", "product_name": "SYNTHETIC SCREW",
            "material": "SYNTHETIC STEEL", "grade": "12.9", "diameter_max": "6", "max_inclusive": False,
            "description": "SYNTHETIC SCM435 SMALL SCREW", "change_reason": "SYNTHETIC TEST", **changes}


def item(line="erp-7", **changes):
    return {"line_id": line, "source_row": int(line.split("-")[-1]), "product_name": "SYNTHETIC SCREW",
            "specification": "M1.6×4 (P0.35)", "material": "SYNTHETIC STEEL", "grade": "12.9",
            "surface_color": "SYNTHETIC BLACK", "quantity": "100", "unit": "支", "unit_price_usd": "0.0152",
            "amount_usd": "1.52", "source_cells": {"quantity": {"raw": "100", "cell": "L7", "sheet": "SYNTHETIC"}}, **changes}


def conversion(*items):
    return {"source_sha256": "f" * 64, "source_sheet": "SYNTHETIC", "items": list(items or [item()]),
            "totals": {"total_usd": "4.57", "quantity": "300"}, "exchange_rate": "6.72"}


def req(lines, **changes):
    return {"group_id": "cg-test", "member_line_ids": lines, "fields": {}, **changes}


def test_grouping_across_surface_and_different_specs_preserves_amount_and_all_sources(tmp_path):
    source = conversion(item(), item("erp-8", specification="M2x5", surface_color="SYNTHETIC NICKEL",
                                    quantity="200", amount_usd="3.05"))
    original = deepcopy(source)
    state = prepare_groups(Store(tmp_path), source, [])
    assert source == original
    group, = state["groups"]
    assert group["quantity"] == "300"
    assert group["amount_usd"] == "4.57"
    assert group["unit_price_usd"] == "0.0152"
    assert group["rounding_difference_usd"] == "0.01"
    assert Decimal(group["unit_price_usd_exact"]) > Decimal("0.0152")
    assert group["surface_colors"] == ["SYNTHETIC BLACK", "SYNTHETIC NICKEL"]
    assert group["members"] == source["items"]
    assert group["fields"] == {}
    assert state["conversion"]["source_sha256"] == source["source_sha256"]
    assert state["conversion"]["totals"] == source["totals"]
    grouped, = state["conversion"]["items"]
    assert grouped["source_cells"] == {} and grouped["source_rows"] == [7, 8]
    assert grouped["specification"] == "M1.6×4 (P0.35)；M2x5"
    # Mutating a result cannot alter original ERP evidence.
    group["members"][0]["source_cells"]["quantity"]["raw"] = "SYNTHETIC RESULT ONLY"
    assert source == original


@pytest.mark.parametrize("changes", [{"material": "SYNTHETIC OTHER"}, {"grade": "8.8"},
    {"product_name": "SYNTHETIC NUT"}, {"unit": "千支"}, {"specification": "M6x5"},
    {"specification": "M8x5"}, {"specification": "SYNTHETIC UNKNOWN"}])
def test_distinct_business_facts_or_range_not_merged(tmp_path, changes):
    rows = conversion(item(), item("erp-8", **changes))
    assert len(prepare_groups(Store(tmp_path), rows, [])["groups"]) == 2
    with pytest.raises(ValueError):
        prepare_groups(Store(tmp_path), rows, [req(["erp-7", "erp-8"])])


def test_large_explicit_diameters_separate_unknown_specs_conservative(tmp_path):
    source = conversion(item(specification="M6x10"), item("erp-8", specification="M6.0×20"),
                        item("erp-9", specification="M8x10"), item("erp-10", specification="SYNTHETIC UNKNOWN A"),
                        item("erp-11", specification="SYNTHETIC UNKNOWN B"))
    groups = prepare_groups(Store(tmp_path), source, [])["groups"]
    assert [group["member_line_ids"] for group in groups] == [["erp-7", "erp-8"], ["erp-9"], ["erp-10"], ["erp-11"]]
    assert groups[0]["diameter_label"] == "杆径=6 mm"
    assert any("未安全识别" in warning for warning in groups[-1]["warnings"])


def test_category_reuse_ignores_surface_defaults_without_overwriting_manual_fields(tmp_path):
    store = Store(tmp_path)
    saved = save_category(store, category(catalog_snapshot_id="hs_snapshot_SYNTHETIC", domestic_origin="SYNTHETIC ORIGIN"))
    source = conversion(item(), item("erp-8", specification="M2x5", surface_color="SYNTHETIC NICKEL"))
    state = prepare_groups(store, source, [])
    group, = state["groups"]
    assert group["category_id"] == saved["id"] and group["category_version"] == 1
    assert group["fields"]["hs_code"] == "7318159090" and group["fields"]["legal_unit"] == "KG"
    assert state["catalog_bindings"] == {group["group_id"]: "hs_snapshot_SYNTHETIC"}
    explicit = prepare_groups(store, source, [req(["erp-8", "erp-7"], category_id=saved["id"], fields={"description": "", "domestic_origin": "SYNTHETIC MANUAL"})])
    assert explicit["groups"][0]["fields"]["description"] == ""
    assert explicit["groups"][0]["fields"]["domestic_origin"] == "SYNTHETIC MANUAL"
    assert explicit["groups"][0]["member_line_ids"] == ["erp-7", "erp-8"]


def test_same_hs_distinct_saved_ranges_separate_and_boundary_inclusive(tmp_path):
    store = Store(tmp_path)
    small = save_category(store, category())
    large = save_category(store, category(name="SYNTHETIC LARGE", diameter_min="6", diameter_max="12", min_inclusive=True, max_inclusive=True))
    source = conversion(item(specification="M5.999×5"), item("erp-8", specification="M6x5"), item("erp-9", specification="M12x20"))
    groups = prepare_groups(store, source, [])["groups"]
    assert [group["category_id"] for group in groups] == [small["id"], large["id"]]
    assert groups[1]["member_line_ids"] == ["erp-8", "erp-9"]
    with pytest.raises(ValueError, match="不符"):
        prepare_groups(store, source, [req(["erp-7", "erp-8", "erp-9"], category_id=small["id"])])


def test_overlapping_rules_not_silently_chosen(tmp_path):
    store = Store(tmp_path)
    save_category(store, category())
    save_category(store, category(name="SYNTHETIC OVERLAP", hs_code="7318150000", diameter_max="4"))
    group, = prepare_groups(store, conversion(), [])["groups"]
    assert group["category_id"] is None and not group["fields"].get("hs_code")
    assert any("重叠" in warning for warning in group["warnings"])


def test_stale_categories_report_newest_without_silently_switching(tmp_path):
    store = Store(tmp_path)
    saved = save_category(store, category())
    revised = save_category(store, category(expected_version=1, description="SYNTHETIC REVISION"))
    group, = prepare_groups(store, conversion(), [req(["erp-7"], category_id=saved["id"])])["groups"]
    assert group["category_stale"] and group["newest_category_id"] == revised["id"]
    assert group["category_version"] == 1 and group["fields"]["description"] == saved["description"]
    assert prepare_groups(store, conversion(), [])["groups"][0]["category_id"] == revised["id"]


def test_category_version_chain_idempotency_audit_and_conflict(tmp_path):
    store = Store(tmp_path)
    first = save_category(store, category())
    assert save_category(store, category(hs_code="7318159090")) == first
    second = save_category(store, category(expected_version=1, description="SYNTHETIC CHANGED"))
    assert second["previous_id"] == first["id"] and second["version"] == 2
    assert list_categories(store) == [second]
    assert len(store.list_records("customs_category")) == 2
    with pytest.raises(ConflictError):
        save_category(store, category(expected_version=1, description="SYNTHETIC STALE"))
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit WHERE action='customs_category_saved'").fetchone()[0] == 2


def test_category_concurrent_revisions_only_one_wins(tmp_path):
    store = Store(tmp_path)
    save_category(store, category())
    barrier = Barrier(2)
    def update(index):
        barrier.wait()
        try:
            return save_category(store, category(expected_version=1, description=f"SYNTHETIC {index}"))["version"]
        except ConflictError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(update, [1, 2]))) == ["2", "conflict"]


@pytest.mark.parametrize("changes", [{"diameter_min": None, "diameter_max": None}, {"diameter_max": 6},
    {"diameter_max": "NaN"}, {"diameter_max": "0"}, {"diameter_max": "-1"},
    {"diameter_min": "6", "diameter_max": "4"}, {"diameter_min": "6", "diameter_max": "6", "max_inclusive": False},
    {"min_inclusive": "true"}, {"max_inclusive": 1}, {"expected_version": True}, {"expected_version": -1},
    {"name": ""}, {"material": ""}, {"grade": ""}, {"product_name": ""}, {"hs_code": "7318"},
    {"api_key": "SYNTHETIC NOT A SECRET"}, {"change_reason": ""}, {"catalog_snapshot_id": "../unsafe"}])
def test_reject_invalid_or_unsupported_category_data_without_saving(tmp_path, changes):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        save_category(store, category(**changes))
    assert list_categories(store) == []


@pytest.mark.parametrize("requests", [[req(["erp-7", "erp-7"])], [req(["erp-999"])], [req([])],
    [req(["erp-7"]), req(["erp-7"], group_id="cg-other")], [req(["erp-7"], group_id="../bad")],
    [req(["erp-7"], unknown="SYNTHETIC")]])
def test_reject_duplicate_foreign_empty_or_unsafe_groups(tmp_path, requests):
    with pytest.raises(ValueError):
        prepare_groups(Store(tmp_path), conversion(), requests)


def test_every_source_row_must_have_one_group_and_group_ids_unique(tmp_path):
    store = Store(tmp_path)
    source = conversion(item(), item("erp-8"))
    with pytest.raises(ValueError, match="遗漏"):
        prepare_groups(store, source, [req(["erp-7"])])
    with pytest.raises(ValueError, match="标识"):
        prepare_groups(store, source, [req(["erp-7"]), req(["erp-8"])])
    with pytest.raises(ValueError):
        prepare_groups(store, source, [req(["erp-7"])] * 1001)


@pytest.mark.parametrize("field", ["quantity", "unit_price_usd", "amount_usd", "unit", "specification", "material", "grade", "product_name"])
def test_never_allow_source_fact_overrides_in_group_fields(tmp_path, field):
    with pytest.raises(ValueError, match="禁止覆盖"):
        prepare_groups(Store(tmp_path), conversion(), [req(["erp-7"], fields={field: "SYNTHETIC OVERRIDE"})])


def test_selected_category_cannot_forge_source_or_change_hs(tmp_path):
    store = Store(tmp_path)
    wrong = save_category(store, category(material="SYNTHETIC DIFFERENT"))
    with pytest.raises(ValueError, match="不符"):
        prepare_groups(store, conversion(), [req(["erp-7"], category_id=wrong["id"])])
    correct = save_category(store, category(name="SYNTHETIC CORRECT"))
    with pytest.raises(ValueError, match="HS"):
        prepare_groups(store, conversion(), [req(["erp-7"], category_id=correct["id"], fields={"hs_code": "7318150000"})])


def test_safe_trim_case_normalization_preserves_original_product_text(tmp_path):
    store = Store(tmp_path)
    saved = save_category(store, category(material=" synthetic steel "))
    source = conversion(item(specification="ｍ２×５", material="SYNTHETIC STEEL"))
    group = prepare_groups(store, source, [])["groups"][0]
    assert group["category_id"] == saved["id"]
    assert group["material"] == "SYNTHETIC STEEL" and group["members"][0]["specification"] == "ｍ２×５"


def test_none_is_opt_out_and_explicit_group_order_is_stable(tmp_path):
    store = Store(tmp_path)
    source = conversion(item(), item("erp-8"))
    assert prepare_groups(store, source, None) is None
    auto = prepare_groups(store, source, [])
    group = auto["groups"][0]
    explicit = prepare_groups(store, source, [{"group_id": group["group_id"], "member_line_ids": ["erp-8", "erp-7"], "category_id": None, "fields": {}, "catalog_snapshot_id": None}])
    assert auto == explicit


def test_blank_open_boundary_and_unparseable_zero_diameter_remain_reviewable(tmp_path):
    store = Store(tmp_path)
    saved = save_category(store, category(diameter_min=""))
    assert saved["diameter_min"] is None and not saved["max_inclusive"]
    group = prepare_groups(store, conversion(item(specification="M0x5")), [])["groups"][0]
    assert group["category_id"] is None
    assert any("未安全识别" in warning for warning in group["warnings"])


def test_decimal_addition_avoids_float_loss_and_total_unchanged(tmp_path):
    source = conversion(item(quantity="0.1", amount_usd="0.01"), item("erp-8", quantity="0.2", amount_usd="0.02"))
    group = prepare_groups(Store(tmp_path), source, [])["groups"][0]
    assert group["quantity"] == "0.3" and group["amount_usd"] == "0.03" and group["unit_price_usd"] == "0.1000"
