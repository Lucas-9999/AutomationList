"""Customs review drafts are tested with explicitly synthetic declarations."""
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from tradeflow import customs
from tradeflow.profiles import read_values
from tradeflow.templates import DOC_REL, MAIN, inspect_template


def pack(parts):
    out = BytesIO()
    with ZipFile(out, "w") as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return out.getvalue()


def unpack(content):
    with ZipFile(BytesIO(content)) as package:
        return {name: package.read(name) for name in package.namelist()}


def synthetic_inputs(profile):
    fields = {field["field"]: "SYNTHETIC TEST DATA" for field in profile["fields"]}
    fields.update({name: Decimal(2) for name in customs.DECIMAL_HEADERS})
    fields.update(package_count=Decimal(1), gross_weight=Decimal("2.5"), miscellaneous_fees=Decimal(0),
                  miscellaneous_fees_currency="USD", incoterm="EXW", export_date_text="", declaration_date_text="")
    item = {"line_id": "SYNTHETIC-LINE", "number": Decimal(1), "hs_code": "0000000000",
            "description": "SYNTHETIC TEST GOODS", "quantity": Decimal(3), "unit": "PCS",
            "legal_quantity": Decimal(2), "legal_unit": "KG", "unit_price": Decimal(4),
            "amount": Decimal(12), "currency": "USD", "origin_country": "SYNTHETIC ORIGIN",
            "destination_country": "SYNTHETIC TEST DATA", "domestic_origin": "SYNTHETIC PLACE",
            "levy_method": "SYNTHETIC EXPLICIT INPUT", "declaration_elements_text": "SYNTHETIC EXPLICIT DECLARATION ELEMENTS"}
    return fields, [item]


@pytest.fixture
def synthetic(monkeypatch):
    coordinates = {"A3", "C3", "F3", "I3", "A5", "C5", "A6", "C6", "F6", "A8", "C8", "F8", "I8", "A10", "C10", "D10", "F10", "H10", "M10", "A12", "A14", "B14", "C14", "E14", "G14", "H14", "I14", "J14", "L14", "M14", "A15", "B16"}
    texts = {cell: "SYNTHETIC OLD FACT" for cell in coordinates}
    texts["A1"] = "中华人民共和国海关出口货物报关单"
    texts["B14"] = "73181510.9"
    texts["B16"] = "SYNTHETIC HISTORICAL BRAND/PREFERENCE ASSERTION"
    cells = {cell: f'<c r="{cell}" t="inlineStr"><is><t>{value}</t></is></c>' for cell, value in texts.items()}
    cells["A5"] = '<c r="A5" t="s"><v>0</v></c>'
    for cell, formula in {"C8": "F8", "F8": "[1]合同!F42", "O15": "SUM(O14:O14)", "Q15": "SUM(Q14:Q14)"}.items():
        cells[cell] = f'<c r="{cell}" t="str"><f>{formula}</f><v>SYNTHETIC STALE COUNTRY OR ZERO</v></c>'
    rows = {}
    for cell, xml in cells.items():
        row = int(''.join(char for char in cell if char.isdigit()))
        rows.setdefault(row, []).append(xml)
    sheet = (f'<worksheet xmlns="{MAIN}"><sheetData>' + ''.join(f'<row r="{row}">{"".join(values)}</row>' for row, values in sorted(rows.items())) + '</sheetData></worksheet>').encode()
    pkg = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="wmf" ContentType="image/x-wmf"/><Override PartName="/xl/externalLinks/externalLink1.xml" ContentType="external-synthetic"/><Override PartName="/docProps/custom.xml" ContentType="custom-synthetic"/></Types>',
        "_rels/.rels": f'<Relationships><Relationship Id="thumb" Type="{DOC_REL}/metadata/thumbnail" Target="docProps/thumbnail.wmf"/><Relationship Id="custom" Type="{DOC_REL}/custom-properties" Target="docProps/custom.xml"/></Relationships>'.encode(),
        "docProps/thumbnail.wmf": b"SYNTHETIC OLD CUSTOMER PREVIEW",
        "docProps/core.xml": f'<cp:coreProperties xmlns:cp="{customs.CORE}" xmlns:dc="{customs.DC}" xmlns:dcterms="{customs.DCTERMS}"><dc:creator>SYNTHETIC OLD AUTHOR</dc:creator><cp:lastModifiedBy>SYNTHETIC OLD EDITOR</cp:lastModifiedBy><dcterms:created>SYNTHETIC OLD CREATION TIME</dcterms:created><dcterms:modified>SYNTHETIC OLD MODIFIED TIME</dcterms:modified><cp:lastPrinted>SYNTHETIC OLD PRINT TIME</cp:lastPrinted><cp:revision>2</cp:revision><dc:description>SYNTHETIC GENERIC TEMPLATE DESCRIPTION</dc:description></cp:coreProperties>'.encode(),
        "docProps/custom.xml": b'<Properties><property name="ICV">SYNTHETIC HISTORICAL ICV</property></Properties>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}" xmlns:mc="urn:synthetic-mc" xmlns:untouched="urn:synthetic-prefix" mc:Ignorable="untouched"><sheets><sheet name="报关单" sheetId="1" r:id="sheet"/></sheets><externalReferences><externalReference r:id="external"/></externalReferences></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": f'<Relationships><Relationship Id="sheet" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="external" Type="{DOC_REL}/externalLink" Target="externalLinks/externalLink1.xml"/></Relationships>'.encode(),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/sharedStrings.xml": f'<sst xmlns="{MAIN}"><si><t>SYNTHETIC OLD BUYER NAME</t></si></sst>'.encode(),
        "xl/styles.xml": b"SYNTHETIC STYLE BYTES MUST SURVIVE",
        "xl/externalLinks/externalLink1.xml": f'<externalLink xmlns="{MAIN}"><externalBook><sheetDataSet><sheetData><row r="42"><cell r="F42"><v>SYNTHETIC CACHED OLD COUNTRY</v></cell></row></sheetData></sheetDataSet></externalBook></externalLink>'.encode(),
        "xl/externalLinks/_rels/externalLink1.xml.rels": f'<Relationships><Relationship Id="old" Type="{DOC_REL}/externalLinkPath" Target="file:///SYNTHETIC-OLD-2019.xlsx" TargetMode="External"/></Relationships>'.encode(),
    }
    raw = pack(pkg)
    monkeypatch.setattr(customs, "KNOWN_CUSTOMS", customs.KNOWN_CUSTOMS | {sha256(raw).hexdigest()})
    fields, items = synthetic_inputs(customs.inspect_customs_template(raw))
    return raw, fields, items


