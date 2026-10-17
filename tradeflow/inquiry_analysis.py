"""Resumable, immutable inquiry analysis; a failed chunk never means success."""
from __future__ import annotations

from copy import deepcopy

from .agent import AgentClient, validate_inquiry_source
from .inquiry_chunks import build_inquiry_plan
from .store import ConflictError
from .inquiry_translation import validate_source_row_result, ROW_TRANSLATION_VERSION


def structure_summary(source):
    structure = source.get("analysis_structure", {})
    rows = structure.get("rows", [])
    return {"version": structure.get("version"), "row_count": len(rows),
            "table_count": len({row["table_id"] for row in rows}),
            "warnings": list(structure.get("warnings", [])), "blockers": list(structure.get("blockers", []))}


def _prepare(inquiry_record, client):
    if not isinstance(inquiry_record, dict) or inquiry_record.get("kind") != "inquiry":
        raise ValueError("分块分析需要已保存的询价记录")
    source = inquiry_record["payload"]
    validate_inquiry_source(source)
    plan = build_inquiry_plan(source)
    fingerprint = client.configuration_fingerprint
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("分析服务配置指纹无效")
    key = f"{inquiry_record['id']}:{fingerprint}:{plan['plan_id']}"
    return source, plan, fingerprint, key


def _check_cached_chunk(record, inquiry_id, plan, fingerprint, chunk):
    payload = record["payload"]
    if (payload.get("inquiry_id") != inquiry_id or payload.get("plan_id") != plan["plan_id"]
            or payload.get("configuration_fingerprint") != fingerprint or payload.get("chunk") != chunk):
        raise ValueError("已保存分块记录与当前原文计划不一致；禁止覆盖")
    AgentClient._validate(payload["result"], chunk["text"])
    if chunk.get("structured_rows"):
        validate_source_row_result(payload["result"], chunk["structured_rows"], chunk["text"])
    return record


def _cached_chunk(store, inquiry_id, plan, fingerprint, key, chunk):
    record = store.find("analysis_chunk", key + ":" + chunk["id"])
    if record is not None:
        return _check_cached_chunk(record, inquiry_id, plan, fingerprint, chunk)
    return None


def get_cached_chunk_ids(store, inquiry_record, client, plan) -> list[str]:
    """Return successfully validated cached chunk IDs in source order."""
    _, current_plan, fingerprint, key = _prepare(inquiry_record, client)
    if plan != current_plan:
        raise ValueError("询价分块计划已变化，请刷新后重试")
    return [chunk["id"] for chunk in plan["chunks"]
            if _cached_chunk(store, inquiry_record["id"], plan, fingerprint, key, chunk) is not None]


def _analyze_chunk(store, inquiry_record, client, source, plan, fingerprint, key, chunk):
    old = _cached_chunk(store, inquiry_record["id"], plan, fingerprint, key, chunk)
    if old is not None:
        return old
    request_source = deepcopy(source)
    request_source["body"] = chunk["text"]
    request_source["structured_rows"] = deepcopy(chunk.get("structured_rows", []))
    request_source["chunk_context"] = {"index": chunk["index"], "count": plan["chunk_count"],
                                       "start": chunk["start"], "end": chunk["end"],
                                       "context_before": chunk["context_before"],
                                       "context_after": chunk["context_after"]}
    if source.get("analysis_structure", {}).get("rows"):
        # Product rows already carry their own complete context. Never present
        # a neighbour's trailing quantity as part of the current request.
        request_source["chunk_context"]["context_before"] = ""
        request_source["chunk_context"]["context_after"] = ""
    result = deepcopy(client.analyze_inquiry(request_source))
    # Enforce evidence boundaries even when an alternate client or test double
    # bypasses AgentClient's validator. Context is never an evidence source.
    AgentClient._validate(result, chunk["text"])
    if chunk.get("structured_rows"):
        validate_source_row_result(result, chunk["structured_rows"], chunk["text"])
    payload = {"inquiry_id": inquiry_record["id"], "plan_id": plan["plan_id"],
               "plan_version": plan["version"], "body_sha256": plan["body_sha256"],
               "configuration_fingerprint": fingerprint, "chunk": deepcopy(chunk), "result": result}
    try:
        return store.create_record("analysis_chunk", key + ":" + chunk["id"], payload)
    except ConflictError:
        # Concurrent requests may finish the same chunk. Reuse the immutable
        # winning record instead of replacing it with a second model response.
        winner = _cached_chunk(store, inquiry_record["id"], plan, fingerprint, key, chunk)
        if winner is None:
            raise
        return winner


def analyze_inquiry_chunk(store, inquiry_record, client, chunk_id, *, plan_id=None) -> dict:
    """Analyze one exact chunk, or return its existing successful record."""
    source, plan, fingerprint, key = _prepare(inquiry_record, client)
    if plan_id is not None and plan_id != plan["plan_id"]:
        raise ValueError("询价分块计划已变化，请刷新后重试")
    chunk = next((item for item in plan["chunks"] if item["id"] == chunk_id), None)
    if chunk is None:
        raise ValueError("询价分块不存在，请刷新分块计划")
    return _analyze_chunk(store, inquiry_record, client, source, plan, fingerprint, key, chunk)


