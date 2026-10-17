"""SYNTHETIC public distribution defaults, without customer credentials or files."""
import pytest
from fastapi.testclient import TestClient

from tradeflow.api import create_app
from tradeflow.local_settings import template_seller_names, default_seller_name


def test_unconfigured_template_seller_fails_closed(monkeypatch):
    monkeypatch.delenv('TRADEFLOW_SELLER_NAME', raising=False)
    monkeypatch.delenv('TRADEFLOW_TEMPLATE_SELLER_NAMES', raising=False)
    assert default_seller_name() == ''
    with pytest.raises(ValueError, match='not configured'):
        template_seller_names()


def test_seller_names_can_be_configured_locally(monkeypatch):
    monkeypatch.setenv('TRADEFLOW_SELLER_NAME', 'SYNTHETIC LOCAL SELLER')
    monkeypatch.setenv('TRADEFLOW_TEMPLATE_SELLER_NAMES', '["SYNTHETIC LOCAL SELLER", "合成卖方"]')
    assert default_seller_name() == 'SYNTHETIC LOCAL SELLER'
    assert template_seller_names() == ['SYNTHETIC LOCAL SELLER', '合成卖方']


@pytest.mark.parametrize('value', ['invalid', '{}', '[]', '[null]', '[""]', '[1]'])
def test_invalid_seller_configuration_fails_explicitly(monkeypatch, value):
    monkeypatch.setenv('TRADEFLOW_TEMPLATE_SELLER_NAMES', value)
    with pytest.raises(ValueError):
        template_seller_names()


def test_empty_install_starts_and_has_no_private_records_or_templates(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/health').json()['status'] == 'ok'
        assert client.get('/').status_code == 200
        assert client.get('/templates').json() == []
        assert client.get('/document-jobs/sources').json() == []
        assert client.get('/customs/categories').json() == []
        assert client.get('/history-replay').json()['available'] is False
