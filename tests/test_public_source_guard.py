"""SYNTHETIC canaries for the public publishing guard; never use real keys."""
from pathlib import Path

import pytest

from tools.check_public_source import inspect_file


@pytest.mark.parametrize('relative', ['.env', '.evn', '.env.local', 'data/source.py', 'customer.xlsx', 'database.sqlite3-wal'])
def test_private_files_are_rejected(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('SYNTHETIC private data', encoding='utf-8')
    assert inspect_file(tmp_path, Path(relative))


def test_credential_canary_is_rejected_without_echoing_value(tmp_path):
    canary = 'ghp_' + 'A' * 36
    (tmp_path / 'source.py').write_text('SYNTHETIC_KEY = "' + canary + '"', encoding='utf-8')
    findings = inspect_file(tmp_path, Path('source.py'))
    assert findings == ['credential_pattern_at_line_1']
    assert all(canary not in finding for finding in findings)


def test_blank_env_example_and_synthetic_source_are_allowed(tmp_path):
    (tmp_path / '.env.example').write_text('TRADEFLOW_AGENT_API_KEY=\n', encoding='utf-8')
    (tmp_path / 'source.py').write_text('VALUE = "SYNTHETIC"\n', encoding='utf-8')
    assert inspect_file(tmp_path, Path('.env.example')) == []
    assert inspect_file(tmp_path, Path('source.py')) == []


def test_nonempty_example_key_is_rejected(tmp_path):
    (tmp_path / '.env.example').write_text('TRADEFLOW_AGENT_API_KEY=SYNTHETIC_NOT_ALLOWED\n', encoding='utf-8')
    assert inspect_file(tmp_path, Path('.env.example')) == ['nonempty_credential_example_at_line_1']
