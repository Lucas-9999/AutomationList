"""Synthetic fixtures plus optional read-only compatibility with user samples."""
import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import tradeflow.inquiry_workbook as inquiry
from tradeflow.templates import MAIN, DOC_REL


def synthetic_workbook(cells=None, *, extra=None, state="visible", sheet_xml=None):
    """In-memory synthetic OOXML only; never write a customer workbook."""
    if cells is None:
        cells = '<c r="B2" t="inlineStr"><is><t>synthetic 0-80 x 0.187&quot;L</t></is></c><c r="D2"><v>500.000</v></c><c r="E2" t="inlineStr"><is><t>PIECES</t></is></c>'
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets><sheet name="Synthetic RFQ" sheetId="1" state="{state}" r:id="rId1"/></sheets></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'.encode(),
        "xl/worksheets/sheet1.xml": sheet_xml or f'<worksheet xmlns="{MAIN}"><dimension ref="A1:XFD1048576"/><sheetData><row r="2">{cells}</row></sheetData></worksheet>'.encode(),
    }
    parts.update(extra or {})
    target = BytesIO()
    with ZipFile(target, "w", ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return target.getvalue()


def test_sparse_original_text_unit_and_identity_are_preserved():
    source = synthetic_workbook()
    result = inquiry.parse_inquiry_workbook(source, "Synthetic RFQ.xlsx")
    assert result["subject"] == "Synthetic RFQ.xlsx"
    assert result["sender"] == "" and result["message_id"] == ""
    assert result["source_type"] == "inquiry_workbook"
    assert result["sha256"] == hashlib.sha256(source).hexdigest()
    assert len(result["cells"]) == 3
    assert [x["cell"] for x in result["cells"]] == ["B2", "D2", "E2"]
    assert result["cells"][0]["raw"] == 'synthetic 0-80 x 0.187"L'
    assert result["cells"][1]["raw"] == "500.000"
    assert "[Synthetic RFQ!B2]" in result["body"]
    assert 'synthetic 0-80 x 0.187"L' in result["body"]
    assert "PIECES" in result["body"]
    assert result["source"]["sheets"][0]["content_rows"] == [2]
    assert result["source"]["text_analysis_available"] is True
    assert "items" not in result  # No inferred technical rows or quotation.


def test_formula_cache_is_unverified_and_missing_cache_is_explicit():
    cells = '<c r="A2"><f>1+2</f><v>999</v></c><c r="B2"><f t="shared" si="4"/></c>'
    result = inquiry.parse_inquiry_workbook(synthetic_workbook(cells), "synthetic.xlsx")
    assert result["cells"][0]["formula"] == "1+2"
    assert result["cells"][0]["cached_value"] == "999"
    assert result["cells"][0]["cache_verified"] is False
    assert result["cells"][1]["formula_attributes"] == {"t": "shared", "si": "4"}
    assert result["cells"][1]["cached_value"] is None
    assert "未重算" in result["body"] and "未验证" in result["body"]
    assert any("没有缓存" in x for x in result["warnings"])
    assert any("历史值" in x for x in result["warnings"])


def test_image_only_is_not_understood_or_claimed_as_cell_text():
    source = synthetic_workbook("", extra={"xl/media/synthetic.png": b"synthetic image metadata only"})
    result = inquiry.parse_inquiry_workbook(source, "synthetic-image.xlsx")
    assert not result["source"]["text_analysis_available"]
    assert not result["cells"]
    assert result["attachments"][0]["analysis_status"] == "not_analyzed"
    assert result["attachments"][0]["sha256"] == hashlib.sha256(b"synthetic image metadata only").hexdigest()
    assert any("not-analyzed" in x for x in result["warnings"])
    assert "没有可分析的单元格正文" in result["body"]


def test_empty_styled_cells_do_not_expand_but_whitespace_content_remains():
    cells = '<c r="A2"/><c r="B2" t="inlineStr"><is><t xml:space="preserve">  </t></is></c>'
    result = inquiry.parse_inquiry_workbook(synthetic_workbook(cells), "synthetic.xlsx")
    assert len(result["cells"]) == 1
    assert result["cells"][0]["raw"] == "  "


def test_custom_format_unit_and_hidden_sheet_are_retained_without_conversion():
    styles = f'<styleSheet xmlns="{MAIN}"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.00 &quot;MM&quot;"/></numFmts><cellXfs count="1"><xf numFmtId="164"/></cellXfs></styleSheet>'.encode()
    result = inquiry.parse_inquiry_workbook(synthetic_workbook('<c r="A2" s="0"><v>2.50</v></c>', extra={"xl/styles.xml": styles}, state="hidden"), "synthetic.xlsx")
    assert result["cells"][0]["raw"] == "2.50"
    assert result["cells"][0]["number_format"] == '0.00 "MM"'
    assert '0.00 "MM"' in result["body"]
    assert any("隐藏工作表" in x for x in result["warnings"])


def test_error_cell_is_preserved_with_warning_not_validated_as_technical_fact():
    result = inquiry.parse_inquiry_workbook(synthetic_workbook('<c r="A2" t="e"><v>#VALUE!</v></c>'), "synthetic.xlsx")
    assert result["cells"][0]["raw"] == "#VALUE!"
    assert "不能作为技术参数" in result["body"]
    assert any("Excel 错误值" in x for x in result["warnings"])


@pytest.mark.parametrize("filename", ["synthetic.xls", "synthetic.xlsm", "synthetic.csv", "", None])
def test_only_xlsx_is_accepted(filename):
    with pytest.raises(ValueError, match="仅支持"):
        inquiry.parse_inquiry_workbook(synthetic_workbook(), filename)


@pytest.mark.parametrize("content", [b"", b"not zip", "not bytes"])
def test_invalid_input_is_rejected(content):
    with pytest.raises(ValueError):
        inquiry.parse_inquiry_workbook(content, "synthetic.xlsx")


@pytest.mark.parametrize("limit", ["MAX_CONTENT_CELLS", "MAX_BODY_CHARACTERS", "MAX_INPUT_BYTES", "MAX_PARTS", "MAX_PART_BYTES", "MAX_UNCOMPRESSED_BYTES"])
def test_limits_reject_whole_request_without_truncating(monkeypatch, limit):
    monkeypatch.setattr(inquiry, limit, 1)
    with pytest.raises(ValueError, match="上限|超限"):
        inquiry.parse_inquiry_workbook(synthetic_workbook(), "synthetic.xlsx")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_xml_entity_expansion_is_rejected(encoding):
    xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE worksheet [<!ENTITY injected "synthetic">]><worksheet xmlns="{MAIN}"><sheetData><row r="2"><c r="A2" t="inlineStr"><is><t>&injected;</t></is></c></row></sheetData></worksheet>'
    with pytest.raises(ValueError, match="DTD"):
        inquiry.parse_inquiry_workbook(synthetic_workbook(sheet_xml=xml.encode(encoding)), "synthetic.xlsx")


def test_unsafe_zip_paths_and_duplicate_cells_are_rejected():
    with pytest.raises(ValueError, match="非法部件路径"):
        inquiry.parse_inquiry_workbook(synthetic_workbook(extra={"../synthetic.txt": b"bad"}), "synthetic.xlsx")
    with pytest.raises(ValueError, match="重复"):
        inquiry.parse_inquiry_workbook(synthetic_workbook('<c r="A2"><v>1</v></c><c r="A2"><v>2</v></c>'), "synthetic.xlsx")


def test_external_link_is_never_opened_and_is_flagged():
    relations = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" Type="synthetic" Target="https://example.invalid/synthetic" TargetMode="External"/></Relationships>'
    result = inquiry.parse_inquiry_workbook(synthetic_workbook(extra={"xl/worksheets/_rels/sheet1.xml.rels": relations}), "synthetic.xlsx")
    assert result["source"]["external_relationships"][0]["target"] == "https://example.invalid/synthetic"
    assert any("外部关系" in x for x in result["warnings"])


def test_compression_bomb_ratio_is_blocked_before_xml_read():
    source = synthetic_workbook(extra={"xl/synthetic.bin": b"x" * (1024 * 1024 + 1)})
    with pytest.raises(ValueError, match="压缩比"):
        inquiry.parse_inquiry_workbook(source, "synthetic.xlsx")


def test_vml_text_drawing_is_explicitly_not_analyzed():
    xml = f'<worksheet xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheetData/><legacyDrawing r:id="rId4"/></worksheet>'.encode()
    result = inquiry.parse_inquiry_workbook(synthetic_workbook(sheet_xml=xml), "synthetic.xlsx")
    assert result["source"]["sheets"][0]["legacy_drawing_count"] == 1
    assert any("not-analyzed" in x for x in result["warnings"])
