"""Register authorized inquiries and receipt originals without modifying them."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradeflow.ingestion import parse_email
from tradeflow.inquiry_workbook import parse_inquiry_workbook
from tradeflow.inquiry_document import parse_inquiry_document
from tradeflow.receipts import inspect_receipt_template
from tradeflow.store import Store


def intake(inquiries: Path, receipts: Path, data: Path):
    store = Store(data)
    result = {"inquiries": [], "receipts": [], "skipped": []}
    for folder, kind in ((inquiries, "inquiry"), (receipts, "receipt")):
        if not folder.is_dir():
            raise ValueError("Source directory is missing: " + str(folder))
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            extension = path.suffix.lower()
            if extension not in ({".eml", ".txt", ".xlsx", ".docx"} if kind == "inquiry" else {".xlsx"}):
                result["skipped"].append(path.name)
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if kind == "inquiry":
                if extension == ".xlsx":
                    payload = parse_inquiry_workbook(content, path.name)
                elif extension == ".docx":
                    payload = parse_inquiry_document(content, path.name)
                else:
                    payload = parse_email(content, path.name)
                record = store.find("inquiry", digest)
                if not record:
                    store.save_original(content)
                    record = store.create_record("inquiry", digest, payload)
                result["inquiries"].append({"id": record["id"], "file": str(path), "sha256": digest, "body_chars": len(record["payload"]["body"]), "translated": False})
            else:
                profile = inspect_receipt_template(content)
                key = "receipt:" + digest
                record = store.find("template", key)
                if not record:
                    store.save_original(content)
                    record = store.create_record("template", key, {"document_type": "receipt", "filename": path.name, "sha256": digest, "status": "received_unverified", "format_accepted": False})
                result["receipts"].append({"id": record["id"], "file": str(path), "sha256": digest, "line_capacity": profile.get("line_capacity"), "format_accepted": False})
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError("Source changed during intake: " + path.name)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inquiries", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args()
    index = intake(args.inquiries, args.receipts, args.data)
    destination = args.data / "customer_inputs.json"
    destination.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"inquiries": len(index["inquiries"]), "receipts": len(index["receipts"]), "skipped": index["skipped"], "index": str(destination.resolve())}, ensure_ascii=False))
