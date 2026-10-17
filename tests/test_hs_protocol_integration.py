"""SYNTHETIC provider switching; no real keys or external requests."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tradeflow import hs_catalog
from tradeflow.api import create_app
from tradeflow.store import Store


def test_tax_and_mcp_snapshots_never_share_cache_or_versions(tmp_path, monkeypatch):
    store = Store(tmp_path)
    calls = []

    def provider(name):
        def detail(code):
            calls.append(name)
            data = {"provider": name, "hs_code": code, "name": "SYNTHETIC CATEGORY", "remark": "SYNTHETIC",
                    "unit1": "千克", "unit2": "", "status": "active", "provider_updated_at": "2026-01-01",
                    "reporting_elements": [{"order": 0, "name": "SYNTHETIC", "required": True}]}
            return data, sha256(json.dumps(data).encode()).hexdigest()
        return SimpleNamespace(name=name, configured=True, detail=detail)

    selected = [provider('hsciq')]
    monkeypatch.setattr(hs_catalog, '_provider', lambda: selected[0])
    tax = hs_catalog.lookup(store, '0000000000')
    selected[0] = provider('hsciq_mcp')
    assert hs_catalog.latest_snapshot(store, '0000000000') is None
    mcp = hs_catalog.lookup(store, '0000000000')
    assert mcp['provider'] == 'hsciq_mcp' and mcp['snapshot_id'] != tax['snapshot_id']
    assert tax['version'] == mcp['version'] == 1
    assert hs_catalog.lookup(store, '0000000000')['snapshot_id'] == mcp['snapshot_id']
    selected[0] = provider('hsciq')
    assert hs_catalog.lookup(store, '0000000000')['snapshot_id'] == tax['snapshot_id']
    assert calls == ['hsciq', 'hsciq_mcp']


def test_unsupported_update_operation_has_explicit_safe_http_result(tmp_path, monkeypatch):
    def unsupported(*args, **kwargs):
        raise hs_catalog.CatalogError('unsupported_operation')
    monkeypatch.setattr(hs_catalog, 'updates', unsupported)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.get('/customs/hs-catalog/updates?range=30d')
        assert response.status_code == 400
        assert response.json()['code'] == 'unsupported_operation'
        assert '鉴权失败' not in response.json()['detail']
    assert app.state.store.history('hs_catalog')[-1]['detail']['code'] == 'unsupported_operation'