def _occurrences(text, quote, offset):
    result, position = [], text.find(quote)
    while position >= 0:
        result.append({"start": offset + position, "end": offset + position + len(quote)})
        position = text.find(quote, position + 1)
    return result


def _aggregate(source, plan, records):
    results = [record["payload"]["result"] for record in records]
    result = {"technical_chinese": "\n\n".join(item["technical_chinese"] for item in results),
              "requirements": [], "questions": [], "warnings": []}
    sources, requirement_positions = [], {}
    for chunk, item in zip(plan["chunks"], results, strict=True):
        for requirement in item["requirements"]:
            identity = tuple(requirement[field] for field in ("field", "original_quote", "chinese", "value"))
            if identity not in requirement_positions:
                requirement_positions[identity] = len(result["requirements"])
                result["requirements"].append(deepcopy(requirement))
                sources.append({"requirement_index": len(sources), "requirement": deepcopy(requirement), "sources": []})
            index = requirement_positions[identity]
            evidence = {"chunk_id": chunk["id"], "index": chunk["index"],
                        "start": chunk["start"], "end": chunk["end"],
                        "quote_occurrences": _occurrences(chunk["text"], requirement["original_quote"], chunk["start"])}
            if evidence not in sources[index]["sources"]:
                sources[index]["sources"].append(evidence)
        for field in ("questions", "warnings"):
            for value in item[field]:
                if value not in result[field]:
                    result[field].append(value)
    for warning in source.get("warnings", []):
        if warning not in result["warnings"]:
            result["warnings"].append(warning)
    if source.get("attachments") and not any("附件" in warning for warning in result["warnings"]):
        result["warnings"].append("邮件附件尚未分析，不能确认附件中的技术需求")
    AgentClient._validate(result, source["body"])
    return result, sources


def _validate_complete(payload, source):
    AgentClient._validate(payload["result"], source["body"])
    expected = source.get("analysis_structure", {}).get("rows", [])
    saved = [{key: value for key, value in row.items() if key != "description_chinese"}
             for row in payload.get("source_rows", [])]
    if saved != expected:
        raise ValueError("商品结构行与当前原文不一致；结果不接受")
    for row in expected:
        matches = [item for item in payload["result"]["requirements"] if item["field"] == row["id"]]
        if len(matches) != 1:
            raise ValueError("商品翻译行数与原表格不一致；结果不接受")
        validate_source_row_result({"requirements": matches, "technical_chinese": matches[0]["chinese"]}, [row], source["body"])
        translated = matches[0]["chinese"].split("\n原文品名及规格：", 1)[0].removeprefix("品名及技术描述：")
        actual = next(item for item in payload["source_rows"] if item["id"] == row["id"])
        if actual.get("description_chinese") != translated:
            raise ValueError("商品汇总与逐行结果不一致；结果不接受")


def analyze_inquiry_all(store, inquiry_record, client) -> dict:
    """Process missing chunks sequentially; publish only complete analysis."""
    source, plan, fingerprint, key = _prepare(inquiry_record, client)
    old = store.find("analysis", key)
    if old is not None:
        _validate_complete(old["payload"], source)
        return old
    records = [_analyze_chunk(store, inquiry_record, client, source, plan, fingerprint, key, chunk)
               for chunk in plan["chunks"]]
    result, requirement_sources = _aggregate(source, plan, records)
    source_rows = deepcopy(source.get("analysis_structure", {}).get("rows", []))
    for row in source_rows:
        matches = [item for item in result["requirements"] if item["field"] == row["id"]]
        if len(matches) != 1:
            raise ValueError("商品翻译行数与原表格不一致；结果不接受")
        row["description_chinese"] = matches[0]["chinese"].split("\n原文品名及规格：", 1)[0].removeprefix("品名及技术描述：")
    payload = {"inquiry_id": inquiry_record["id"], "configuration_fingerprint": fingerprint,
               "plan_id": plan["plan_id"],
               "plan": {field: plan[field] for field in ("plan_id", "version", "body_sha256", "chunk_count")},
               "analysis_chunk_ids": [record["id"] for record in records],
               "requirement_sources": requirement_sources, "result": result,
               "analysis_version": ROW_TRANSLATION_VERSION,
               "structure_summary": structure_summary(source), "source_rows": source_rows,
               "translation_review": {"status": "requires_business_review", "checks": {
                   "source_rows_complete": len(source_rows),
                   "quantities_from_source_cells": bool(source_rows) and all(row.get("quantity_text") is not None for row in source_rows),
                   "missing_quantity_rows": sum(row.get("quantity_text") is None for row in source_rows),
                   "description_numeric_and_glossary_guards": bool(source_rows)}}}
    _validate_complete(payload, source)
    try:
        return store.create_record("analysis", key, payload)
    except ConflictError:
        winner = store.find("analysis", key)
        if winner is None:
            raise
        _validate_complete(winner["payload"], source)
        return winner
