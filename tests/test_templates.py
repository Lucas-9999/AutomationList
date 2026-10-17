"""Synthetic OOXML fixtures only. No customer workbook is written or generated."""
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from tradeflow.templates import MAIN, inspect_template, patch_xlsx_cells


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_entity_declarations_block_in_all_supported_xml_encodings(encoding):
    from tradeflow.templates import _xml
    xml = '<?xml version="1.0" encoding="' + encoding + '"?><!DOCTYPE x [<!ENTITY secret "expanded">]><x>&secret;</x>'
    with pytest.raises(ValueError, match="DTD"):
        _xml(xml.encode(encoding))


@pytest.mark.parametrize("children", ["<f>1+2</f><f>3+4</f><v>0</v>", "<v>1</v><v>2</v>", "<is><t>a</t></is><v>2</v>", "<f>1</f><is><t>a</t></is>"])
def test_ambiguous_cell_contents_fail_inspection(children):
    sheet = f'<worksheet xmlns="{MAIN}"><sheetData><row r="1"><c r="A1">{children}</c></row></sheetData></worksheet>'.encode()
    with pytest.raises(ValueError, match="重复或互斥"):
        inspect_template(synthetic_package(sheet))


SHEET = b'''<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<!-- literal fake cell <c r="A1"><v>do not change</v></c> -->
<dimension ref="A1:XFD1000"/><sheetViews><sheetView workbookViewId="0"/></sheetViews>
<cols><col min="1" max="16384" width="12.75" customWidth="1"/></cols>
<sheetData><row r="1" ht="31.5" customHeight="1">
<c r='A1' s='7' t='s' cm='2'><v>0</v></c>
<c r="B1" s="3"/>
<c r="C1" s="2" t="inlineStr"><is><t>synthetic old customer</t></is></c>
<c r="D1" s="4"><v>100</v></c>
<c r="E1" s="4"><f>D1*2</f><v>200</v></c>
<c r="F1" s="4"><f>'[1]contract'!F42</f><v>300</v></c>
<c r="G1"><f t="shared" si="0" ref="G1:G2">D1</f><v>100</v></c>
</row><row r="2"><c r="G2"><f t="shared" si="0"/><v>100</v></c></row>
<row r="3"><c r="B3" s="1"><v>10</v></c><c r="C3" s="1"/></row></sheetData>
<mergeCells count="1"><mergeCell ref="B3:C3"/></mergeCells>
<printOptions horizontalCentered="1"/><pageMargins left="0.2" right="0.2" top="0.3" bottom="0.3" header="0.1" footer="0.1"/>
<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="1"/>
<drawing r:id="rId1"/></worksheet>'''


