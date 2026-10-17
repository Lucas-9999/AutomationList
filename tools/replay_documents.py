"""Create a new directory of historical-only review copies; never overwrites."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradeflow.history_drafts import build_historical_copies


def main():
    parser = argparse.ArgumentParser(description="按已提取历史事实生成未签署回放副本，不创建实际批次单据")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--new-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.new_output.exists() or args.new_output.is_symlink():
            raise ValueError("输出目录已存在，禁止覆盖历史证据")
        replay = json.loads(args.report.read_text(encoding="utf-8"))
        copies = build_historical_copies(replay)
        args.new_output.mkdir(parents=True, exist_ok=False)
        manifest = {"historical_replay_only": True, "format_accepted": False, "business_records_created": 0,
                    "source_report": str(args.report.absolute()), "documents": []}
        for name, content, report in copies:
            (args.new_output / name).write_bytes(content)
            (args.new_output / (name + ".report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest["documents"].append({"filename": name, "sha256": report["output_sha256"], "source_sha256": report["source_sha256"], "document_type": report["document_type"], "batch_group": report["batch_group"]})
        (args.new_output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(args.new_output.absolute()), "historical_review_copies": len(copies), "business_records_created": 0, "format_accepted": False}, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