def test_explicit_facts_replace_history_and_all_external_dependencies(synthetic):
    raw, fields, items = synthetic
    fields["trade_country"] = "SYNTHETIC DIFFERENT TRADE COUNTRY"
    generated, report = customs.generate_customs_draft(raw, fields, items)
    assert report["status"] == "review_draft" and report["submitted"] is False
    assert report["format_accepted"] is False and report["business_validation_passed"] is False
    assert report["source_line_ids"] == ["SYNTHETIC-LINE"] and report["aggregation_performed"] is False
    assert report["external_dependency_proof"] == {"external_link_parts": 0, "external_relationships": 0, "workbook_external_references": 0, "external_formulas": 0}
    pkg = unpack(generated)
    assert not any(name.startswith("xl/externalLinks/") for name in pkg)
    assert "docProps/thumbnail.wmf" not in pkg
    assert b"SYNTHETIC OLD BUYER NAME" not in pkg["xl/sharedStrings.xml"]
    assert b'xmlns:untouched="urn:synthetic-prefix"' in pkg["xl/workbook.xml"]
    assert pkg["xl/styles.xml"] == unpack(raw)["xl/styles.xml"]
    inspection = inspect_template(generated)
    values = read_values(generated, inspection)["报关单"]
    assert values["F8"] == "SYNTHETIC TEST DATA"
    assert values["C8"] == "SYNTHETIC DIFFERENT TRADE COUNTRY"
    assert values["E14"] == "2 KG\n3 PCS" and values["H14"] == "12 USD"
    assert values["A15"] == "" and values["B16"] == "SYNTHETIC EXPLICIT DECLARATION ELEMENTS"
    assert "待审阅（未提交）" in values["A1"]
    assert report["formula_recalculation"]["formula_count"] == 2
    assert report["visual_acceptance"]["artifact_renderer_accepted"] is False
    assert report["visual_acceptance"]["native_excel_accepted"] is False


@pytest.mark.parametrize("currency", ["EUR", "CNY", "KRW", "GBP"])
def test_fixed_usd_unit_price_style_rejects_other_goods_currencies(synthetic, currency):
    raw, fields, items = synthetic
    assert customs.inspect_customs_template(raw)["fixed_context"]["currency"] == "USD"
    items[0]["currency"] = currency
    with pytest.raises(ValueError, match="固定 USD"):
        customs.generate_customs_draft(raw, fields, items)


