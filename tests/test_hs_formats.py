"""SYNTHETIC user-authored HS formats, isolated versions and safe placeholders."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from tradeflow.api import create_app
from tradeflow.hs_formats import save_format, list_formats, apply_formats
from tradeflow.store import Store, ConflictError


def data(**updates):
    return {"hs_code": "73181590.90", "name": "SYNTHETIC USER FORMAT",
            "description_template": "SYNTHETIC {product_name} {specification} {material} {grade}",
            "declaration_elements_template": "SYNTHETIC {surface_color} {standard}",
            "change_reason": "SYNTHETIC TEST SAVE", "expected_version": 0, **updates}


def conversion():
    return {"items": [{"line_id": "SYNTHETIC-LINE-A", "product_name": "SYNTHETIC SCREW", "specification": "M1.6x4",
                       "material": "SYNTHETIC STEEL", "grade": "12.9", "surface_color": "SYNTHETIC SURFACE", "standard": "SYNTHETIC ISO"},
                      {"line_id": "SYNTHETIC-LINE-B", "product_name": "SYNTHETIC NUT", "specification": "M2",
                       "material": "SYNTHETIC STEEL", "grade": "8", "surface_color": "SYNTHETIC SURFACE", "standard": "SYNTHETIC DIN"}]}


def test_version_chain_idempotency_audit_and_normalized_hs(tmp_path):
    store = Store(tmp_path)
    first = save_format(store, data())
    repeated = save_format(store, data())
    assert repeated == first
    assert first["version"] == 1 and first["previous_id"] is None
    assert first["hs_code"] == "7318159090" and first["hs_code_raw"] == "73181590.90"
    assert first["authority"] == "user_defined_not_official"
    assert save_format(store, data(hs_code="7318159090"))["id"] == first["id"]
    second = save_format(store, data(expected_version=1, description_template="SYNTHETIC REVISED {product_name}", change_reason="SYNTHETIC CHANGE"))
    assert second["version"] == 2 and second["previous_id"] == first["id"]
    assert list_formats(store) == [second]
    assert len(store.list_records("hs_format")) == 2
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit WHERE action='hs_format_saved'").fetchone()[0] == 2
    with pytest.raises(ConflictError):
        save_format(store, data(expected_version=1, description_template="SYNTHETIC STALE EDIT"))


@pytest.mark.parametrize("template", ["{product_name.__class__}", "{product_name[0]}", "{unknown}", "{product_name!r}", "{grade:>20}",
    "{__import__('os').system('echo')}", "{product_name", "}", "{}", "{0}", "{specification:{grade}}"])
def test_expression_or_unsupported_placeholders_never_execute_or_save(tmp_path, template):
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="申报格式"):
        save_format(store, data(description_template=template))
    assert store.list_records("hs_format") == []


def test_literal_braces_and_all_supported_fields_are_plain_text(tmp_path):
    store = Store(tmp_path)
    save_format(store, data(description_template="SYNTHETIC {{literal}} {customer_part_no}"))
    values = conversion()
    values["items"][0]["customer_part_no"] = "SYNTHETIC-PART"
    output, bindings = apply_formats(store, values, {"SYNTHETIC-LINE-A": {"hs_code": "73181590.90"}})
    assert output["SYNTHETIC-LINE-A"]["description"] == "SYNTHETIC {literal} SYNTHETIC-PART"
    assert bindings["SYNTHETIC-LINE-A"]["version"] == 1


def test_missing_erp_placeholder_facts_do_not_silently_make_a_description(tmp_path):
    store = Store(tmp_path)
    save_format(store, data(description_template="SYNTHETIC {customer_part_no}", declaration_elements_template=""))
    extras = {"SYNTHETIC-LINE-A": {"hs_code": "7318159090"}}
    output, bindings = apply_formats(store, conversion(), extras)
    assert output == extras and bindings == {}


def test_manual_description_and_elements_never_overwritten_including_explicit_blank(tmp_path):
    store = Store(tmp_path)
    save_format(store, data())
    extras = {"SYNTHETIC-LINE-A": {"hs_code": "73181590.90", "description": "SYNTHETIC MANUAL DESCRIPTION", "declaration_elements_text": ""}}
    source = conversion()
    before = deepcopy((source, extras))
    output, bindings = apply_formats(store, source, extras)
    assert output == extras and bindings == {}
    assert (source, extras) == before


def test_hs_profiles_are_independent_and_apply_only_matching_code(tmp_path):
    store = Store(tmp_path)
    first = save_format(store, data())
    other = save_format(store, data(hs_code="73181600.00", name="SYNTHETIC NUT", description_template="SYNTHETIC NUT FORMAT {specification}"))
    extras = {"SYNTHETIC-LINE-A": {"hs_code": "73181590.90"}, "SYNTHETIC-LINE-B": {"hs_code": "7318160000"}}
    output, bindings = apply_formats(store, conversion(), extras)
    assert output["SYNTHETIC-LINE-A"]["description"].startswith("SYNTHETIC SYNTHETIC SCREW")
    assert output["SYNTHETIC-LINE-B"]["description"] == "SYNTHETIC NUT FORMAT M2"
    assert bindings["SYNTHETIC-LINE-A"]["format_id"] == first["id"]
    assert bindings["SYNTHETIC-LINE-B"]["format_id"] == other["id"]
    save_format(store, data(expected_version=1, description_template="SYNTHETIC SCREW REVISED {specification}"))
    after, after_bindings = apply_formats(store, conversion(), extras)
    assert after["SYNTHETIC-LINE-B"] == output["SYNTHETIC-LINE-B"]
    assert after_bindings["SYNTHETIC-LINE-B"] == bindings["SYNTHETIC-LINE-B"]


def test_unknown_hs_does_not_borrow_another_formats_description(tmp_path):
    store = Store(tmp_path)
    save_format(store, data())
    extras = {"SYNTHETIC-LINE-A": {"hs_code": "0000000000"}}
    assert apply_formats(store, conversion(), extras) == (extras, {})


def test_concurrent_different_edits_have_one_winner_one_version_conflict(tmp_path):
    store = Store(tmp_path)
    first = save_format(store, data())
    barrier = Barrier(2)

    def edit(label):
        barrier.wait(timeout=10)
        try:
            return save_format(store, data(expected_version=1, description_template="SYNTHETIC CONCURRENT " + label))
        except ConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, ["A", "B"]))
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    latest = list_formats(store)[0]
    assert latest["version"] == 2 and latest["previous_id"] == first["id"]
    assert len(store.list_records("hs_format")) == 2


def test_concurrent_identical_save_is_idempotent(tmp_path):
    store = Store(tmp_path)
    barrier = Barrier(2)

    def save(_):
        barrier.wait(timeout=10)
        return save_format(store, data())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, [1, 2]))
    assert results[0]["id"] == results[1]["id"]
    assert len(store.list_records("hs_format")) == 1


def test_http_list_save_revise_and_conflict_are_isolated(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/customs/hs-formats").json() == []
        first = client.post("/customs/hs-formats", json=data())
        assert first.status_code == 200, first.text
        assert first.json()["version"] == 1
        assert client.post("/customs/hs-formats", json=data()).json()["id"] == first.json()["id"]
        updated = client.post("/customs/hs-formats", json=data(expected_version=1, description_template="SYNTHETIC REVISED {specification}"))
        assert updated.status_code == 200 and updated.json()["version"] == 2
        stale = client.post("/customs/hs-formats", json=data(expected_version=1, description_template="SYNTHETIC STALE"))
        assert stale.status_code == 409
        invalid = client.post("/customs/hs-formats", json=data(expected_version=2, description_template="{product_name.__class__}"))
        assert invalid.status_code == 422
        assert client.get("/customs/hs-formats").json() == [updated.json()]
