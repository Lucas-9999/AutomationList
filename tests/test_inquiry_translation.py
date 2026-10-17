"""Synthetic source-owned product rows; no external requests."""
from copy import deepcopy
import json

import pytest

from tradeflow.agent import AgentClient
from tradeflow.inquiry_translation import validate_description, validate_source_row_result


@pytest.fixture(autouse=True)
def configuration(monkeypatch):
    monkeypatch.setenv("TRADEFLOW_AGENT_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("TRADEFLOW_AGENT_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("TRADEFLOW_AGENT_MODEL", "SYNTHETIC_MODEL")
    for key in ("TRADEFLOW_AGENT_RESPONSE_FORMAT", "TRADEFLOW_AGENT_THINKING", "TRADEFLOW_AGENT_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)


def source():
    rows = []
    for index, quantity in enumerate(("444", "1258"), 1):
        description = "RONDELLE A-MAGNETIQUE M8"
        reference = "SYNTHETIC-REF-" + str(index)
        text = f"{description}\n{reference}\n{quantity}\n"
        rows.append({"id": "SYNTHETIC-ROW-" + str(index), "table_id": "SYNTHETIC-TABLE", "row_index": index,
                     "text": text, "description_text": description, "quantity_text": quantity, "reference_text": reference,
                     "cells": [{"column_index": 1, "header": "Désignation", "role": "description", "raw_text": description},
                               {"column_index": 2, "header": "Réf.", "role": "reference", "raw_text": reference},
                               {"column_index": 3, "header": "Quantité", "role": "quantity", "raw_text": quantity}]})
    return {"body": "".join(row["text"] for row in rows), "structured_rows": rows, "warnings": [], "attachments": []}


def transport_response(translations):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"translations": translations}, ensure_ascii=False)}}]}


def test_only_descriptions_sent_and_source_quantities_bound_even_reordered_response():
    email, captured = source(), []
    def transport(url, payload, headers, timeout):
        captured.append(payload)
        return transport_response([{"row_id": row["id"], "description_chinese": "非磁性垫圈 M8"}
                                   for row in reversed(email["structured_rows"])])
    result = AgentClient(transport=transport).analyze_inquiry(email)
    request = json.loads(captured[0]["messages"][1]["content"])
    assert request == {"rows": [{"row_id": row["id"], "description": row["description_text"]} for row in email["structured_rows"]]}
    assert "444" not in str(request) and "1258" not in str(request)
    assert [item["value"] for item in result["requirements"]] == ["444", "1258"]
    assert "[Quantité]：444" in result["requirements"][0]["chinese"]
    assert "[Quantité]：1258" in result["requirements"][1]["chinese"]
    validate_source_row_result(result, email["structured_rows"], email["body"])


@pytest.mark.parametrize("translation", ["磁性垫圈 M8", "非磁性垫圈 M10", "非磁性垫圈 M8，数量444", "非磁性垫圈 M8，配套螺栓"])
def test_wrong_negation_numbers_quantity_or_invented_product_relationship_rejected(translation):
    with pytest.raises(ValueError):
        validate_description("RONDELLE A-MAGNETIQUE M8", translation)


@pytest.mark.parametrize("original,chinese", [
    ("Rondelle conique striée M10 22 1,6 XC60", "锥形齿形垫圈 M10 22 1.6 XC60"),
    ("Écrou M10 pas d’insert nylon, pas d’embase", "M10螺母，无尼龙嵌件、无底座"),
    ("Écrou M8 sans frein ni embase", "M8螺母，无防松结构、无法兰"),
])
def test_dimensions_and_negations_preserved(original, chinese):
    validate_description(original, chinese)


@pytest.mark.parametrize("original,chinese", [
    ("A-MAGNETIQUE M8", "磁性垫圈M8（不是非磁性）"),
    ("conique striée M10", "平垫圈M10，非锥形、无齿"),
    ("SCREW M8 without washer", "M8螺钉，配套垫圈"),
])
def test_negated_glossary_words_and_without_cannot_bypass_guards(original, chinese):
    with pytest.raises(ValueError):
        validate_description(original, chinese)


