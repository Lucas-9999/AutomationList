"""Local UI upgrades must not mix new HTML with stale document-job scripts."""
from hashlib import sha256
from pathlib import Path
import re

from fastapi.testclient import TestClient

from tradeflow.api import create_app


def test_html_references_script_content_version_and_revalidates(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.get('/')
        digest = sha256((Path(__file__).parents[1] / 'tradeflow/static/document_jobs.js').read_bytes()).hexdigest()[:16]
        assert f'/static/document_jobs.js?v={digest}' in response.text
        assert 'no-store' in response.headers.get('cache-control', '')
        script = client.get(f'/static/document_jobs.js?v={digest}')
        assert script.status_code == 200
        assert 'no-store' in script.headers.get('cache-control', '')


def test_content_change_alters_version_without_editing_html(tmp_path, monkeypatch):
    from tradeflow import api
    static = tmp_path / 'SYNTHETIC-static'
    static.mkdir()
    (static / 'index.html').write_text('<script src="/static/document_jobs.js" defer></script>', encoding='utf-8')
    script = static / 'document_jobs.js'
    script.write_text('// SYNTHETIC v1', encoding='utf-8')
    monkeypatch.setattr(api, 'STATIC_ROOT', static, raising=False)
    with TestClient(create_app(tmp_path / 'data')) as client:
        first = client.get('/').text
        script.write_text('// SYNTHETIC v2', encoding='utf-8')
        second = client.get('/').text
        assert first != second
        assert '?v=' in first and '?v=' in second
        assert re.search(r'document_jobs.js\?v=[0-9a-f]{16}', second)
