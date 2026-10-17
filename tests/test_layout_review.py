"""Synthetic OOXML display-fit tests; no customer workbook is modified."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tradeflow.layout_review import inspect_text_fit
from tradeflow.templates import MAIN, DOC_REL


def synthetic_book(text="synthetic short", *, columns=30, height=25, wrap=False,
                   shrink=False, merged=None, bold=False, font=True, size=11,
                   cell_xml=None, extra=None, alignment=""):
    font_xml = f'<font><name val="Synthetic Font"/><sz val="{size}"/>{"<b/>" if bold else ""}</font>' if font else "<font/>"
    styles = f'<styleSheet xmlns="{MAIN}"><fonts count="1">{font_xml}</fonts><cellXfs count="1"><xf numFmtId="0" fontId="0"><alignment wrapText="{int(wrap)}" shrinkToFit="{int(shrink)}" {alignment}/></xf></cellXfs></styleSheet>'
    from xml.sax.saxutils import escape
    cell_xml = cell_xml or f'<c r="A1" t="inlineStr" s="0"><is><t>{escape(text)}</t></is></c>'
    merge_xml = f'<mergeCells count="1"><mergeCell ref="{merged}"/></mergeCells>' if merged else ""
    sheet = f'<worksheet xmlns="{MAIN}"><dimension ref="A1:XFD1048576"/><sheetFormatPr defaultColWidth="8.43" defaultRowHeight="15"/><cols><col min="1" max="3" width="{columns}"/></cols><sheetData><row r="1" ht="{height}" customHeight="1">{cell_xml}</row></sheetData>{merge_xml}</worksheet>'
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets><sheet name="Synthetic" sheetId="1" r:id="rId1"/></sheets></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'.encode(),
        "xl/worksheets/sheet1.xml": sheet.encode(),
        "xl/styles.xml": styles.encode(),
    }
    parts.update(extra or {})
    target = BytesIO()
    with ZipFile(target, "w", ZIP_DEFLATED) as package:
        for name, content in parts.items():
            package.writestr(name, content)
    return target.getvalue()


def only_cell(report):
    return report["cells"][0]


def test_short_text_is_within_estimate_without_native_acceptance_claim():
    content = synthetic_book()
    result = inspect_text_fit(content)
    cell = only_cell(result)
    assert cell["risk"] == "within_estimate"
    assert cell["estimated_text_width_px"] < cell["available_width_px"]
    assert result["native_print_verified"] is False
    assert result["fonts_installed_verified"] is False
    assert result["measurement_method"] == "conservative_character_estimate"
    assert result["summary"]["checked_cells"] == 1


@pytest.mark.parametrize("text", ["SYNTHETIC MATERIAL " * 30, "合成长中文材质和备注" * 30])
def test_long_english_and_chinese_text_reports_horizontal_risk(text):
    result = inspect_text_fit(synthetic_book(text, columns=8))
    cell = only_cell(result)
    assert cell["horizontal_risk"]
    assert cell["text_length"] == len(text)
    assert cell["risk"] == "possible_spill"
    assert cell["spill_behavior"] == "unverified_blank_neighbor"


def test_adjacent_occupied_cell_blocks_text_spill():
    xml = '<c r="A1" t="inlineStr"><is><t>synthetic very long material value</t></is></c><c r="B1" t="inlineStr"><is><t>next column</t></is></c>'
    cell = only_cell(inspect_text_fit(synthetic_book(columns=5, cell_xml=xml)))
    assert cell["risk"] == "possible_clipping"
    assert cell["spill_behavior"] == "blocked_by_neighbor"
    assert cell["clipping_priority"] == "high"


def test_merged_range_uses_combined_width_and_long_title_is_reviewed():
    short = inspect_text_fit(synthetic_book("synthetic title", columns=8, merged="A1:C1"))
    plain = inspect_text_fit(synthetic_book("synthetic title", columns=8))
    assert only_cell(short)["available_width_px"] > only_cell(plain)["available_width_px"] * 2
    title = "Commercial Invoice / REVIEW DRAFT / 待审阅 " * 8
    cell = only_cell(inspect_text_fit(synthetic_book(title, columns=8, merged="A1:C1")))
    assert cell["merged_range"] == "A1:C1"
    assert cell["horizontal_risk"] and cell["clipping_priority"] == "high"


def test_wrapped_text_compares_required_height_to_actual_row_height():
    low = only_cell(inspect_text_fit(synthetic_book("synthetic wrapped wording " * 8, columns=12, wrap=True, height=15)))
    tall = only_cell(inspect_text_fit(synthetic_book("synthetic wrapped wording " * 8, columns=12, wrap=True, height=360)))
    assert low["wrap_text"] is True and not low["horizontal_risk"]
    assert low["estimated_line_count"] > 1 and low["vertical_risk"]
    assert low["risk"] == "possible_clipping"
    assert not tall["vertical_risk"]


def test_explicit_newlines_require_height_without_wrap_flag():
    cell = only_cell(inspect_text_fit(synthetic_book("synthetic\nsecond\nthird", height=15)))
    assert cell["estimated_line_count"] == 3
    assert cell["vertical_risk"]


def test_shrink_to_fit_does_not_claim_font_readability_verified():
    cell = only_cell(inspect_text_fit(synthetic_book("synthetic long material " * 12, columns=8, shrink=True)))
    assert cell["risk"] == "shrink_to_fit_unverified"
    assert cell["shrink_to_fit"]
    assert cell["estimated_shrunk_font_pt"] < cell["font"]["size_pt"]
    assert any("可读性" in reason for reason in cell["reasons"])


def test_missing_font_is_unknown_and_nonexistent_font_not_claimed_installed():
    missing = only_cell(inspect_text_fit(synthetic_book(font=False)))
    declared = only_cell(inspect_text_fit(synthetic_book()))
    assert missing["measurement_status"] == "unknown"
    assert missing["risk"] == "unknown"
    assert any("字体名称或字号" in reason for reason in missing["reasons"])
    assert declared["font"]["name"] == "Synthetic Font"
    assert declared["font"]["availability"] == "not_checked"


def test_nonfinite_or_extreme_font_geometry_is_not_exported_as_measured_fact():
    cell = only_cell(inspect_text_fit(synthetic_book(size="1e300")))
    assert cell["risk"] == "unknown"
    assert cell["font"]["size_pt"] is None
    assert cell["estimated_text_width_px"] > 0


def test_bold_and_font_size_change_width_estimate():
    normal = only_cell(inspect_text_fit(synthetic_book("Synthetic", size=11)))
    bold = only_cell(inspect_text_fit(synthetic_book("Synthetic", size=11, bold=True)))
    larger = only_cell(inspect_text_fit(synthetic_book("Synthetic", size=22)))
    assert bold["estimated_text_width_px"] > normal["estimated_text_width_px"]
    assert larger["estimated_text_width_px"] > normal["estimated_text_width_px"] * 1.99


def test_formula_width_uses_cached_display_not_expression():
    formula = "SUM(A100:A999)+SUM(B100:B999)+SUM(C100:C999)"
    xml = f'<c r="A1"><f>{formula}</f><v>123</v></c>'
    cell = only_cell(inspect_text_fit(synthetic_book(columns=10, cell_xml=xml)))
    assert cell["text"] == "123"
    assert cell["text_length"] == 3
    assert cell["formula"] == formula and cell["formula_cache_verified"] is False
    assert not cell["horizontal_risk"]


def test_formula_without_cache_is_unknown():
    cell = only_cell(inspect_text_fit(synthetic_book(cell_xml='<c r="A1"><f>1+2</f></c>')))
    assert cell["risk"] == "unknown"
    assert cell["text"] == ""
    assert any("没有可显示缓存" in reason for reason in cell["reasons"])


def test_numeric_format_measures_displayed_decimal_and_currency():
    styles = f'<styleSheet xmlns="{MAIN}"><numFmts count="1"><numFmt numFmtId="164" formatCode="&quot;$&quot;#,##0.00"/></numFmts><fonts count="1"><font><name val="Synthetic"/><sz val="11"/></font></fonts><cellXfs count="1"><xf fontId="0" numFmtId="164"/></cellXfs></styleSheet>'.encode()
    cell = only_cell(inspect_text_fit(synthetic_book(cell_xml='<c r="A1"><v>1234.5</v></c>', extra={"xl/styles.xml": styles})))
    assert cell["text"] == "$1,234.50"
    assert cell["number_format"] == '"$"#,##0.00'
    assert cell["measurement_status"] == "estimate"


def test_complex_format_rotation_and_rich_text_are_unknown():
    rotated = only_cell(inspect_text_fit(synthetic_book(alignment='textRotation="45"')))
    rich = only_cell(inspect_text_fit(synthetic_book(cell_xml='<c r="A1" t="inlineStr"><is><r><rPr><sz val="32"/></rPr><t>synthetic rich</t></r></is></c>')))
    assert rotated["risk"] == "unknown" and rich["risk"] == "unknown"
    assert any("旋转" in value for value in rotated["reasons"])
    assert any("富文本" in value for value in rich["reasons"])


def test_date_format_and_shared_rich_text_are_unknown_instead_of_guessed():
    styles = f'<styleSheet xmlns="{MAIN}"><fonts count="1"><font><name val="Synthetic"/><sz val="11"/></font></fonts><cellXfs count="1"><xf fontId="0" numFmtId="14"/></cellXfs></styleSheet>'.encode()
    date = only_cell(inspect_text_fit(synthetic_book(cell_xml='<c r="A1"><v>46000</v></c>', extra={"xl/styles.xml": styles})))
    shared = f'<sst xmlns="{MAIN}"><si><r><rPr><sz val="32"/></rPr><t>synthetic shared rich</t></r></si></sst>'.encode()
    rich = only_cell(inspect_text_fit(synthetic_book(cell_xml='<c r="A1" t="s"><v>0</v></c>', extra={"xl/sharedStrings.xml": shared})))
    assert date["text"] == "46000" and date["risk"] == "unknown"
    assert rich["text"] == "synthetic shared rich" and rich["risk"] == "unknown"
    assert any("显示文本未知" in value for value in date["reasons"])


def test_sparse_declared_dimension_does_not_expand_into_blank_cells():
    result = inspect_text_fit(synthetic_book())
    assert result["summary"]["checked_cells"] == 1
