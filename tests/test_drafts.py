"""Synthetic business facts; native templates are only read into memory."""
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from tradeflow import drafts
from tradeflow.profiles import candidate_profile, read_values
from tradeflow.templates import MAIN


def repack(parts):
    output = BytesIO()
    with ZipFile(output, "w") as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return output.getvalue()


def parts(content):
    with ZipFile(BytesIO(content)) as package:
        return {name: package.read(name) for name in package.namelist()}


@pytest.fixture
def synthetic(monkeypatch):
    strings = {"A6": "Commercial Invoice", "B32": "SYNTHETIC old product", "E32": "SYNTHETIC old specification", "A33": "S.TOTAL", "A34": "G.TOTAL", "A48": "Please old remittance"}
    coordinates = {"A7", "A9", "A11", "A15", "A17", "A19", "A21", "A23", "A25", "B27", "A29", "F32", "G32", "I32", "M32"}
    cells = {key: f'<c r="{key}" t="inlineStr"><is><t>{value}</t></is></c>' for key, value in strings.items()}
    cells.update({key: f'<c r="{key}" t="inlineStr"><is><t>SYNTHETIC old text</t></is></c>' for key in coordinates})
    cells["A15"] = '<c r="A15" t="s"><v>0</v></c>'
    for cell, value in {"A32": 1, "H32": 10, "J32": 10}.items():
        cells[cell] = f'<c r="{cell}"><v>{value}</v></c>'
    for cell, formula in {"K32": "J32*H32", "H33": "SUM(H32:H32)", "K33": "SUM(K32:L32)", "H34": "H33", "K34": "K33"}.items():
        cells[cell] = f'<c r="{cell}"><f>{formula}</f><v>999999</v></c>'
    rows = {}
    for cell, xml in cells.items():
        rows.setdefault(int(''.join(c for c in cell if c.isdigit())), []).append(xml)
    sheet = (f'<worksheet xmlns="{MAIN}"><sheetData>' + ''.join(f'<row r="{row}">{"".join(xml)}</row>' for row, xml in sorted(rows.items())) + '</sheetData></worksheet>').encode()
    pkg = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{drafts.DOC_REL}"><sheets><sheet name="SYNTHETIC" sheetId="1" r:id="rId1"/></sheets></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{drafts.DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'.encode(),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/sharedStrings.xml": f'<sst xmlns="{MAIN}"><si><t>SYNTHETIC historical buyer</t></si></sst>'.encode(),
        "xl/styles.xml": b"SYNTHETIC style bytes must survive",
    }
    raw = repack(pkg)
    monkeypatch.setitem(drafts.KNOWN_TEMPLATES, sha256(raw).hexdigest(), "ci")
    profile = candidate_profile(raw, "ci")
    fields, items = synthetic_inputs(profile)
    return raw, fields, items


def synthetic_inputs(profile):
    fields = {f["field"]: "SYNTHETIC REVIEW DATA" for f in profile["fields"]}
    fields["payment_terms"] = "30% down payment; SYNTHETIC REVIEW DATA"
    fields["trade_terms"] = "EXW; SYNTHETIC REVIEW DATA"
    fields["buyer.address_extra"] = ""
    fields["buyer.contact"] = ""
    items = []
    for index, slot in enumerate(profile["item_slots"], 1):
        item = {key: "SYNTHETIC" for key in slot["cells"]}
        item.update({key: Decimal("2") for key in item if key in drafts.NUMERIC_FIELDS})
        item["number"] = Decimal(index)
        if "unit_price" in item:
            item["unit_price"], item["amount"] = Decimal("3"), Decimal("6")
        if "gross_weight" in item:
            item["gross_weight"] = Decimal("2.5")
        items.append(item)
    return fields, items


def test_recalculates_stale_cache_marks_draft_and_clears_old_strings(synthetic):
    raw, fields, items = synthetic
    generated, report = drafts.generate_review_draft(raw, "ci", fields, items)
    assert report["status"] == "review_draft"
    assert report["format_accepted"] is False
    assert report["business_validation_passed"] is False
    assert report["independent_totals"] == {"quantity": "2", "amount": "6"}
    values = read_values(generated, candidate_profile(raw, "ci")["inspection"])["SYNTHETIC"]
    assert values["K34"] == "6"
    assert "REVIEW DRAFT" in values["A6"]
    assert values["A19"] == ""
    assert b"SYNTHETIC historical buyer" not in generated
    assert b"historical buyer" not in parts(generated)["xl/sharedStrings.xml"]
    assert parts(raw)["xl/styles.xml"] == parts(generated)["xl/styles.xml"]
    assert report["formula_recalculation"]["formula_count"] == 5


