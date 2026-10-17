"""SYNTHETIC table fixtures and read-only optional historical compatibility."""
from copy import deepcopy
from email.message import EmailMessage
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from tradeflow.ingestion import parse_email
from tradeflow.inquiry_document import CONTENT_TYPES, DOCUMENT_CONTENT_TYPE, WORD, parse_inquiry_document
from tradeflow.inquiry_structure import enrich_inquiry_structure
from tradeflow.inquiry_workbook import parse_inquiry_workbook
from tradeflow.templates import DOC_REL, MAIN


def synthetic_mail(html, plain=None):
    message = EmailMessage()
    message['Subject'] = 'SYNTHETIC table inquiry'
    if plain is None:
        message.set_content(html, subtype='html')
    else:
        message.set_content(plain)
        message.add_alternative(html, subtype='html')
    content = message.as_bytes()
    return parse_email(content, 'SYNTHETIC.eml'), content


def table(rows, headers=('Description', 'Reference', 'Supplier', 'Supplier reference', 'Quantity')):
    return '<table><tr>' + ''.join('<th>' + header + '</th>' for header in headers) + '</tr>' + ''.join(
        '<tr>' + ''.join('<td>' + value + '</td>' for value in row) + '</tr>' for row in rows) + '</table>'


def assert_spans(source, enriched):
    assert source['body'] == enriched['body']
    previous = 0
    for row in enriched['analysis_structure']['rows']:
        assert row['start'] >= previous
        assert row['text'] == source['body'][row['start']:row['end']]
        assert row['start'] < row['end']
        previous = row['end']
    json.dumps(enriched, ensure_ascii=False, allow_nan=False)


def test_explicit_html_columns_decimal_original_cells_and_source_hashes():
    source, content = synthetic_mail(table([
        ['SYNTHETIC A', 'SYNTHETIC-01', 'SYNTHETIC SUPPLIER', 'No nylon; M10; coating X', ' 148.00 '],
        ['SYNTHETIC B', 'SYNTHETIC-02', '', 'A-MAGNETIQUE', '222'],
    ]))
    before = deepcopy(source)
    result = enrich_inquiry_structure(source, content)
    assert source == before
    assert_spans(source, result)
    structure = result['analysis_structure']
    assert not structure['blockers']
    assert len(structure['rows']) == 2
    row = structure['rows'][0]
    assert row['row_index'] == 2
    assert row['quantity_text'] == '148.00'
    assert row['quantity_decimal'] == '148.00'
    assert row['reference_text'] == 'SYNTHETIC-01'
    assert 'No nylon; M10; coating X' in row['description_text']
    assert 'SYNTHETIC SUPPLIER' not in row['description_text']
    assert row['cells'][-1]['role'] == 'quantity'
    assert row['cells'][0]['source']['sha256'] == hashlib.sha256(content).hexdigest()
    assert row['cells'][0]['source']['raw_sha256'] == hashlib.sha256(b'SYNTHETIC A').hexdigest()


def test_html_tables_are_used_even_when_original_plain_body_was_preferred():
    html = table([['SYNTHETIC A', 'REF-1', 'SUPPLIER', 'M8 no insert', '444']])
    plain = 'SYNTHETIC greeting\nSYNTHETIC A\n REF-1\n SUPPLIER\n M8 no insert\n 444\nSYNTHETIC closing'
    source, content = synthetic_mail(html, plain)
    result = enrich_inquiry_structure(source, content)
    assert result['body'] == plain + '\n'
    assert not result['analysis_structure']['blockers']
    assert len(result['analysis_structure']['rows']) == 1
    assert_spans(source, result)


def test_french_headers_are_generic_and_reference_columns_preserve_technical_text():
    source, content = synthetic_mail(table([
        ['SYNTHETIC ECROU', 'SYNTHETIC-REF', 'SYNTHETIC', 'sans frein ni embase<br>Acier; classe 8', '296'],
    ], headers=('Désignation', 'Réf. CLIENT-SYNTHETIC', 'Fournisseur', 'Réf. fournisseur', 'Quantité consolidée')))
    result = enrich_inquiry_structure(source, content)
    row = result['analysis_structure']['rows'][0]
    assert not result['analysis_structure']['blockers']
    assert 'sans frein ni embase' in row['description_text']
    assert 'Acier; classe 8' in row['description_text']
    assert [cell['role'] for cell in row['cells']] == ['description', 'reference', 'supplier', 'reference', 'quantity']


