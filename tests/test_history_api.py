"""Read-only report endpoint using synthetic local evidence."""
from hashlib import sha256
import json

from fastapi.testclient import TestClient

from tradeflow.api import create_app


def test_report_is_local_read_only_and_digest_matches(tmp_path):
    app = create_app(tmp_path / "SYNTHETIC")
    with TestClient(app) as client:
        assert client.get("/history-replay").json()["available"] is False
        report = {"format": "tradeflow-history-replay-v1", "status": "SYNTHETIC", "summary": {"business_records_created": 0}}
        content = json.dumps(report).encode()
        (app.state.store.root / "history_replay.json").write_bytes(content)
        with app.state.store.connect() as con:
            counts = [con.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in ("records", "audit", "workflows")]
        result = client.get("/history-replay")
        assert result.status_code == 200
        assert result.json() == {"available": True, "report_sha256": sha256(content).hexdigest(), "report": report}
        with app.state.store.connect() as con:
            assert counts == [con.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in ("records", "audit", "workflows")]
        assert (app.state.store.root / "history_replay.json").read_bytes() == content


def test_damaged_or_unknown_report_is_not_exposed_as_valid(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        for content in (b"{BROKEN SYNTHETIC", b'{"format":"UNKNOWN"}', b"[]"):
            (tmp_path / "history_replay.json").write_bytes(content)
            assert client.get("/history-replay").status_code == 422
