"""Extract historical candidates without creating operational business records."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradeflow.history_replay import build_history_replay


def main():
    parser = argparse.ArgumentParser(description="本地样本历史只读回放；输出候选JSON，不创建业务记录或单据")
    parser.add_argument("--source-root", required=True, type=Path, help="完整出货文件目录，包含 excel 单据、ERP导出文件")
    parser.add_argument("--receipt-root", required=True, type=Path, help="发货签收单目录")
    parser.add_argument("--rules", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "business_rules.json")
    parser.add_argument("--output", type=Path, default=Path("data/history_replay.json"), help="新建JSON；已存在则拒绝覆盖")
    args = parser.parse_args()
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError("回放报告已存在，请选择新输出文件，禁止覆盖证据")
        rules = json.loads(args.rules.read_text(encoding="utf-8"))
        report = build_history_replay(args.source_root, args.receipt_root, rules)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        print(json.dumps({"output": str(args.output.absolute()), "source_files_read": report["source_files_read"], "source_unchanged": report["source_unchanged"], **report["actual_results"]}, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
