"""Synthetic OOXML formula tests. No real customer workbook is written."""
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from tradeflow.calculations import recalculate_supported_formulas
from tradeflow.templates import MAIN, patch_xlsx_cells


def numeric(reference, value):
    return f'<c r="{reference}" s="7"><v>{value}</v></c>'


def formula(reference, expression, cache="99999", attrs="", cell_type=""):
    kind = f' t="{cell_type}"' if cell_type else ""
    return f'<c r="{reference}" s="8"{kind}><f{attrs}>{escape(expression)}</f><v>{escape(cache)}</v></c>'


def book(*cells):
    sheet = (f'<worksheet xmlns="{MAIN}"><sheetData><row r="1">' + "".join(cells)
             + '</row></sheetData><pageMargins left="0.2" right="0.2" top="0.3" bottom="0.3" header="0.1" footer="0.1"/></worksheet>').encode()
    parts = {
        "xl/workbook.xml": (f'<workbook xmlns="{MAIN}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic" sheetId="1" r:id="rId1"/></sheets><calcPr calcId="1"/></workbook>').encode(),
        "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": sheet,
        "xl/styles.xml": b"synthetic style byte preservation fixture",
        "xl/media/image1.png": b"synthetic media byte preservation fixture",
        "xl/sharedStrings.xml": (f'<sst xmlns="{MAIN}"><si><r><t>synthetic </t></r><r><t>buyer</t></r><rPh><t>ignored phonetics</t></rPh></si><si><t>5</t></si></sst>').encode(),
    }
    output = BytesIO()
    with ZipFile(output, "w") as package:
        package.comment = b"synthetic formula fixture"
        for name, value in parts.items():
            info = ZipInfo(name, (2026, 10, 14, 12, 0, 0))
            info.compress_type = ZIP_DEFLATED
            package.writestr(info, value)
    return output.getvalue()


def parts(content):
    with ZipFile(BytesIO(content)) as package:
        return {name: package.read(name) for name in package.namelist()}


def cell_cache(content, reference):
    root = ET.fromstring(parts(content)["xl/worksheets/sheet1.xml"])
    cell = root.find(f"{{{MAIN}}}sheetData/{{{MAIN}}}row/{{{MAIN}}}c[@r='{reference}']")
    return cell, cell.find(f"{{{MAIN}}}v").text


def test_changed_inputs_update_products_and_all_dependent_totals_exactly():
    original = book(numeric("A1", "3"), numeric("B1", "0.10"), numeric("A2", "2"), numeric("B2", "0.20"),
                    formula("C1", "A1*B1"), formula("C2", "A2*B2"), formula("C3", "SUM(C1:C2)"), formula("C4", "C3"))
    changed = patch_xlsx_cells(original, {"Synthetic": {"A1": 7, "B2": Decimal("0.30")}})
    output, report = recalculate_supported_formulas(changed)
    assert report["calculated_values"]["Synthetic"] == {"C1": "0.70", "C2": "0.60", "C3": "1.30", "C4": "1.30"}
    assert report["formula_count"] == 4
    assert report["business_validation_passed"] is False
    before, after = parts(changed), parts(output)
    for name in before:
        if name != "xl/worksheets/sheet1.xml":
            assert before[name] == after[name]
    expected = before["xl/worksheets/sheet1.xml"]
    for reference, value in report["calculated_values"]["Synthetic"].items():
        cell, cache = cell_cache(output, reference)
        assert cache == value
        assert cell.get("s") == "8"
        expression = {"C1": "A1*B1", "C2": "A2*B2", "C3": "SUM(C1:C2)", "C4": "C3"}[reference]
        expected = expected.replace(formula(reference, expression).encode(), formula(reference, expression, value).encode())
    assert after["xl/worksheets/sheet1.xml"] == expected


def test_precedence_parentheses_unary_absolute_refs_and_division_policy():
    source = book(numeric("A1", "2"), numeric("B1", "0.10"),
                  formula("C1", "-(+$A$1 + B1) * (3-1)/2"), formula("D1", "1/3"))
    output, report = recalculate_supported_formulas(source)
    assert Decimal(cell_cache(output, "C1")[1]) == Decimal("-2.10")
    assert cell_cache(output, "D1")[1] == "0." + "3" * 50
    assert report["scope"]["division_precision"] == 50
    assert report["scope"]["division_rounding"] == "ROUND_HALF_EVEN"
    assert report["scope"]["complete_excel_engine"] is False


def test_direct_shared_string_and_inline_text_references_update_text_cache():
    source = book('<c r="A1" t="s"><v>0</v></c>', '<c r="B1" t="inlineStr"><is><t>synthetic &amp; text</t></is></c>',
                  formula("C1", "A1"), formula("D1", "B1"), formula("E1", "C1"))
    output, report = recalculate_supported_formulas(source)
    for reference, expected in (("C1", "synthetic buyer"), ("D1", "synthetic & text"), ("E1", "synthetic buyer")):
        cell, value = cell_cache(output, reference)
        assert cell.get("t") == "str"
        assert value == expected
    assert report["calculated_cells"][0]["type"] == "text"