@pytest.mark.parametrize("mutation", ["missing_field", "extra_field", "missing_item_field", "wrong_rows", "float", "bool", "amount", "payment", "sequence", "conflicting_payment", "trade_terms", "blank_product"])
def test_rejects_incomplete_or_inconsistent_business_input(synthetic, mutation):
    raw, fields, items = synthetic
    if mutation == "missing_field": fields.pop("buyer.name")
    if mutation == "extra_field": fields["unmapped"] = "SYNTHETIC"
    if mutation == "missing_item_field": items[0].pop("remark")
    if mutation == "wrong_rows": items = []
    if mutation == "float": items[0]["quantity"] = 2.0
    if mutation == "bool": items[0]["quantity"] = True
    if mutation == "amount": items[0]["amount"] = Decimal("7")
    if mutation == "payment": fields["payment_terms"] = "50% down payment"
    if mutation == "sequence": items[0]["number"] = Decimal("2")
    if mutation == "conflicting_payment": fields["payment_terms"] = "30% down payment; 50% down payment"
    if mutation == "trade_terms": fields["trade_terms"] = "CIF SYNTHETIC"
    if mutation == "blank_product": items[0]["description"] = ""
    with pytest.raises(ValueError):
        drafts.generate_review_draft(raw, "ci", fields, items)


def test_rejects_unclassified_images_and_unsupported_documents(synthetic):
    raw, fields, items = synthetic
    pkg = parts(raw)
    pkg["xl/media/unclassified.png"] = b"SYNTHETIC unknown image"
    changed = repack(pkg)
    drafts.KNOWN_TEMPLATES[sha256(changed).hexdigest()] = "ci"
    try:
        with pytest.raises(ValueError, match="未分类媒体"):
            drafts.generate_review_draft(changed, "ci", fields, items)
    finally:
        drafts.KNOWN_TEMPLATES.pop(sha256(changed).hexdigest())
    for document_type in ("receipt", "customs"):
        with pytest.raises(ValueError):
            drafts.generate_review_draft(raw, document_type, fields, items)
    with pytest.raises(ValueError, match="SHA256"):
        drafts.generate_review_draft(raw + b"SYNTHETIC modification", "ci", fields, items)


@pytest.mark.parametrize("formula", [b"J32*H32+1", b"UNSUPPORTED(H32)"])
def test_formula_mismatch_or_unsupported_engine_scope_blocks(synthetic, monkeypatch, formula):
    raw, fields, items = synthetic
    pkg = parts(raw)
    pkg["xl/worksheets/sheet1.xml"] = pkg["xl/worksheets/sheet1.xml"].replace(b"J32*H32", formula)
    changed = repack(pkg)
    monkeypatch.setitem(drafts.KNOWN_TEMPLATES, sha256(changed).hexdigest(), "ci")
    with pytest.raises(ValueError):
        drafts.generate_review_draft(changed, "ci", fields, items)


def test_signature_removal_preserves_logo_and_removes_anchors_and_relationships(monkeypatch):
    stamp, logo = b"SYNTHETIC stamp", b"SYNTHETIC logo"
    monkeypatch.setitem(drafts.MEDIA_CLASSIFICATION, sha256(stamp).hexdigest(), "historical_company_stamp")
    monkeypatch.setitem(drafts.MEDIA_CLASSIFICATION, sha256(logo).hexdigest(), "logo")
    pkg = {
        "xl/media/stamp.png": stamp, "xl/media/logo.png": logo,
        "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="{drafts.XDR}" xmlns:a="{drafts.A}" xmlns:r="{drafts.DOC_REL}"><xdr:twoCellAnchor><a:blip r:embed="old"/></xdr:twoCellAnchor><xdr:oneCellAnchor><a:blip r:embed="logo"/></xdr:oneCellAnchor></xdr:wsDr>'.encode(),
        "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships><Relationship Id="old" Type="{drafts.DOC_REL}/image" Target="../media/stamp.png"/><Relationship Id="logo" Type="{drafts.DOC_REL}/image" Target="../media/logo.png"/></Relationships>'.encode(),
    }
    generated, report = drafts._strip_signatures(repack(pkg))
    result = parts(generated)
    assert "xl/media/stamp.png" not in result
    assert result["xl/media/logo.png"] == logo
    drawing = ET.fromstring(result["xl/drawings/drawing1.xml"])
    assert len(drawing) == 1
    assert "old" not in result["xl/drawings/_rels/drawing1.xml.rels"].decode()
    assert report[0]["classification"] == "historical_company_stamp"