def test_duplicate_identical_html_rows_remain_separate_ordered_rows():
    repeated = ['SYNTHETIC A', 'REF', '', 'M8', '10']
    source, content = synthetic_mail(table([repeated, repeated]))
    result = enrich_inquiry_structure(source, content)
    assert len(result['analysis_structure']['rows']) == 2
    assert len({row['id'] for row in result['analysis_structure']['rows']}) == 2
    assert not result['analysis_structure']['blockers']
    assert_spans(source, result)


def test_nested_layout_table_never_absorbs_inner_product_cells():
    inner = table([['SYNTHETIC INNER', 'REF', 'SUPPLIER', 'M8', '74']])
    source, content = synthetic_mail('<table><tr><td>SYNTHETIC layout' + inner + '</td></tr></table>')
    result = enrich_inquiry_structure(source, content)
    assert len(result['analysis_structure']['rows']) == 1
    row = result['analysis_structure']['rows'][0]
    assert row['table_id'].endswith('table-2')
    assert row['cells'][0]['raw_text'] == 'SYNTHETIC INNER'
    assert 'layout' not in row['description_text']


@pytest.mark.parametrize('quantity', ['1,000', '1\n2', '148 PCS', '', 'NaN', '-1', '0', '1e3'])
def test_ambiguous_nondecimal_or_missing_quantity_is_blocked_without_guessing(quantity):
    source, content = synthetic_mail(table([['SYNTHETIC A', 'REF', '', 'M8', quantity]]))
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['rows']
    assert any('unverified_quantity_or_product' in blocker for blocker in result['analysis_structure']['blockers'])


@pytest.mark.parametrize('html', [
    '<table><tr><th>Description</th><th>Qty</th><th>Quantity</th></tr><tr><td>SYNTHETIC</td><td>10</td><td>20</td></tr></table>',
    '<table><tr><th colspan="2">Description</th><th>Qty</th></tr><tr><td>SYNTHETIC</td><td>10</td></tr></table>',
    '<table><tr><th>Description</th><th>Qty</th></tr><tr><td rowspan="2">SYNTHETIC</td><td>10</td></tr><tr><td>20</td></tr></table>',
])
def test_duplicate_quantity_or_merged_cells_block_whole_product_table(html):
    source, content = synthetic_mail(html)
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['rows']
    assert any('ambiguous_header_or_merged_table' in blocker for blocker in result['analysis_structure']['blockers'])


def test_unknown_product_header_does_not_fall_back_to_flat_number_guess():
    source, content = synthetic_mail(table([['SYNTHETIC A', '148']], headers=('Thing', 'Count-unclear')))
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['rows']
    assert any('unmapped_product_table' in blocker for blocker in result['analysis_structure']['blockers'])


def test_plain_html_layout_and_prose_do_not_become_products():
    source, content = synthetic_mail('<p>SYNTHETIC prose 148</p><table><tr><td>From</td><td>SYNTHETIC</td></tr><tr><td>Subject</td><td>RFQ</td></tr></table>')
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['rows']
    assert not result['analysis_structure']['blockers']


@pytest.mark.parametrize('reference_header', ['Item No', 'Part No', 'PN', 'SYNTHETIC PN', '料号'])
def test_explicit_reference_and_description_table_without_quantity_is_not_fabricated(reference_header):
    source, content = synthetic_mail(table([
        ['SYNTHETIC-001', 'SYNTHETIC washer M8', '9.95'],
    ], headers=(reference_header, 'COMPLETE DESCRIPTION', 'Price')))
    result = enrich_inquiry_structure(source, content)
    structure = result['analysis_structure']
    assert not structure['blockers']
    assert len(structure['rows']) == 1
    row = structure['rows'][0]
    assert row['quantity_text'] is None and row['quantity_decimal'] is None
    assert row['source']['quantity_not_provided'] is True
    assert not any(cell['role'] == 'quantity' for cell in row['cells'])
    assert row['reference_text'] == 'SYNTHETIC-001'
    assert row['cells'][-1]['raw_text'] == '9.95' and row['cells'][-1]['role'] == 'other'
    assert any('该表未提供数量列，需业务确认' in warning for warning in structure['warnings'])
    assert_spans(source, result)


