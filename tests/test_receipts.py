"""Synthetic receipt facts; native source workbooks are read only into memory."""
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from tradeflow import receipts
from tradeflow.profiles import read_values
from tradeflow.templates import DOC_REL, MAIN


def pack(parts):
    target = BytesIO()
    with ZipFile(target, "w") as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return target.getvalue()


def unpack(content):
    with ZipFile(BytesIO(content)) as package:
        return {name: package.read(name) for name in package.namelist()}


def synthetic_inputs(profile):
    fields = {field["field"]: "SYNTHETIC TEST DATA" for field in profile["fields"]}
    fields["payment_terms"] = "30% down payment; SYNTHETIC TEST DATA"
    fields["trade_terms"] = "EXW; SYNTHETIC TEST DATA"
    fields["buyer.address_extra"] = ""
    fields["buyer.contact"] = ""
    items = []
    for number, slot in enumerate(profile["item_slots"], 1):
        item = {field: "SYNTHETIC TEST DATA" for field in slot["cells"]}
        item.update({field: Decimal(2) for field in receipts.NUMERIC})
        item.update(number=Decimal(number), gross_weight=Decimal("2.5"), carton_numbers="")
        items.append(item)
    return fields, items


@pytest.fixture
def synthetic(monkeypatch):
    text = {"A1": "SYNTHETIC SELLER", "A7": receipts.TITLE, "A8": "SYNTHETIC OLD NUMBER",
            "A10": "SYNTHETIC OLD ISSUE DATE", "A12": "SYNTHETIC OLD VALIDITY", "A16": "SYNTHETIC OLD BUYER",
            "A18": "SYNTHETIC OLD ADDRESS", "A20": "SYNTHETIC OLD EXTRA", "A22": "SYNTHETIC OLD CONTACT",
            "A24": "SYNTHETIC OLD CONSIGNEE", "A26": "EXW", "B28": "50% down payment",
            "A30": "SYNTHETIC OLD DELIVERY", "B33": "SYNTHETIC OLD PRODUCT", "C33": "SYNTHETIC OLD SPEC",
            "E33": "PCS", "I33": "SYNTHETIC OLD CARTON", "A34": "G.TOTAL 总计", "B45": "Received by (收货人)",
            "H45": "SYNTHETIC OLD SIGNING DATE"}
    cells = {cell: f'<c r="{cell}" t="inlineStr"><is><t>{value.replace("&", "&amp;")}</t></is></c>' for cell, value in text.items()}
    cells["D45"] = '<c r="D45" t="s"><v>0</v></c>'
    for cell in ("A33", "D33", "F33", "G33", "H33"):
        cells[cell] = f'<c r="{cell}"><v>999</v></c>'
    for cell, formula in {"D34": "D33", "F34": "SUM(F33)", "G34": "SUM(G33)"}.items():
        cells[cell] = f'<c r="{cell}"><f>{formula}</f><v>99999</v></c>'
    rows = {}
    for cell, xml in cells.items():
        row = int(''.join(char for char in cell if char.isdigit()))
        rows.setdefault(row, []).append(xml)
    sheet = (f'<worksheet xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheetData>' + ''.join(f'<row r="{row}">{"".join(values)}</row>' for row, values in sorted(rows.items())) + '</sheetData><drawing r:id="drawing"/></worksheet>').encode()
    logo, signature = b"SYNTHETIC logo bytes", b"SYNTHETIC signature bytes"
    monkeypatch.setitem(receipts.MEDIA_CLASSIFICATION, sha256(logo).hexdigest(), "logo")
    monkeypatch.setitem(receipts.MEDIA_CLASSIFICATION, sha256(signature).hexdigest(), "historical_customer_signature")
    pkg = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "xl/workbook.xml": f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets><sheet name="PL" sheetId="1" r:id="sheet"/></sheets></workbook>'.encode(),
        "xl/_rels/workbook.xml.rels": f'<Relationships><Relationship Id="sheet" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'.encode(),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/sharedStrings.xml": f'<sst xmlns="{MAIN}"><si><t>SYNTHETIC HISTORICAL RECEIVER NAME</t></si></sst>'.encode(),
        "xl/styles.xml": b"SYNTHETIC exact style bytes",
        "xl/media/logo.jpeg": logo, "xl/media/signature.png": signature,
        "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships><Relationship Id="drawing" Type="{DOC_REL}/drawing" Target="../drawings/drawing1.xml"/></Relationships>'.encode(),
        "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="{DOC_REL}"><xdr:twoCellAnchor><a:blip r:embed="logo"/></xdr:twoCellAnchor><xdr:twoCellAnchor><a:blip r:embed="signature"/></xdr:twoCellAnchor></xdr:wsDr>'.encode(),
        "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships><Relationship Id="logo" Type="{DOC_REL}/image" Target="../media/logo.jpeg"/><Relationship Id="signature" Type="{DOC_REL}/image" Target="../media/signature.png"/></Relationships>'.encode(),
    }
    raw = pack(pkg)
    monkeypatch.setitem(receipts.KNOWN_RECEIPTS, sha256(raw).hexdigest(), {"first_row": 33, "line_capacity": 1, "total_row": 34, "signature_row": 45, "layout": "contract_header"})
    profile = receipts.inspect_receipt_template(raw)
    fields, items = synthetic_inputs(profile)
    return raw, fields, items


