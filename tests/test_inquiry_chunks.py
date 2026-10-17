"""Synthetic text only: lossless chunk planning and explicit failure boundaries."""
import copy
import hashlib

import pytest

from tradeflow.inquiry_chunks import build_inquiry_plan


def assert_coverage(body, plan, limit):
    assert "".join(chunk["text"] for chunk in plan["chunks"]) == body
    assert plan["body_characters"] == len(body)
    assert plan["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert plan["chunk_count"] == len(plan["chunks"])
    position = 0
    for index, chunk in enumerate(plan["chunks"], 1):
        assert chunk["index"] == index
        assert chunk["start"] == position
        assert chunk["text"] == body[chunk["start"]:chunk["end"]]
        assert chunk["characters"] == chunk["end"] - chunk["start"] <= limit
        assert chunk["text"].strip()
        position = chunk["end"]
    assert position == len(body)


@pytest.mark.parametrize("body", [
    "Synthetic RFQ\r\n\r\nM1.6×4(P0.35) 300000 PCS\r\nDo not substitute stainless steel.\r\n\r\nRegards\t Synthetic",
    "合成询价。 材料 SCM435； 不可替换不锈钢。\n블랙 아연 12.9\nVis M2.5×7 quantité 200000 pièces.",
    "[Synthetic!A1] M1.6×4\t[Synthetic!B1] 290000\n[Synthetic!A2] M1.6×4\t[Synthetic!B2] 10000",
    "  synthetic\t\ttext\n\n\nnext\r\n\r\nlast    ",
    "Long synthetic paragraph " * 20 + "no-trailing-whitespace",
    "synthetic words " * 10 + "😀多语言終わり",
])
def test_multilingual_lines_tables_and_whitespace_have_exact_coverage(body):
    plan = build_inquiry_plan({"body": body}, max_chars=60)
    assert_coverage(body, plan, 60)


def test_paragraph_then_line_boundary_preference():
    body = "Synthetic heading\n\nfirst line\nsecond line and more words final extra synthetic words"
    plan = build_inquiry_plan({"body": body}, max_chars=48)
    assert plan["chunks"][0]["text"] == "Synthetic heading\n\n"
    assert plan["chunks"][1]["text"] == "first line\n"
    assert_coverage(body, plan, 48)


def test_sentence_boundary_precedes_word_and_decimal_is_not_a_boundary():
    body = "Synthetic M1.6×4 needs 300000 PCS. Do not substitute stainless steel. Final text."
    plan = build_inquiry_plan({"body": body}, max_chars=56)
    assert plan["chunks"][0]["text"] == "Synthetic M1.6×4 needs 300000 PCS. "
    assert plan["chunks"][1]["text"] == "Do not substitute stainless steel. Final text."
    assert_coverage(body, plan, 56)


def test_context_offsets_and_original_source_are_read_only():
    body = "synthetic\n\nM1.6×4 290000 PCS\nDo not substitute stainless steel.\nfinal"
    inquiry = {"body": body, "sha256": "f" * 64,
               "source_type": "inquiry_document", "source": {"cells": []}}
    before = copy.deepcopy(inquiry)
    plan = build_inquiry_plan(inquiry, max_chars=36, context_chars=12)
    assert inquiry == before
    assert plan["source_sha256"] == "f" * 64
    for chunk in plan["chunks"]:
        assert chunk["context_before"] == body[max(0, chunk["start"] - 12):chunk["start"]]
        assert chunk["context_after"] == body[chunk["end"]:chunk["end"] + 12]
    assert_coverage(body, plan, 36)


def test_model_quantity_and_negation_not_lost_at_boundary():
    body = "Synthetic item M1.6×4(P0.35) 300000 PCS. Do not substitute stainless steel. Packing 10 cartons."
    plan = build_inquiry_plan({"body": body}, max_chars=48)
    texts = [chunk["text"] for chunk in plan["chunks"]]
    for token in ("M1.6×4(P0.35)", "300000", "Do not substitute stainless steel."):
        assert any(token in text for text in texts)
    assert_coverage(body, plan, 48)


def test_stable_identity_ignores_filename_and_changes_with_plan_parameters():
    body = "Synthetic RFQ text " * 10
    inquiry = {"body": body, "subject": "one.xlsx", "sha256": "a" * 64}
    plan = build_inquiry_plan(inquiry, max_chars=40)
    repeat = build_inquiry_plan({**inquiry, "subject": "renamed.docx", "sha256": "b" * 64}, max_chars=40)
    assert plan["plan_id"] == repeat["plan_id"]
    assert plan["chunks"] == repeat["chunks"]
    assert plan["plan_id"] != build_inquiry_plan(inquiry, max_chars=50)["plan_id"]
    assert plan["plan_id"] != build_inquiry_plan(inquiry, max_chars=40, context_chars=0)["plan_id"]
    assert plan["plan_id"] != build_inquiry_plan({"body": body + "!"}, max_chars=40)["plan_id"]


def test_trailing_whitespace_follows_content_and_never_creates_empty_block():
    body = "one two     "
    plan = build_inquiry_plan({"body": body}, max_chars=8)
    assert [chunk["text"] for chunk in plan["chunks"]] == ["one ", "two     "]
    assert_coverage(body, plan, 8)


def test_long_internal_separator_can_cross_boundary_without_blank_chunk():
    body = "aa" + " " * 100 + "bb"
    plan = build_inquiry_plan({"body": body}, max_chars=100)
    assert plan["chunk_count"] == 2
    assert_coverage(body, plan, 100)


def test_crlf_is_never_split_between_chunks():
    body = "Synthetic\r\n\r\nmore\r\nlast"
    plan = build_inquiry_plan({"body": body}, max_chars=11)
    assert_coverage(body, plan, 11)
    for chunk in plan["chunks"][:-1]:
        assert not (body[chunk["end"] - 1] == "\r" and body[chunk["end"]] == "\n")


@pytest.mark.parametrize("body,limit", [("M1.6×4(P0.35)" * 10, 40), ("one" + " " * 80, 20)])
def test_overlong_unbreakable_token_or_whitespace_rejects_whole_plan(body, limit):
    with pytest.raises(ValueError, match="分块|空白"):
        build_inquiry_plan({"body": body}, max_chars=limit)


@pytest.mark.parametrize("inquiry", [{}, {"body": " \r\n\t"}, {"body": 17}, None])
def test_invalid_or_blank_source_rejected(inquiry):
    with pytest.raises(ValueError):
        build_inquiry_plan(inquiry)


@pytest.mark.parametrize("arguments", [{"max_chars": 0}, {"max_chars": True}, {"max_chars": 1.5},
                                       {"context_chars": -1}, {"context_chars": True}])
def test_parameter_validation(arguments):
    with pytest.raises(ValueError):
        build_inquiry_plan({"body": "Synthetic"}, **arguments)


def test_explicit_whole_body_and_chunk_count_limits():
    with pytest.raises(ValueError, match="180000"):
        build_inquiry_plan({"body": "x " * 90001})
    with pytest.raises(ValueError, match="1000"):
        build_inquiry_plan({"body": "x " * 1001}, max_chars=2)


def test_single_chunk_and_zero_context():
    body = "Synthetic M2.5×7 200000 PCS"
    plan = build_inquiry_plan({"body": body}, context_chars=0)
    assert plan["source_sha256"] is None
    assert plan["chunk_count"] == 1
    assert plan["chunks"][0]["context_before"] == plan["chunks"][0]["context_after"] == ""
    assert_coverage(body, plan, 600)


def test_default_uses_conservative_six_hundred_character_limit():
    body = "Synthetic M1.6×4 300000 PCS. " * 40
    plan = build_inquiry_plan({"body": body})
    explicit = build_inquiry_plan({"body": body}, max_chars=600)
    assert plan == explicit
    assert_coverage(body, plan, 600)


def synthetic_row(body, text, *, identity="row-1", row_index=1, start=None):
    start = body.index(text) if start is None else start
    return {"id": identity, "table_id": "synthetic-table-1", "row_index": row_index,
            "start": start, "end": start + len(text), "text": text,
            "cells": [{"index": 1, "text": text}], "description_text": text,
            "quantity_text": " 300000 ", "reference_text": "SYNTHETIC-REF",
            "source": {"kind": "synthetic", "table_index": 1, "row_index": row_index}}


def test_complete_structured_rows_never_mix_with_prose_or_split_tokens():
    first = "Synthetic M1.6×4(P0.35)\tDo not substitute stainless steel.\t300000 PCS"
    second = "Synthetic M2.5×7\tSCM435\t200000 PCS"
    body = "Synthetic introduction.\n\n" + first + "\r\n \t\r\n" + second + "\nSynthetic footer."
    rows = [synthetic_row(body, first), synthetic_row(body, second, identity="row-2", row_index=2)]
    inquiry = {"body": body, "analysis_structure": {"rows": rows, "blockers": []}}
    previous = copy.deepcopy(inquiry)
    plan = build_inquiry_plan(inquiry, max_chars=25)
    assert plan["version"] == "inquiry-chunks-v2"
    assert inquiry == previous
    assert_coverage(body, plan, 4000)
    seen = []
    for chunk in plan["chunks"]:
        if chunk["structured_rows"]:
            assert len(chunk["structured_rows"]) == 1
            row = chunk["structured_rows"][0]
            assert row["text"] in chunk["text"]
            assert not chunk["text"].replace(row["text"], "", 1).strip()
            assert "introduction" not in chunk["text"] and "footer" not in chunk["text"]
            seen.append(row)
        else:
            assert chunk["characters"] <= 25
        for row in rows:
            assert not row["start"] < chunk["end"] < row["end"]
    assert seen == rows
    plan["chunks"][1]["structured_rows"][0]["quantity_text"] = "changed"
    assert inquiry == previous  # Return values never alias the stored source.


@pytest.mark.parametrize("length", [601, 3999, 4000])
def test_long_complete_row_is_an_independent_block_up_to_hard_limit(length):
    body = "S" * length
    row = synthetic_row(body, body)
    plan = build_inquiry_plan({"body": body, "analysis_structure": {"rows": [row]}})
    assert plan["chunk_count"] == 1
    assert plan["chunks"][0]["structured_rows"] == [row]
    assert_coverage(body, plan, 4000)


def test_row_over_hard_limit_is_blocked_without_fallback_to_text():
    body = "Synthetic item " * 300
    with pytest.raises(ValueError, match="4000"):
        build_inquiry_plan({"body": body, "analysis_structure": {"rows": [synthetic_row(body, body)]}})


def test_structure_metadata_change_invalidates_plan_and_chunk_cache_identity():
    body = "Synthetic item\t300000 PCS"
    row = synthetic_row(body, body)
    inquiry = {"body": body, "analysis_structure": {"rows": [row]}}
    first = build_inquiry_plan(inquiry)
    inquiry["analysis_structure"]["rows"][0]["source"]["row_index"] = 2
    second = build_inquiry_plan(inquiry)
    assert first["body_sha256"] == second["body_sha256"]
    assert first["plan_id"] != second["plan_id"]
    assert first["chunks"][0]["id"] != second["chunks"][0]["id"]


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(start=-1),
    lambda row: row.update(end=1000),
    lambda row: row.update(start=True),
    lambda row: row.update(text="Different synthetic text"),
    lambda row: row.update(row_index=0),
    lambda row: row.update(table_id=3),
    lambda row: row.pop("source"),
    lambda row: row.update(cells={}),
    lambda row: row.update(source=[]),
    lambda row: row.update(quantity_text=300000),
])
def test_invalid_structured_spans_and_contract_fail_closed(mutation):
    body = "Synthetic item\t300000 PCS"
    row = synthetic_row(body, body)
    mutation(row)
    with pytest.raises(ValueError, match="商品|表格"):
        build_inquiry_plan({"body": body, "analysis_structure": {"rows": [row]}})


