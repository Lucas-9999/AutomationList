"""Unsigned historical replay copies, separate from operational batch documents."""
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import re

from .drafts import generate_review_draft, NUMERIC_FIELDS
from .layout_review import inspect_text_fit
from .profiles import candidate_profile, read_values
from .receipts import generate_receipt_draft, inspect_receipt_template, NUMERIC
from .templates import patch_xlsx_cells


def build_historical_copies(replay: dict) -> list[tuple[str, bytes, dict]]:
    if replay.get("format") != "tradeflow-history-replay-v1" or not replay.get("source_unchanged"):
        raise ValueError("需要有效且源文件未变化的历史回放报告")
    if replay["actual_results"]["checks_failed"]:
        raise ValueError("历史一致性检查未通过，禁止生成回放副本")
    copies = []
    for document in replay["documents"]:
        path, kind = Path(document["path"]), document["kind"]
        content = path.read_bytes()
        if sha256(content).hexdigest() != document["sha256"]:
            raise ValueError("历史原件已变化，必须重新提取：" + path.name)
        if kind == "receipt":
            profile, numeric = inspect_receipt_template(content), NUMERIC
        elif kind in {"contract", "ci", "pi", "pl"}:
            profile, numeric = candidate_profile(content, kind), NUMERIC_FIELDS
        else:
            raise ValueError("历史回放副本不支持此类型：" + kind)
        fields, changes = {}, []
        for field in profile["fields"]:
            key = field["field"]
            original = document["headers"][key]["value"]
            value = original.replace("\xa0", " ")
            prefix = field["prefix"].replace("\xa0", " ").strip()
            if prefix:
                if not value.startswith(prefix):
                    raise ValueError("历史表头前缀与绑定映射不一致：" + key)
                value = value[len(prefix):].lstrip()
            if key == "payment_terms":
                policy = next(x["replay_down_payment_percent"] for x in replay["candidate_orders"] if x["batch_group"] == document["batch_group"])
                if policy != "30":
                    raise ValueError("当前模板只支持已确认30%预付款政策")
                value, count = re.subn(r"\d+(?:\.\d+)?\s*%\s*down\s+payment", "30% down payment", value, flags=re.I)
                if count != 1:
                    raise ValueError("历史付款条款无法唯一绑定预付款比例")
            fields[key] = value
            changes.append({"field": key, "source": document["headers"][key], "input_without_template_prefix": value})
        items = [{key: Decimal(fact["value"]) if key in numeric else fact["value"]
                  for key, fact in row["facts"].items()} for row in document["items"]]
        numbering_changes = []
        for index, (item, row) in enumerate(zip(items, document["items"]), 1):
            if item["number"] != index:
                numbering_changes.append({"source": row["facts"]["number"], "replay_sequence": str(index),
                                          "reason": "审阅引擎按副本内行序从1编号；原历史序号与源行ID保留"})
            item["number"] = Decimal(index)
        generated, engine_report = (generate_receipt_draft(content, fields, items) if kind == "receipt"
                                    else generate_review_draft(content, kind, fields, items))
        title = "A7" if kind in {"receipt", "pl"} else "A6"
        original_title = read_values(content, profile["inspection"])[profile["sheet"]][title]
        replay_title = original_title + " / 历史回放·待审阅"
        generated = patch_xlsx_cells(generated, {profile["sheet"]: {title: replay_title}})
        report = {**engine_report, "output_sha256": sha256(generated).hexdigest(), "historical_replay_only": True,
                  "business_record_created": False, "operational_batch_document": False,
                  "source_filename": path.name, "source_sha256": document["sha256"],
                  "batch_group": document["batch_group"], "header_inputs": changes,
                  "historical_sequence_normalizations": numbering_changes,
                  "history_title_marker": {"sheet": profile["sheet"], "cell": title, "source_title": original_title, "replay_title": replay_title},
                  "historical_coverage_boundaries": replay["coverage_boundaries"],
                  "layout_preflight": inspect_text_fit(generated),
                  "note": "复制历史单据覆盖范围，仅供回放验收；不能作为新订单实际批次单据或签署/签收事实。"}
        copies.append((document["batch_group"] + "-" + kind + "-HISTORICAL-REPLAY.xlsx", generated, report))
    return copies