def test_signed_history_is_removed_and_receipt_remains_unsigned(synthetic):
    raw, fields, items = synthetic
    generated, report = receipts.generate_receipt_draft(raw, fields, items)
    assert report["status"] == "review_draft"
    assert report["format_accepted"] is False
    assert report["received_confirmed"] is False
    assert report["receipt_status"] == "awaiting_signature"
    assert report["received_by"] is None and report["signed_at"] is None and report["signature_evidence"] is None
    assert report["unsigned"] is True
    assert report["independent_totals"] == {"quantity": "2", "net_weight": "2", "gross_weight": "2.5"}
    profile = receipts.inspect_receipt_template(raw)
    values = read_values(generated, profile["inspection"])["PL"]
    assert "REVIEW DRAFT" in values["A7"] and "待审阅" in values["A7"]
    assert values["D45"] == "" and values["H45"] == ""
    assert values["B45"] == "Received by (收货人)"
    assert values["A20"] == "" and values["D34"] == "2"
    pkg = unpack(generated)
    assert "xl/media/signature.png" not in pkg
    assert pkg["xl/media/logo.jpeg"] == unpack(raw)["xl/media/logo.jpeg"]
    assert b"SYNTHETIC HISTORICAL RECEIVER NAME" not in pkg["xl/sharedStrings.xml"]
    assert len(ET.fromstring(pkg["xl/drawings/drawing1.xml"])) == 1
    assert pkg["xl/styles.xml"] == unpack(raw)["xl/styles.xml"]
    assert report["formula_recalculation"]["formula_count"] == 3


@pytest.mark.parametrize("mutation", ["missing_field", "signer", "signed_date", "received", "missing_item", "no_rows", "extra_rows", "wrong_number", "gross_less_than_net", "payment", "trade"])
def test_missing_or_receipt_claim_injection_and_inconsistent_input_block(synthetic, mutation):
    raw, fields, items = synthetic
    if mutation == "missing_field": fields.pop("buyer.name")
    if mutation == "signer": fields["received_by"] = "SYNTHETIC person"
    if mutation == "signed_date": fields["signed_at"] = "SYNTHETIC date"
    if mutation == "received": fields["received_confirmed"] = "true"
    if mutation == "missing_item": items[0].pop("carton_numbers")
    if mutation == "no_rows": items = []
    if mutation == "extra_rows": items.append(items[0].copy())
    if mutation == "wrong_number": items[0]["number"] = Decimal(2)
    if mutation == "gross_less_than_net": items[0]["gross_weight"] = Decimal(1)
    if mutation == "payment": fields["payment_terms"] = "50% down payment"
    if mutation == "trade": fields["trade_terms"] = "CIF"
    with pytest.raises(ValueError):
        receipts.generate_receipt_draft(raw, fields, items)


@pytest.mark.parametrize("value", [2.0, True, 2, "2", Decimal("NaN"), Decimal("Infinity"), Decimal(-1), Decimal(0)])
def test_quantity_requires_exact_positive_decimal(synthetic, value):
    raw, fields, items = synthetic
    items[0]["quantity"] = value
    with pytest.raises(ValueError):
        receipts.generate_receipt_draft(raw, fields, items)


def test_unknown_template_is_inspectable_but_not_generatable(synthetic):
    raw, fields, items = synthetic
    changed = raw + b"SYNTHETIC modification"
    profile = receipts.inspect_receipt_template(changed)
    assert not profile["fields"] and profile["blockers"]
    with pytest.raises(ValueError, match="SHA256"):
        receipts.generate_receipt_draft(changed, fields, items)


@pytest.mark.parametrize("change", ["unknown_media", "unsupported_formula", "wrong_total_formula"])
def test_media_and_calculation_scope_fail_closed(synthetic, monkeypatch, change):
    raw, fields, items = synthetic
    pkg = unpack(raw)
    if change == "unknown_media":
        pkg["xl/media/unknown.png"] = b"SYNTHETIC unknown signature?"
    else:
        formula = b"UNSUPPORTED(D33)" if change == "unsupported_formula" else b"D33+1"
        pkg["xl/worksheets/sheet1.xml"] = pkg["xl/worksheets/sheet1.xml"].replace(b"<f>D33</f>", b"<f>" + formula + b"</f>")
    changed = pack(pkg)
    monkeypatch.setitem(receipts.KNOWN_RECEIPTS, sha256(changed).hexdigest(), receipts.KNOWN_RECEIPTS[sha256(raw).hexdigest()])
    with pytest.raises(ValueError):
        receipts.generate_receipt_draft(changed, fields, items)