@pytest.mark.parametrize("case", ["overlap", "unsorted", "duplicate"])
def test_rows_must_be_sorted_nonoverlapping_and_have_unique_ids(case):
    body = "Synthetic row one\nSynthetic row two"
    first = synthetic_row(body, "Synthetic row one")
    second = synthetic_row(body, "Synthetic row two", identity="row-2", row_index=2)
    if case == "overlap":
        second.update(start=first["start"], end=first["end"], text=first["text"])
    elif case == "unsorted":
        first, second = second, first
    else:
        second["id"] = first["id"]
    with pytest.raises(ValueError, match="商品"):
        build_inquiry_plan({"body": body, "analysis_structure": {"rows": [first, second]}})


@pytest.mark.parametrize("structure", [None, [], {"rows": "invalid"},
                                        {"rows": [], "blockers": ["synthetic merged cell ambiguity"]}])
def test_malformed_structure_or_blockers_cannot_silently_fall_back(structure):
    with pytest.raises(ValueError, match="结构"):
        build_inquiry_plan({"body": "Synthetic RFQ", "analysis_structure": structure})


def test_empty_structure_uses_same_plain_text_boundaries():
    body = "Synthetic paragraph. " * 50
    plain = build_inquiry_plan({"body": body})
    empty = build_inquiry_plan({"body": body, "analysis_structure": {"rows": [], "blockers": []}})
    assert plain == empty
    assert all(chunk["structured_rows"] == [] for chunk in plain["chunks"])


