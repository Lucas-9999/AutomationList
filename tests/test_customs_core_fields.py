"""Opt-in customs fields use explicit synthetic facts and read-only templates."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256

import pytest

from tradeflow import customs
from tradeflow.customs_data import prepare_customs_document
from tradeflow.profiles import read_values
from tradeflow.templates import inspect_template, patch_xlsx_cells




@pytest.mark.parametrize("value,expected", [
    ("73181590.90", "7318159090"), ("7318159090", "7318159090"),
    ("00000000.01", "0000000001"), ("0000000000", "0000000000"),
])
def test_hs_syntax_only(value, expected):
    assert customs.normalize_hs_code(value) == expected


@pytest.mark.parametrize("value", [None, 7318159090, "", "73181590.9", "7318159.090", "73181590..90", "73181590900", " 7318159090", "7318159090 ", "７３１８１５９０９０", "73181590-90", "73181590/90", "73181590.9A"])
def test_invalid_hs_is_never_inferred_or_padded(value):
    with pytest.raises(ValueError, match="HS编码须"):
        customs.normalize_hs_code(value)
