"""All workbook/email fixtures here are synthetic, not customer documents."""
import hashlib
import io
import zipfile
from email.message import EmailMessage
from xml.sax.saxutils import escape

import pytest

from tradeflow.ingestion import parse_email, parse_erp_xlsx


HEADERS = ["品名", "规格", "数量", "单位", "表色", "等级", "单价", "合成未知列"]
DETAIL = ["合成螺钉", "M2×5", "12345678901234567890.125", "支", "黑锌", "12.9", "0.1008", "合成证据"]


def fixture_xlsx(rows=None, *, sheets=1, formula=None, merges="", shared=False):
    """Minimal ZIP/XML synthetic test fixture, never a deliverable workbook."""
    rows = rows or [HEADERS, DETAIL]
    shared_values = []
    xml_rows = []
    for number, values in enumerate(rows, 1):
        cells = []
        for colnum, raw in enumerate(values):
            if raw is None:
                continue
            ref = chr(65 + colnum) + str(number)
            if ref == formula:
                cells.append(f'<c r="{ref}"><f>1+1</f></c>')
            elif shared:
                shared_values.append(str(raw))
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared_values)-1}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(raw))}</t></is></c>')
        xml_rows.append(f'<row r="{number}">{"".join(cells)}</row>')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="Synthetic{i}" sheetId="{i}" r:id="rId{i}"/>' for i in range(1, sheets+1)) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, sheets+1)) + '</Relationships>')
        for i in range(1, sheets+1):
            archive.writestr(f"xl/worksheets/sheet{i}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(xml_rows) + '</sheetData>' + merges + '</worksheet>')
        if shared:
            archive.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + ''.join(f'<si><t>{escape(v)}</t></si>' for v in shared_values) + '</sst>')
    return stream.getvalue()


def test_txt_utf8_and_gb18030():
    for codec in ("utf-8", "gb18030"):
        raw = "合成询价：请供应 M2×5 螺钉".encode(codec)
        result = parse_email(raw, "synthetic.txt")
        assert result["body"] == "合成询价：请供应 M2×5 螺钉"
        assert result["sha256"] == hashlib.sha256(raw).hexdigest()


def test_html_not_executed_and_attachment_reported():
    msg = EmailMessage()
    msg["Subject"] = "合成询价"
    msg["From"] = "synthetic@example.invalid"
    msg.set_content('<p>合成正文</p><script>evil()</script><style>hidden</style>', subtype="html")
    msg.add_attachment(b"SYNTHETIC DATA", maintype="application", subtype="octet-stream", filename="synthetic.bin")
    result = parse_email(msg.as_bytes(), "synthetic.eml")
    assert result["body"] == "合成正文"
    assert result["subject"] == "合成询价"
    assert result["attachments"][0]["size"] == 14
    assert len(result["warnings"]) == 2


def test_attached_email_not_treated_as_main_body():
    outer = EmailMessage()
    outer.set_content("Synthetic main body")
    inner = EmailMessage()
    inner.set_content("Attached body must not be mixed in")
    outer.add_attachment(inner, filename="synthetic.eml")
    result = parse_email(outer.as_bytes(), "synthetic.eml")
    assert result["body"].strip() == "Synthetic main body"
    assert result["attachments"][0]["content_type"] == "message/rfc822"


@pytest.mark.parametrize("name,content", [("synthetic.pdf", b"x"), ("synthetic.txt", b""), ("synthetic.eml", b"Subject: x\r\n\r\n")])
def test_email_invalid(name, content):
    with pytest.raises(ValueError):
        parse_email(content, name)


@pytest.mark.parametrize("shared", [False, True])
def test_xlsx_decimal_and_evidence(shared):
    content = fixture_xlsx(shared=shared)
    result = parse_erp_xlsx(content, "synthetic.xlsx")
    item = result["items"][0]
    assert item["quantity"] == DETAIL[2]
    assert item["source_cells"]["quantity"] == {"sheet": "Synthetic1", "cell": "C2", "raw": DETAIL[2], "formula": False}
    assert item["extra_fields"]["合成未知列"]["cell"] == "H2"
    assert "line_id" not in item
    assert result["sha256"] == hashlib.sha256(content).hexdigest()
    assert result["warnings"]


