"""Deterministic, lossless inquiry text boundaries; never calls an external API."""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import hashlib
import json
import re


PLAN_VERSION = "inquiry-chunks-v2"
MAX_BODY_CHARACTERS = 180000
MAX_CHUNKS = 1000
MAX_STRUCTURED_ROW_CHARACTERS = 4000
_SENTENCE_ENDINGS = frozenset(".!?;。！？；")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _boundaries(body: str) -> list[list[int]]:
    """Prefer paragraphs, lines, sentence endings, then other whitespace.

    Only whitespace boundaries are used, so punctuation inside a model number,
    decimal, URL or other non-whitespace token cannot become a split point.
    Usually the whole separator belongs to the preceding block. An internal
    whitespace boundary is also safe if the whole separator would overflow it.
    """
    grouped: list[set[int]] = [set(), set(), set(), set()]
    for match in re.finditer(r"\s+", body):
        start, end = match.span()
        separator = match.group()
        newlines = separator.count("\n") + separator.count("\r") - separator.count("\r\n")
        if newlines >= 2:
            rank = 0
        elif newlines:
            rank = 1
        elif start and body[start - 1] in _SENTENCE_ENDINGS:
            rank = 2
        else:
            rank = 3
        grouped[rank].add(end)
        grouped[3].add(start)
        # Long separator runs may span a boundary, without creating a blank
        # block. Treat CRLF as an indivisible separator pair.
        grouped[3].update(position for position in range(start + 1, end)
                          if not (body[position - 1] == "\r" and body[position] == "\n"))
    return [sorted(group) for group in grouped]


def _plain_offsets(body: str, max_chars: int) -> list[tuple[int, int]]:
    candidates = _boundaries(body)
    last_content = len(body.rstrip())
    offsets: list[tuple[int, int]] = []
    start = 0
    while start < len(body):
        if len(offsets) >= MAX_CHUNKS:
            raise ValueError(f"询价分块超过 {MAX_CHUNKS} 块上限；整件未建立计划，不会截断原文")
        limit = min(start + max_chars, len(body))
        if limit == len(body):
            end = limit
        else:
            end = None
            # Keep a final whitespace suffix with a content-bearing block.
            upper = min(limit, last_content - 1)
            for group in candidates:
                position = bisect_right(group, upper) - 1
                if position >= 0:
                    candidate = group[position]
                    if candidate > start and body[start:candidate].strip():
                        end = candidate
                        break
            if end is None:
                raise ValueError("询价包含超过分块上限的连续型号、文本或空白段，无法在不拆开原文 token 的前提下分块；请提高上限或提供明确分隔的原文")
        if not body[start:end].strip():
            raise ValueError("询价空白段无法与正文在分块上限内完整保留；整件未建立计划")
        offsets.append((start, end))
        start = end

    return offsets


