"""Create the parent of explicitly requested workspace test directories on clean clones."""
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def synthetic_template_seller_configuration(monkeypatch):
    # This is test-only configuration for synthetic in-memory templates.
    # The application itself has no default template seller allowlist.
    monkeypatch.setenv('TRADEFLOW_TEMPLATE_SELLER_NAMES', '["SYNTHETIC SELLER LTD", "合成示例卖方公司"]')


def pytest_configure(config):
    requested = config.getoption('basetemp')
    if requested:
        base = Path(requested).resolve()
        workspace_tmp = Path(__file__).resolve().parents[1] / 'tmp'
        if workspace_tmp in base.parents:
            base.parent.mkdir(parents=True, exist_ok=True)