def test_sum_rectangles_ignore_text_numeric_strings_and_missing_cells():
    source = book(numeric("A1", "0.1"), numeric("B1", "0.2"), '<c r="A2" t="s"><v>1</v></c>',
                  '<c r="B2" t="inlineStr"><is><t>ignored synthetic text</t></is></c>',
                  formula("D1", "SUM(A1:C3)"), formula("D2", "SUM(C3)"), formula("D3", "C3+1"))
    output, _ = recalculate_supported_formulas(source)
    assert cell_cache(output, "D1")[1] == "0.3"
    assert cell_cache(output, "D2")[1] == "0"
    assert cell_cache(output, "D3")[1] == "1"


def test_dependencies_use_recomputed_formula_values_instead_of_historical_caches():
    source = book(formula("D1", "C1*2"), formula("C1", "B1+1"), formula("B1", "A1+1"), numeric("A1", "0"))
    output, _ = recalculate_supported_formulas(source)
    assert cell_cache(output, "D1")[1] == "4"


@pytest.mark.parametrize("expression", ["[1]contract!F42", "OtherSheet!A1", "AVERAGE(A1:A2)", "IF(A1,1,2)", "A1^2", "A1&1", "SUM(A2:A1)", "SUM(1,2)", "1/0", "'text'", "A1+", "__import__('os')"])
def test_external_unknown_unsafe_and_invalid_formulas_fail(expression):
    with pytest.raises(ValueError):
        recalculate_supported_formulas(book(numeric("A1", "1"), formula("B1", expression)))


def test_cycle_and_range_self_dependency_fail_atomically():
    with pytest.raises(ValueError, match="循环"):
        recalculate_supported_formulas(book(formula("A1", "B1"), formula("B1", "A1")))
    with pytest.raises(ValueError, match="循环"):
        recalculate_supported_formulas(book(formula("A1", "SUM(A1:A2)")))


@pytest.mark.parametrize("input_cell", ['<c r="A1" t="e"><v>#VALUE!</v></c>', '<c r="A1"><v>NaN</v></c>', '<c r="A1" t="s"><v>99</v></c>'])
def test_error_values_and_invalid_inputs_fail(input_cell):
    with pytest.raises(ValueError):
        recalculate_supported_formulas(book(input_cell, formula("B1", "A1")))


def test_text_is_not_silently_coerced_in_arithmetic():
    with pytest.raises(ValueError, match="文本"):
        recalculate_supported_formulas(book('<c r="A1" t="s"><v>1</v></c>', formula("B1", "A1+1")))


def test_array_formula_is_rejected():
    with pytest.raises(ValueError, match="数组"):
        recalculate_supported_formulas(book(numeric("A1", "1"), formula("B1", "A1", attrs=' t="array" ref="B1:B2"')))


def test_blank_direct_reference_returns_numeric_zero_and_old_cache_type_is_updated():
    output, _ = recalculate_supported_formulas(book(formula("B1", "A1", cache="old synthetic text", cell_type="str")))
    cell, cache = cell_cache(output, "B1")
    assert cache == "0"
    assert cell.get("t") is None


def test_no_formulas_returns_original_bytes():
    source = book(numeric("A1", "1"))
    output, report = recalculate_supported_formulas(source)
    assert output == source
    assert report["formula_count"] == 0


def test_output_is_deterministic():
    source = book(numeric("A1", "0.10"), formula("B1", "A1*3"))
    assert recalculate_supported_formulas(source) == recalculate_supported_formulas(source)


def test_shared_relative_rows_recalculate_after_input_update_and_keep_shared_xml():
    source = book(numeric("H35", "1"), numeric("H36", "2"), numeric("H37", "3"),
                  formula("I35", "H35+0.5", attrs=' t="shared" ref="I35:I37" si="0"'),
                  formula("I36", "", attrs=' t="shared" si="0"'), formula("I37", "", attrs=' t="shared" si="0"'),
                  formula("I38", "SUM(I35:I37)"))
    updated = patch_xlsx_cells(source, {"Synthetic": {"H36": Decimal("10")}})
    output, report = recalculate_supported_formulas(updated)
    for reference, expected in (("I35", "1.5"), ("I36", "10.5"), ("I37", "3.5"), ("I38", "15.5")):
        assert cell_cache(output, reference)[1] == expected
    assert b'<f t="shared" ref="I35:I37" si="0">H35+0.5</f>' in parts(output)["xl/worksheets/sheet1.xml"]
    assert b'<f t="shared" si="0"></f>' in parts(output)["xl/worksheets/sheet1.xml"]
    assert all(record["shared_formula"] for record in report["calculated_cells"] if record["cell"] in {"I35", "I36", "I37"})