def _structure_rows(inquiry: dict, body: str) -> list[dict]:
    if "analysis_structure" not in inquiry:
        return []
    structure = inquiry["analysis_structure"]
    if not isinstance(structure, dict):
        raise ValueError("询价结构分析不是对象，不能建立分块计划")
    if structure.get("blockers"):
        raise ValueError("询价结构存在阻断项；请核对商品行和来源，不能退回纯文本分块")
    rows = structure.get("rows")
    if not isinstance(rows, list):
        raise ValueError("询价结构必须提供 rows 列表")
    required = {"id", "table_id", "row_index", "start", "end", "text", "cells",
                "description_text", "quantity_text", "reference_text", "source"}
    previous_end = 0
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not required <= row.keys():
            raise ValueError("询价商品行字段不完整")
        if (not isinstance(row["id"], str) or not row["id"].strip()
                or row["id"] in identities):
            raise ValueError("询价商品行 ID 为空或重复")
        if (not isinstance(row["table_id"], str) or not row["table_id"].strip()
                or type(row["row_index"]) is not int or row["row_index"] < 1):
            raise ValueError("询价商品表格 ID 或行号无效")
        start, end = row["start"], row["end"]
        if (type(start) is not int or type(end) is not int
                or not previous_end <= start < end <= len(body)):
            raise ValueError("询价商品行原文偏移无效、未排序或重叠")
        if not isinstance(row["text"], str) or row["text"] != body[start:end] or not row["text"].strip():
            raise ValueError("询价商品行与原始正文未逐字对齐")
        if not isinstance(row["cells"], list) or not isinstance(row["source"], dict):
            raise ValueError("询价商品行 cells/source 来源结构无效")
        if not isinstance(row["description_text"], str) or not row["description_text"].strip():
            raise ValueError("询价商品行说明必须为非空字符串")
        if row["reference_text"] is not None and not isinstance(row["reference_text"], str):
            raise ValueError("询价商品行参考文本必须为字符串或明确未知 null")
        if not isinstance(row["quantity_text"], str):
            explicitly_unknown = (row["quantity_text"] is None
                                  and row["source"].get("quantity_not_provided") is True
                                  and not any(isinstance(cell, dict) and cell.get("role") == "quantity"
                                              for cell in row["cells"]))
            if not explicitly_unknown:
                raise ValueError("询价商品行数量必须为原文字符串，或具备无数量列来源依据的明确未知 null")
        if end - start > MAX_STRUCTURED_ROW_CHARACTERS:
            raise ValueError(f"完整商品行超过 {MAX_STRUCTURED_ROW_CHARACTERS} 字符硬上限；不会拆开商品行")
        try:
            json.dumps(row, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("询价商品行含非 JSON 来源数据") from None
        identities.add(row["id"])
        previous_end = end
    return deepcopy(rows)


def _structured_offsets(body: str, rows: list[dict], max_chars: int) -> list[tuple[int, int, list[dict]]]:
    result: list[tuple[int, int, list[dict]]] = []
    cursor = 0
    for row in rows:
        start, end = row["start"], row["end"]
        gap = body[cursor:start]
        prefix_start = start
        if gap.strip():
            result.extend((cursor + first, cursor + last, [])
                          for first, last in _plain_offsets(gap, max_chars))
        elif gap:
            # Whitespace may attach to either adjacent content-bearing block;
            # it never becomes a synthetic empty block or hides prose.
            if result:
                first, last, previous_rows = result[-1]
                capacity = MAX_STRUCTURED_ROW_CHARACTERS if previous_rows else max_chars
                attached_end = min(start, last + capacity - (last - first))
                if attached_end < start and attached_end and body[attached_end - 1:attached_end + 1] == "\r\n":
                    attached_end -= 1
                result[-1] = (first, attached_end, previous_rows)
                prefix_start = attached_end
            else:
                prefix_start = cursor
        if end - prefix_start > MAX_STRUCTURED_ROW_CHARACTERS:
            raise ValueError("商品行及相邻空白无法在 4000 字符硬上限内完整保留")
        result.append((prefix_start, end, [row]))
        cursor = end
    tail = body[cursor:]
    if tail.strip():
        result.extend((cursor + first, cursor + last, [])
                      for first, last in _plain_offsets(tail, max_chars))
    elif tail:
        first, last, previous_rows = result[-1]
        capacity = MAX_STRUCTURED_ROW_CHARACTERS if previous_rows else max_chars
        if len(body) - first > capacity:
            raise ValueError("商品行尾部空白无法在分块硬上限内完整保留")
        result[-1] = (first, len(body), previous_rows)
    return result


def build_inquiry_plan(inquiry: dict, *, max_chars: int = 600,
                       context_chars: int = 240) -> dict:
    """Plan lossless text coverage; structured product rows are indivisible."""
    if not isinstance(inquiry, dict):
        raise ValueError("询价分块需要原始询价对象")
    body = inquiry.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("询价正文为空或不是文本，无法建立完整分块计划")
    if len(body) > MAX_BODY_CHARACTERS:
        raise ValueError(f"询价正文超过 {MAX_BODY_CHARACTERS} 字符上限；不会截断原文")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_BODY_CHARACTERS:
        raise ValueError(f"分块字符上限必须为 1 至 {MAX_BODY_CHARACTERS} 的整数")
    if type(context_chars) is not int or not 0 <= context_chars <= MAX_BODY_CHARACTERS:
        raise ValueError(f"旁文字符数必须为 0 至 {MAX_BODY_CHARACTERS} 的整数")
    rows = _structure_rows(inquiry, body)
    offsets = (_structured_offsets(body, rows, max_chars) if rows else
               [(start, end, []) for start, end in _plain_offsets(body, max_chars)])
    if len(offsets) > MAX_CHUNKS:
        raise ValueError(f"询价分块超过 {MAX_CHUNKS} 块上限；整件未建立计划，不会截断原文")
    body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
    identity = {"version": PLAN_VERSION, "body_sha256": body_sha256,
                "max_chars": max_chars, "context_chars": context_chars,
                "boundaries": [(start, end) for start, end, _ in offsets],
                "structure_rows_sha256": _digest(rows)}
    plan_id = "plan_" + _digest(identity)
    chunks = []
    for index, (start, end, structured_rows) in enumerate(offsets, 1):
        text = body[start:end]
        chunks.append({"id": "chunk_" + _digest({"plan_id": plan_id,
                                                   "index": index,
                                                   "start": start, "end": end,
                                                   "text": text}),
                       "index": index, "start": start, "end": end,
                       "text": text,
                       "context_before": body[max(0, start - context_chars):start],
                       "context_after": body[end:end + context_chars],
                       "characters": end - start,
                       "structured_rows": structured_rows})
    return {"version": PLAN_VERSION, "plan_id": plan_id,
            "source_sha256": inquiry.get("sha256"),
            "body_sha256": body_sha256, "body_characters": len(body),
            "chunk_count": len(chunks), "chunks": chunks}