def test_quantity_header_with_missing_value_still_blocks_instead_of_missing_column_mode():
    source, content = synthetic_mail(table([['SYNTHETIC A', 'REF-1', '', 'M8', '']]))
    structure = enrich_inquiry_structure(source, content)['analysis_structure']
    assert not structure['rows']
    assert structure['blockers']
    assert not any('该表未提供数量列' in warning for warning in structure['warnings'])


def test_complete_quantity_rows_never_gain_quantity_missing_flag():
    source, content = synthetic_mail(table([['SYNTHETIC A', 'REF-1', '', 'M8', '10']]))
    structure = enrich_inquiry_structure(source, content)['analysis_structure']
    assert not structure['blockers']
    assert structure['rows'][0]['quantity_text'] == '10'
    assert 'quantity_not_provided' not in structure['rows'][0]['source']


def test_hash_mismatch_alignment_failure_and_ambiguous_body_are_explicit_blockers():
    html = table([['SYNTHETIC A', 'REF', '', 'M8', '148']])
    source, content = synthetic_mail(html)
    assert enrich_inquiry_structure(source, content + b'changed')['analysis_structure']['blockers'] == ['original_sha256_mismatch']
    wrong = {**source, 'body': 'SYNTHETIC incompatible plain text'}
    assert any('unaligned_or_ambiguous_body' in value for value in enrich_inquiry_structure(wrong, content)['analysis_structure']['blockers'])
    doubled = {**source, 'body': source['body'] + '\n' + source['body']}
    assert any('unaligned_or_ambiguous_body' in value for value in enrich_inquiry_structure(doubled, content)['analysis_structure']['blockers'])


def zip_parts(parts):
    target = BytesIO()
    with ZipFile(target, 'w', ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value.encode() if isinstance(value, str) else value)
    return target.getvalue()


def synthetic_docx(*, merged=False, no_quantity=False):
    def cell(value, merge=False):
        return '<w:tc>' + ('<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>' if merge else '') + '<w:p><w:r><w:t>' + value + '</w:t></w:r></w:p></w:tc>'
    rows = '<w:tr>' + cell('Description') + cell('Reference') + ('' if no_quantity else cell('Quantity')) + '</w:tr>'
    rows += '<w:tr>' + cell('SYNTHETIC DOCX', merged) + cell('No nylon; M8') + ('' if no_quantity else cell('1258')) + '</w:tr>'
    return zip_parts({'[Content_Types].xml': f'<Types xmlns="{CONTENT_TYPES}"><Override PartName="/word/document.xml" ContentType="{DOCUMENT_CONTENT_TYPE}"/></Types>',
                      'word/document.xml': f'<w:document xmlns:w="{WORD}"><w:body><w:tbl>{rows}</w:tbl></w:body></w:document>'})


def test_docx_table_rows_use_exact_block_locations_and_complete_technical_cells():
    content = synthetic_docx()
    source = parse_inquiry_document(content, 'SYNTHETIC.docx')
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['blockers']
    row = result['analysis_structure']['rows'][0]
    assert row['quantity_text'] == '1258'
    assert row['row_index'] == 2
    assert 'No nylon; M8' in row['description_text']
    assert row['cells'][0]['source']['cell_index'] == 1
    assert row['cells'][0]['source']['part'] == 'word/document.xml'
    assert_spans(source, result)


def test_docx_merged_cell_ambiguity_is_never_silently_flattened():
    content = synthetic_docx(merged=True)
    result = enrich_inquiry_structure(parse_inquiry_document(content, 'SYNTHETIC.docx'), content)
    assert not result['analysis_structure']['rows']
    assert result['analysis_structure']['blockers']


def test_docx_table_without_quantity_binds_description_and_reference():
    content = synthetic_docx(no_quantity=True)
    source = parse_inquiry_document(content, 'SYNTHETIC.docx')
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['blockers']
    row = result['analysis_structure']['rows'][0]
    assert row['quantity_text'] is None and row['quantity_decimal'] is None
    assert row['source']['quantity_not_provided']
    assert_spans(source, result)


