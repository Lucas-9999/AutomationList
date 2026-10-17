"""Import authorized source files as unverified samples, never overwrite originals."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradeflow.ingestion import parse_erp_xlsx
from tradeflow.store import Store


def bootstrap(root: Path, data_root: Path):
    store = Store(data_root)
    result = {"root": str(root.resolve()), "shipments": [], "templates": []}
    for path in sorted((root / "ERP导出文件").glob("*.xlsx")):
        content = path.read_bytes()
        digest = store.save_original(content)
        record = store.find("shipment", "sample:" + digest)
        if not record:
            parsed = parse_erp_xlsx(content, path.name)
            record = store.create_record("shipment", "sample:" + digest, parsed)
        result["shipments"].append({"id": record["id"], "file": str(path), "rows": len(record["payload"]["items"]), "source_type": record["payload"]["source"]["document_type"]})
    for path in sorted((root / "excel 单据").glob("*.xlsx")):
        stem = path.stem.lower()
        kind = next((k for k in ("contract", "ci", "pi", "pl") if k in stem), None)
        if "报关" in stem:
            kind = "customs"
        if kind is None:
            continue
        content = path.read_bytes()
        digest = store.save_original(content)
        key = f"{kind}:{digest}"
        record = store.find("template", key)
        if not record:
            record = store.create_record("template", key, {"document_type": kind, "filename": path.name, "sha256": digest, "status": "received_unverified", "format_accepted": False})
        result["templates"].append({"id": record["id"], "type": kind, "file": str(path), "format_accepted": False})
    return result


if __name__ == "__main__":
    root = Path(sys.argv[1])
    data_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data")
    result = bootstrap(root, data_root)
    destination = data_root / "sample_bootstrap.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"erp_files": len(result["shipments"]), "template_files": len(result["templates"]), "record_index": str(destination.resolve()), "accepted_templates": 0}, ensure_ascii=False))