def test_shared_mixed_absolute_axes_are_preserved():
    source = book(numeric("A1", "1"), numeric("A2", "2"), numeric("B1", "10"),
                  formula("C1", "$A1+B$1+$A$1", attrs=' t="shared" ref="C1:C2" si="0"'),
                  formula("C2", "", attrs=' t="shared" si="0"'))
    output, report = recalculate_supported_formulas(source)
    assert cell_cache(output, "C1")[1] == "12"
    assert cell_cache(output, "C2")[1] == "13"
    assert next(record["formula"] for record in report["calculated_cells"] if record["cell"] == "C2") == "$A2+B$1+$A$1"


def test_shared_relative_columns_can_depend_on_recalculated_shared_anchor():
    source = book(numeric("A1", "2"), numeric("B1", "3"),
                  formula("C1", "A1*2", attrs=' t="shared" ref="C1:E1" si="0"'),
                  formula("D1", "", attrs=' t="shared" si="0"'), formula("E1", "", attrs=' t="shared" si="0"'))
    output, _ = recalculate_supported_formulas(source)
    assert [cell_cache(output, cell)[1] for cell in ("C1", "D1", "E1")] == ["4", "6", "8"]


def test_shared_sum_rectangles_shift_each_relative_endpoint():
    source = book(numeric("A1", "1"), numeric("A2", "2"), numeric("A3", "3"),
                  formula("B1", "SUM(A1:A2)", attrs=' t="shared" ref="B1:B2" si="7"'),
                  formula("B2", "", attrs=' t="shared" si="7"'))
    output, _ = recalculate_supported_formulas(source)
    assert cell_cache(output, "B1")[1] == "3"
    assert cell_cache(output, "B2")[1] == "5"


def test_shared_direct_text_references_use_current_inline_values():
    source = book('<c r="A1" t="s"><v>0</v></c>', '<c r="A2" t="inlineStr"><is><t>synthetic second buyer</t></is></c>',
                  formula("B1", "A1", attrs=' t="shared" ref="B1:B2" si="0"'),
                  formula("B2", "", attrs=' t="shared" si="0"'))
    output, _ = recalculate_supported_formulas(source)
    assert cell_cache(output, "B1")[1] == "synthetic buyer"
    assert cell_cache(output, "B2")[1] == "synthetic second buyer"
    assert cell_cache(output, "B2")[0].get("t") == "str"


@pytest.mark.parametrize("formula_cells", [
    (formula("B1", "", attrs=' t="shared" si="0"'),),
    (formula("B1", "A1", attrs=' t="shared" ref="B1:B2" si="0"'), formula("B2", "A2", attrs=' t="shared" si="0"')),
    (formula("B1", "A1", attrs=' t="shared" ref="B1:B2" si="0"'),),
    (formula("B1", "A1", attrs=' t="shared" ref="B1:B2" si="0"'), formula("B3", "", attrs=' t="shared" si="0"')),
    (formula("C2", "A1", attrs=' t="shared" ref="C1:C2" si="0"'), formula("C1", "", attrs=' t="shared" si="0"')),
    (formula("B1", "Other!A1", attrs=' t="shared" ref="B1:B2" si="0"'), formula("B2", "", attrs=' t="shared" si="0"')),
    (formula("B1", "IF(A1,1,2)", attrs=' t="shared" ref="B1:B2" si="0"'), formula("B2", "", attrs=' t="shared" si="0"')),
], ids=["missing-anchor", "duplicate-anchor", "missing-member", "outside-member", "shift-outside-sheet", "cross-sheet", "unknown-function"])
def test_invalid_or_unsupported_shared_groups_fail(formula_cells):
    with pytest.raises(ValueError):
        recalculate_supported_formulas(book(numeric("A1", "1"), *formula_cells))


def test_shared_overlap_is_rejected():
    source = book(numeric("A1", "1"),
                  formula("B1", "A1", attrs=' t="shared" ref="B1:B3" si="0"'),
                  formula("B2", "", attrs=' t="shared" si="0"'),
                  formula("B3", "A3", attrs=' t="shared" ref="B3:B4" si="1"'),
                  formula("B4", "", attrs=' t="shared" si="1"'))
    with pytest.raises(ValueError):
        recalculate_supported_formulas(source)


@pytest.mark.parametrize("expression, expected", [
    ("ROUND(1.005,2)", "1.01"), ("ROUND(-1.005,2)", "-1.01"),
    ("ROUND(0.0049,2)", "0.00"), ("ROUND(125,-1)", "130"),
    ("ROUND(ROUND(0.12345,4)*1.5,2)", "0.19"),
])
def test_explicit_round_uses_decimal_half_up(expression, expected):
    output, _ = recalculate_supported_formulas(book(formula("C1", expression)))
    assert Decimal(cell_cache(output, "C1")[1]) == Decimal(expected)


@pytest.mark.parametrize("expression", ["ROUND(1,2.5)", "ROUND(1,101)", "ROUND(1)", "ROUND(1,2,3)"])
def test_round_rejects_unsupported_precision_or_arguments(expression):
    with pytest.raises(ValueError):
        recalculate_supported_formulas(book(formula("C1", expression)))