def test_structured_rows_and_leading_trailing_whitespace_remain_covered():
    body = " \nSynthetic one\r\n\r\nSynthetic two\t "
    rows = [synthetic_row(body, "Synthetic one"),
            synthetic_row(body, "Synthetic two", identity="row-2", row_index=2)]
    plan = build_inquiry_plan({"body": body, "analysis_structure": {"rows": rows}})
    assert_coverage(body, plan, 4000)
    assert plan["chunks"][0]["text"].startswith(" \n")
    assert plan["chunks"][-1]["text"].endswith("\t ")


def test_explicit_missing_quantity_and_reference_are_preserved_as_unknown():
    body = "Synthetic M1.6×4(P0.35)\tSCM435"
    row = synthetic_row(body, body)
    row.update(quantity_text=None, reference_text=None)
    row["source"]["quantity_not_provided"] = True
    plan = build_inquiry_plan({"body": body, "analysis_structure": {"rows": [row]}})
    assert plan["chunks"][0]["structured_rows"] == [row]
    assert plan["chunks"][0]["structured_rows"][0]["quantity_text"] is None
    assert_coverage(body, plan, 600)


@pytest.mark.parametrize("marker,cells", [
    (None, []), (False, []), ("true", []),
    (True, [{"role": "quantity", "raw_text": ""}]),
    (True, [{"role": "quantity", "raw_text": "300000"}]),
])
def test_unknown_quantity_requires_exact_source_marker_and_no_quantity_cell(marker, cells):
    body = "Synthetic M1.6×4"
    row = synthetic_row(body, body)
    row.update(quantity_text=None, cells=cells)
    if marker is not None:
        row["source"]["quantity_not_provided"] = marker
    with pytest.raises(ValueError, match="数量"):
        build_inquiry_plan({"body": body, "analysis_structure": {"rows": [row]}})


@pytest.mark.parametrize("description", [None, "", " \t ", 17])
def test_structured_description_cannot_be_unknown_or_blank(description):
    body = "Synthetic M1.6×4"
    row = synthetic_row(body, body)
    row["description_text"] = description
    with pytest.raises(ValueError, match="说明"):
        build_inquiry_plan({"body": body, "analysis_structure": {"rows": [row]}})


def test_existing_known_quantity_plan_and_chunk_ids_remain_unchanged():
    body = "Synthetic item\t300000 PCS"
    row = synthetic_row(body, body)
    plan = build_inquiry_plan({"body": body, "analysis_structure": {"rows": [row]}})
    assert plan["version"] == "inquiry-chunks-v2"
    assert plan["plan_id"] == "plan_437d2124e6a6546bee099995741d9d9e2560cfbffd6819b681a9bc2a6d920640"
    assert plan["chunks"][0]["id"] == "chunk_4cb69e0a1d63bf4632b2089faefaac479b1873f5cfc93d0f57f36ada1f3f4eeb"