def test_multiple_tables_blocked_explicit_mapping_resolves():
    content = fixture_xlsx(sheets=2)
    with pytest.raises(ValueError, match="多个候选"):
        parse_erp_xlsx(content, "synthetic.xlsx")
    mapping = {"sheet": "Synthetic2", "header_row": 1, "columns": dict(zip(("product_name", "specification", "quantity", "unit", "surface_color", "grade", "unit_price"), HEADERS[:7]))}
    assert parse_erp_xlsx(content, "synthetic.xlsx", mapping)["source"]["sheet"] == "Synthetic2"


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-1", "0", "unavailable", ""])
def test_bad_quantity_blocked(quantity):
    row = list(DETAIL)
    row[2] = quantity
    with pytest.raises(ValueError):
        parse_erp_xlsx(fixture_xlsx([HEADERS, row]), "synthetic.xlsx")


def test_duplicate_headers_blocked():
    with pytest.raises(ValueError, match="表头重复"):
        parse_erp_xlsx(fixture_xlsx([HEADERS + ["品名"], DETAIL + ["合成"]]), "synthetic.xlsx")


def test_missing_required_field_blocked():
    with pytest.raises(ValueError, match="缺失关键"):
        parse_erp_xlsx(fixture_xlsx([HEADERS[:5], DETAIL[:5]]), "synthetic.xlsx")


def test_formula_missing_cache_blocked():
    with pytest.raises(ValueError, match="公式没有缓存"):
        parse_erp_xlsx(fixture_xlsx(formula="C2"), "synthetic.xlsx")


def test_merged_data_blocked():
    merges = '<mergeCells><mergeCell ref="A2:B2"/></mergeCells>'
    with pytest.raises(ValueError, match="合并单元格"):
        parse_erp_xlsx(fixture_xlsx(merges=merges), "synthetic.xlsx")


def test_footer_and_sales_title_preserved():
    rows = [["合成公司-销售订单"], HEADERS, DETAIL, [None, None, None, None, None, None, None, "总金额(大写):"], [None, None, None, None, None, None, None, "制单人签名：合成人"]]
    result = parse_erp_xlsx(fixture_xlsx(rows, merges='<mergeCells><mergeCell ref="A4:G4"/></mergeCells>'), "synthetic.xlsx")
    assert len(result["items"]) == 1
    assert result["source"]["document_type"] == "sales_order"
    assert [row["row"] for row in result["source"]["skipped_rows"]] == [4, 5]
    assert any("实际出货" in warning for warning in result["warnings"])


def test_malformed_product_never_silently_skipped():
    row = list(DETAIL)
    row[0] = ""
    row[7] = "备注：合成残缺产品"
    with pytest.raises(ValueError, match="product_name"):
        parse_erp_xlsx(fixture_xlsx([HEADERS, row]), "synthetic.xlsx")


def test_unrecognized_nonempty_footer_blocked():
    with pytest.raises(ValueError):
        parse_erp_xlsx(fixture_xlsx([HEADERS, DETAIL, [None, None, None, None, None, None, None, "无法归属的合成行"]]), "synthetic.xlsx")


@pytest.mark.parametrize("content,name", [(b"bad", "synthetic.xlsx"), (b"bad", "synthetic.xls")])
def test_invalid_workbook(content, name):
    with pytest.raises(ValueError):
        parse_erp_xlsx(content, name)


def test_zip_bomb_blocked():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("synthetic-bomb", b"0" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="压缩比例"):
        parse_erp_xlsx(stream.getvalue(), "synthetic.xlsx")


def test_quantity_only_row_not_discarded_as_note():
    partial = [None, None, "100", "支", None, None, None, "备注：合成残缺品名"]
    with pytest.raises(ValueError, match="product_name"):
        parse_erp_xlsx(fixture_xlsx([HEADERS, DETAIL, partial]), "synthetic.xlsx")


def test_formula_without_cache_in_total_also_blocked():
    total = [None, None, None, None, None, None, "1", "总金额(大写):"]
    with pytest.raises(ValueError, match="公式没有缓存"):
        parse_erp_xlsx(fixture_xlsx([HEADERS, DETAIL, total], formula="G3"), "synthetic.xlsx")


def test_utf16_xml_entity_blocked():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE workbook [<!ENTITY x "synthetic">]><workbook>&x;</workbook>'.encode("utf-16"))
    with pytest.raises(ValueError, match="实体声明"):
        parse_erp_xlsx(stream.getvalue(), "synthetic.xlsx")