@pytest.mark.parametrize("original,chinese", [
    ("SCREW M8X20", "螺钉M20×8"),
    ("EPDM 1323X938", "EPDM密封件938×1323"),
    ("SCREW SYNTHETIC123AEJ", "螺钉SYNTHETIC123AEK"),
])
def test_dimension_order_and_alphanumeric_identity_preserved(original, chinese):
    with pytest.raises(ValueError):
        validate_description(original, chinese)


@pytest.mark.parametrize("mutate", ["missing", "duplicate", "unknown", "extra_key"])
def test_translation_row_identity_must_be_complete_exact_and_unique(mutate):
    email = source()
    response = [{"row_id": row["id"], "description_chinese": "非磁性垫圈 M8"} for row in email["structured_rows"]]
    if mutate == "missing": response.pop()
    elif mutate == "duplicate": response[1] = deepcopy(response[0])
    elif mutate == "unknown": response[1]["row_id"] = "SYNTHETIC-UNKNOWN"
    else: response[0]["quantity"] = "1258"
    with pytest.raises(ValueError):
        AgentClient(transport=lambda *args: transport_response(response)).analyze_inquiry(email)


def test_persisted_quantity_and_assembled_narrative_cannot_be_swapped():
    email = source()
    response = [{"row_id": row["id"], "description_chinese": "非磁性垫圈 M8"} for row in email["structured_rows"]]
    result = AgentClient(transport=lambda *args: transport_response(response)).analyze_inquiry(email)
    result["requirements"][0]["chinese"] = result["requirements"][0]["chinese"].replace("[Quantité]：444", "[Quantité]：1258")
    with pytest.raises(ValueError, match="归属"):
        validate_source_row_result(result, email["structured_rows"], email["body"])


def test_bad_source_quantity_binding_rejected_before_provider():
    email = source(); email["structured_rows"][0]["quantity_text"] = "1258"
    def forbidden(*args):
        pytest.fail("must not request model for unverified source quantities")
    with pytest.raises(ValueError, match="数量"):
        AgentClient(transport=forbidden).analyze_inquiry(email)


def test_non_stop_response_still_rejected_for_row_mode():
    email = source()
    with pytest.raises(ValueError, match="截断"):
        AgentClient(transport=lambda *args: {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}).analyze_inquiry(email)


def test_missing_quantity_is_explicit_unknown_and_never_invented():
    email = source(); row = email["structured_rows"][0]
    row["cells"] = [cell for cell in row["cells"] if cell["role"] != "quantity"]
    row["quantity_text"] = None
    row["source"] = {"quantity_not_provided": True}
    email["structured_rows"] = [row]
    result = AgentClient(transport=lambda *args: transport_response([{"row_id": row["id"], "description_chinese": "非磁性垫圈 M8"}])).analyze_inquiry(email)
    assert "未提供数量列" in result["technical_chinese"]
    assert result["requirements"][0]["value"] == row["text"]


def test_surrounding_prose_evidence_is_source_owned_and_no_mail_metadata_sent():
    email = {"body": "SYNTHETIC shipping terms please.", "subject": "SYNTHETIC OUTSIDE SUBJECT",
             "analysis_structure": {"rows": [{"id": "SYNTHETIC-TABLE-ROW"}]},
             "chunk_context": {"index": 53}, "warnings": []}
    captured = []
    def transport(url, payload, headers, timeout):
        captured.append(json.loads(payload["messages"][1]["content"]))
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"technical_chinese":"合成：请提供运输条款。"}'}}]}
    result = AgentClient(transport=transport).analyze_inquiry(email)
    assert captured == [{"body": email["body"]}]
    assert result["requirements"][0]["original_quote"] == email["body"]