def synthetic_package(sheet=SHEET, *, extra=None):
    parts = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": (f'<workbook xmlns="{MAIN}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Synthetic Invoice" sheetId="1" r:id="rId1"/></sheets><calcPr calcId="191029"/></workbook>').encode(),
        "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink" Target="externalLinks/externalLink1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": sheet,
        "xl/sharedStrings.xml": (f'<sst xmlns="{MAIN}"><si><t>Synthetic original buyer</t></si></sst>').encode(),
        "xl/styles.xml": b"synthetic style part byte preservation fixture",
        "xl/worksheets/_rels/sheet1.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/></Relationships>',
        "xl/drawings/drawing1.xml": b"synthetic drawing preservation fixture",
        "xl/media/image1.png": b"synthetic image bytes, not an actual signature or PNG",
        "xl/externalLinks/externalLink1.xml": b"synthetic external link preservation fixture",
        "xl/externalLinks/_rels/externalLink1.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLinkPath" Target="file:///synthetic-old.xlsx" TargetMode="External"/></Relationships>',
        "docProps/custom.xml": b"synthetic custom properties fixture",
    }
    parts.update(extra or {})
    target = BytesIO()
    with ZipFile(target, "w") as package:
        package.comment = b"synthetic package comment"
        for name, payload in parts.items():
            info = ZipInfo(name, (2026, 10, 14, 12, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            info.comment = b"synthetic-entry-comment"
            package.writestr(info, payload)
    return target.getvalue()


def read_parts(content):
    with ZipFile(BytesIO(content)) as package:
        return {info.filename: package.read(info) for info in package.infolist()}


def cells(content):
    root = ET.fromstring(read_parts(content)["xl/worksheets/sheet1.xml"])
    return {cell.get("r"): cell for cell in root.findall(f"{{{MAIN}}}sheetData/{{{MAIN}}}row/{{{MAIN}}}c")}


def test_inspection_is_sparse_and_reports_unverified_caches_and_external_dependencies():
    result = inspect_template(synthetic_package())
    assert len(result["sha256"]) == 64
    sheet = result["sheets"][0]
    assert sheet["dimension"] == "A1:XFD1000"
    assert sheet["cell_count"] == 10
    assert sheet["cells"]["A1"]["style"] == "7"
    assert sheet["merged_ranges"] == ["B3:C3"]
    assert sheet["drawings"] == 1
    assert result["media_parts"] == ["xl/media/image1.png"]
    external = next(formula for formula in sheet["formulas"] if formula["cell"] == "F1")
    assert external["external_reference"]
    assert external["cached_value"] == "300"
    assert external["cache_verified"] is False
    assert result["external_relationships"][0]["target"] == "file:///synthetic-old.xlsx"
    assert result["signature_review_required"] is True
    assert result["formula_recalculation_available"] is False


def test_single_cell_patch_preserves_every_other_part_and_unrelated_worksheet_bytes():
    original = synthetic_package()
    changed = patch_xlsx_cells(original, {"Synthetic Invoice": {"A1": "合成新买方 <&>"}})
    before, after = read_parts(original), read_parts(changed)
    assert before.keys() == after.keys()
    for name in before:
        if name != "xl/worksheets/sheet1.xml":
            assert before[name] == after[name], name
    old_cell = b"<c r='A1' s='7' t='s' cm='2'><v>0</v></c>"
    new_cell = ("<c r='A1' s='7' cm='2' t=\"inlineStr\"><is><t xml:space=\"preserve\">合成新买方 &lt;&amp;&gt;</t></is></c>").encode()
    assert after["xl/worksheets/sheet1.xml"] == before["xl/worksheets/sheet1.xml"].replace(old_cell, new_cell)
    assert cells(changed)["A1"].get("s") == "7"
    assert cells(changed)["A1"].get("cm") == "2"
    assert original == synthetic_package()
    with ZipFile(BytesIO(original)) as old, ZipFile(BytesIO(changed)) as new:
        assert old.comment == new.comment
        assert old.namelist() == new.namelist()
        for old_info, new_info in zip(old.infolist(), new.infolist()):
            assert old_info.date_time == new_info.date_time
            assert old_info.external_attr == new_info.external_attr
            assert old_info.comment == new_info.comment
            assert old_info.compress_type == new_info.compress_type


def test_decimal_int_clear_and_text_formula_values_are_explicit():
    output = patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {
        "B1": Decimal("0.10"), "C1": None, "D1": 300000, "A1": "=SUM(B1:D1)"}})
    result = cells(output)
    assert result["B1"].find(f"{{{MAIN}}}v").text == "0.10"
    assert result["D1"].find(f"{{{MAIN}}}v").text == "300000"
    assert list(result["C1"]) == []
    assert result["C1"].get("t") is None
    assert result["A1"].find(f"{{{MAIN}}}is/{{{MAIN}}}t").text == "=SUM(B1:D1)"
    assert result["A1"].find(f"{{{MAIN}}}f") is None
    # This low-level primitive deliberately does not claim formula caches valid.
    assert result["E1"].find(f"{{{MAIN}}}f").text == "D1*2"
    assert result["E1"].find(f"{{{MAIN}}}v").text == "200"


def test_whitespace_and_carriage_return_preserved_as_text():
    text = "  synthetic\nline\rreturn  "
    output = patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {"B1": text}})
    assert cells(output)["B1"].find(f"{{{MAIN}}}is/{{{MAIN}}}t").text == text


@pytest.mark.parametrize("value", [0.1, True, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), {}, [], "bad\x00text", "bad\ud800text", "x" * 32768], ids=["float", "bool", "nan", "inf", "minus-inf", "object", "list", "nul", "surrogate", "overlong"])
def test_invalid_value_types_and_nonfinite_numbers_fail(value):
    with pytest.raises(ValueError):
        patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {"A1": value}})