def test_historical_identity_times_and_custom_properties_removed_deterministically(synthetic):
    raw, fields, items = synthetic
    generated, report = customs.generate_customs_draft(raw, fields, items)
    repeated, repeated_report = customs.generate_customs_draft(raw, fields, items)
    assert generated == repeated and report == repeated_report
    pkg = unpack(generated)
    assert "docProps/custom.xml" not in pkg
    assert b"custom-properties" not in pkg["_rels/.rels"]
    assert b"docProps/custom.xml" not in pkg["[Content_Types].xml"]
    core = ET.fromstring(pkg["docProps/core.xml"])
    assert {node.tag.split("}")[-1] for node in core} == {"revision", "description"}
    assert b"SYNTHETIC GENERIC TEMPLATE DESCRIPTION" in pkg["docProps/core.xml"]
    assert {node["node"].split("}")[-1] for node in report["removed_metadata_nodes"]} == {"creator", "lastModifiedBy", "created", "modified", "lastPrinted"}
    assert all(len(node["value_sha256"]) == 64 for node in report["removed_metadata_nodes"])
    assert report["removed_metadata_parts"] == [{"part": "docProps/custom.xml", "sha256": sha256(unpack(raw)["docProps/custom.xml"]).hexdigest(), "classification": "historical_custom_properties"}]
    assert pkg["xl/styles.xml"] == unpack(raw)["xl/styles.xml"]


def test_nonempty_title_or_subject_requires_classification(synthetic, monkeypatch):
    raw, fields, items = synthetic
    pkg = unpack(raw)
    pkg["docProps/core.xml"] = pkg["docProps/core.xml"].replace(b"</cp:coreProperties>", b"<dc:title>SYNTHETIC HISTORICAL CUSTOMER ORDER</dc:title></cp:coreProperties>")
    changed = pack(pkg)
    monkeypatch.setattr(customs, "KNOWN_CUSTOMS", customs.KNOWN_CUSTOMS | {sha256(changed).hexdigest()})
    with pytest.raises(ValueError, match="核心标题或主题"):
        customs.generate_customs_draft(changed, fields, items)


@pytest.mark.parametrize("mutation", ["missing_header", "missing_item", "extra_header", "missing_hs", "old_dotted_hs", "missing_levy", "bad_amount", "bad_legal_quantity", "bad_destination", "bad_unit", "bad_currency", "bad_incoterm", "bad_package_count", "missing_elements", "negative_weight"])
def test_unknown_missing_or_inconsistent_facts_block(synthetic, mutation):
    raw, fields, items = synthetic
    if mutation == "missing_header": fields.pop("supervision_mode_text")
    if mutation == "missing_item": items[0].pop("origin_country")
    if mutation == "extra_header": fields["submitted"] = "true"
    if mutation == "missing_hs": items[0]["hs_code"] = ""
    if mutation == "old_dotted_hs": items[0]["hs_code"] = "73181510.9"
    if mutation == "missing_levy": items[0]["levy_method"] = ""
    if mutation == "bad_amount": items[0]["amount"] = Decimal(13)
    if mutation == "bad_legal_quantity": items[0]["legal_quantity"] = Decimal(3)
    if mutation == "bad_destination": items[0]["destination_country"] = "SYNTHETIC DIFFERENT"
    if mutation == "bad_unit": items[0]["legal_unit"] = "SYNTHETIC-UNIT"
    if mutation == "bad_currency": items[0]["currency"] = ""
    if mutation == "bad_incoterm": fields["incoterm"] = "SYNTHETIC"
    if mutation == "bad_package_count": fields["package_count"] = Decimal("1.5")
    if mutation == "missing_elements": items[0]["declaration_elements_text"] = ""
    if mutation == "negative_weight": fields["gross_weight"] = Decimal(-1)
    with pytest.raises(ValueError):
        customs.generate_customs_draft(raw, fields, items)


@pytest.mark.parametrize("rows", [0, 2, 11])
def test_no_empty_aggregation_omission_or_truncation(synthetic, rows):
    raw, fields, items = synthetic
    with pytest.raises(ValueError, match="一条源商品"):
        customs.generate_customs_draft(raw, fields, items * rows)


@pytest.mark.parametrize("value", [3.0, True, 3, "3", Decimal("NaN"), Decimal("Infinity"), Decimal(0)])
def test_quantity_is_positive_decimal(synthetic, value):
    raw, fields, items = synthetic
    items[0]["quantity"] = value
    with pytest.raises(ValueError):
        customs.generate_customs_draft(raw, fields, items)


def test_unbound_template_cannot_generate(synthetic):
    raw, fields, items = synthetic
    changed = raw + b"SYNTHETIC CHANGED TEMPLATE VERSION"
    assert customs.inspect_customs_template(changed)["generation_supported"] is False
    with pytest.raises(ValueError, match="SHA256"):
        customs.generate_customs_draft(changed, fields, items)
