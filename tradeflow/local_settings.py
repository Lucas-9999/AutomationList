"""Explicit local seller context; no customer or allowed seller is bundled."""
import json
import os


def template_seller_names() -> list[str]:
    raw = os.environ.get('TRADEFLOW_TEMPLATE_SELLER_NAMES', '').strip()
    if not raw:
        raise ValueError('TRADEFLOW_TEMPLATE_SELLER_NAMES not configured; confirm the template seller locally')
    try:
        names = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('TRADEFLOW_TEMPLATE_SELLER_NAMES must be a JSON array of seller names') from None
    if not isinstance(names, list) or not names or len(names) > 20 or any(
        not isinstance(name, str) or not name.strip() or len(name) > 300 for name in names
    ):
        raise ValueError('TRADEFLOW_TEMPLATE_SELLER_NAMES must contain explicit nonempty seller names')
    return [name.strip() for name in names]


def default_seller_name() -> str:
    return os.environ.get('TRADEFLOW_SELLER_NAME', '').strip()