@pytest.mark.parametrize("updates", [
    {}, {"Missing Sheet": {"A1": "synthetic"}}, {"Synthetic Invoice": {}},
    {"Synthetic Invoice": {"A99": "synthetic"}}, {"Synthetic Invoice": {"C3": "synthetic"}},
    {"Synthetic Invoice": {"a1": "synthetic"}}, {"Synthetic Invoice": {"XFE1": "synthetic"}},
    {"Synthetic Invoice": {"A1048577": "synthetic"}},
])
def test_unknown_cells_merge_children_and_invalid_mappings_fail(updates):
    with pytest.raises(ValueError):
        patch_xlsx_cells(synthetic_package(), updates)


def test_merge_main_cell_can_be_patched_without_changing_merge():
    output = patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {"B3": "synthetic new value"}})
    assert inspect_template(output)["sheets"][0]["merged_ranges"] == ["B3:C3"]


def test_formula_overwrite_requires_explicit_permission_and_removes_old_formula_cache():
    with pytest.raises(ValueError, match="公式覆盖"):
        patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {"F1": Decimal("123.45")}})
    output = patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {"F1": Decimal("123.45")}}, allow_formula_overwrite=True)
    result = cells(output)["F1"]
    assert result.find(f"{{{MAIN}}}f") is None
    assert result.find(f"{{{MAIN}}}v").text == "123.45"
    assert result.get("s") == "4"


@pytest.mark.parametrize("reference", ["G1", "G2"])
def test_shared_formula_groups_cannot_be_broken_even_with_general_permission(reference):
    with pytest.raises(ValueError, match="共享"):
        patch_xlsx_cells(synthetic_package(), {"Synthetic Invoice": {reference: 1}}, allow_formula_overwrite=True)


def test_prefixed_xml_cells_keep_namespace_prefixes_and_styles():
    sheet = (f'<x:worksheet xmlns:x="{MAIN}"><x:sheetData><x:row r="1"><x:c r="A1" s="9" t="s"><x:v>0</x:v></x:c></x:row></x:sheetData></x:worksheet>').encode()
    original = synthetic_package(sheet)
    output = patch_xlsx_cells(original, {"Synthetic Invoice": {"A1": "synthetic text"}})
    assert b'<x:c r="A1" s="9" t="inlineStr"><x:is><x:t xml:space="preserve">synthetic text</x:t></x:is></x:c>' in read_parts(output)["xl/worksheets/sheet1.xml"]


def test_unrelated_cell_extension_is_preserved_exactly():
    extension = b'<extLst><ext uri="synthetic"><custom xmlns="urn:synthetic">retain exact bytes</custom></ext></extLst>'
    original = synthetic_package(SHEET.replace(b'<c r="D1" s="4"><v>100</v></c>', b'<c r="D1" s="4"><v>100</v>' + extension + b'</c>'))
    output = patch_xlsx_cells(original, {"Synthetic Invoice": {"D1": Decimal("1.25")}})
    assert b'<c r="D1" s="4"><v>1.25</v>' + extension + b'</c>' in read_parts(output)["xl/worksheets/sheet1.xml"]


@pytest.mark.parametrize("content", [b"", b"not zip"])
def test_invalid_packages_fail(content):
    with pytest.raises(ValueError):
        inspect_template(content)


def test_dtd_entities_are_rejected():
    dangerous = b'<!DOCTYPE worksheet [<!ENTITY secret "synthetic">]>' + SHEET.split(b"?>", 1)[1]
    with pytest.raises(ValueError, match="DTD"):
        inspect_template(synthetic_package(dangerous))


def test_duplicate_cell_refs_fail():
    with pytest.raises(ValueError, match="重复"):
        inspect_template(synthetic_package(SHEET.replace(b'<c r="B1" s="3"/>', b'<c r="A1" s="3"/>')))


def test_same_inputs_produce_deterministic_output():
    original = synthetic_package()
    updates = {"Synthetic Invoice": {"A1": "synthetic buyer", "B1": Decimal("30.0")}}
    assert patch_xlsx_cells(original, updates) == patch_xlsx_cells(original, updates)