def synthetic_xlsx(*, spec_header=False, no_quantity=False):
    def cell(ref, value):
        return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
    rows = '<row r="10">' + cell('A10', 'Description') + cell('B10', 'Reference') + (cell('C10', '规格') if spec_header else '') + cell('D10', 'Price' if no_quantity else 'Quantity') + '</row>'
    rows += '<row r="12">' + cell('A12', 'SYNTHETIC XLSX') + cell('B12', 'M8 no insert') + cell('D12', '444') + '</row>'
    return zip_parts({'[Content_Types].xml': f'<Types xmlns="{CONTENT_TYPES}"/>',
                      'xl/workbook.xml': f'<workbook xmlns="{MAIN}" xmlns:r="{DOC_REL}"><sheets><sheet name="SYNTHETIC" sheetId="1" r:id="rId1"/></sheets></workbook>',
                      'xl/_rels/workbook.xml.rels': f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{DOC_REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
                      'xl/worksheets/sheet1.xml': f'<worksheet xmlns="{MAIN}"><sheetData>{rows}</sheetData></worksheet>'})


def test_xlsx_sparse_columns_and_real_row_numbers_preserved():
    content = synthetic_xlsx()
    source = parse_inquiry_workbook(content, 'SYNTHETIC.xlsx')
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['blockers']
    row = result['analysis_structure']['rows'][0]
    assert row['row_index'] == 12
    assert [cell['column_index'] for cell in row['cells']] == [1, 2, 4]
    assert row['cells'][-1]['source']['cell'] == 'D12'
    assert row['quantity_text'] == '444'
    assert result['analysis_structure']['tables'][0]['header_row_index'] == 10
    assert_spans(source, result)


def test_xlsx_unlabelled_serial_column_and_absent_blank_spec_cell_are_not_ambiguous():
    content = synthetic_xlsx()
    source = parse_inquiry_workbook(content, 'SYNTHETIC.xlsx')
    # SYNTHETIC existing coordinate transcript, matching an intentionally sparse
    # header and additional data serial column like common inquiry sheets.
    source['cells'].append({'sheet': 'SYNTHETIC', 'cell': 'C12', 'raw': 'M8×20',
                            'formula': None, 'formula_attributes': None, 'data_type': 'inlineStr'})
    source['body'] += '\n[SYNTHETIC!C12] 原值：\nM8×20'
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['blockers']
    extra = next(cell for cell in result['analysis_structure']['rows'][0]['cells'] if cell['column_index'] == 3)
    assert extra['role'] == 'other' and extra['header'] == ''
    assert extra['raw_text'] == 'M8×20'


def test_xlsx_missing_quantity_is_never_filled_from_other_numbers():
    content = synthetic_xlsx()
    source = parse_inquiry_workbook(content, 'SYNTHETIC.xlsx')
    source['cells'] = [cell for cell in source['cells'] if cell['cell'] != 'D12']
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['rows']
    assert any('unverified_quantity_or_product' in blocker for blocker in result['analysis_structure']['blockers'])


def test_sparse_known_specification_cell_gets_explicit_empty_source_slot():
    content = synthetic_xlsx(spec_header=True)
    source = parse_inquiry_workbook(content, 'SYNTHETIC.xlsx')
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['blockers']
    cells = result['analysis_structure']['rows'][0]['cells']
    blank_spec = next(cell for cell in cells if cell['column_index'] == 3)
    assert blank_spec['header'] == '规格'
    assert blank_spec['raw_text'] == ''
    assert blank_spec['source']['cell'] == 'C12'
    assert blank_spec['source']['cell_missing_in_source'] is True
    assert_spans(source, result)


def test_partial_valid_table_keeps_good_row_and_explicitly_blocks_bad_row():
    source, content = synthetic_mail(table([
        ['SYNTHETIC good', 'REF-A', '', 'M8', '148'],
        ['SYNTHETIC unknown qty', 'REF-B', '', 'M10', '?'],
    ]))
    result = enrich_inquiry_structure(source, content)['analysis_structure']
    assert len(result['rows']) == 1
    assert len(result['blockers']) == 1




def test_xlsx_unverified_formula_cannot_become_trusted_quantity():
    content = synthetic_xlsx()
    source = parse_inquiry_workbook(content, 'SYNTHETIC.xlsx')
    source['cells'][-1]['formula'] = '1+443'
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['rows']
    assert result['analysis_structure']['blockers']


def test_xlsx_reference_description_price_table_without_quantity():
    content = synthetic_xlsx(no_quantity=True)
    source = parse_inquiry_workbook(content, 'SYNTHETIC.xlsx')
    result = enrich_inquiry_structure(source, content)
    assert not result['analysis_structure']['blockers']
    row = result['analysis_structure']['rows'][0]
    assert row['quantity_text'] is None and row['quantity_decimal'] is None
    assert row['source']['quantity_not_provided']
    assert row['cells'][-1]['role'] == 'other'
    assert_spans(source, result)
