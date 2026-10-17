"""SYNTHETIC in-memory Word fixtures; no user documents are created or changed."""
import hashlib
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

import tradeflow.inquiry_document as inquiry

W = inquiry.WORD


def synthetic_document(body=None, *, extra=None, document_xml=None, content_type=None):
    if body is None:
        body = '<w:p><w:r><w:t>SYNTHETIC RFQ M1.6×4</w:t></w:r></w:p>'
    parts = {
        '[Content_Types].xml': f'<Types xmlns="{inquiry.CONTENT_TYPES}"><Override PartName="/word/document.xml" ContentType="{content_type or inquiry.DOCUMENT_CONTENT_TYPE}"/></Types>'.encode(),
        'word/document.xml': document_xml or f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'.encode(),
    }
    parts.update(extra or {})
    out = BytesIO()
    with ZipFile(out, 'w', ZIP_DEFLATED) as package:
        for name, value in parts.items():
            package.writestr(name, value)
    return out.getvalue()


def p(text):
    return '<w:p><w:r><w:t>' + text + '</w:t></w:r></w:p>'


def test_original_runs_tabs_breaks_and_identity_preserved():
    content = synthetic_document('<w:p><w:r><w:t xml:space="preserve"> SYNTHETIC </w:t></w:r><w:r><w:t>M1.6</w:t><w:tab/><w:t>12.9</w:t><w:br/><w:t>black zinc</w:t><w:cr/></w:r></w:p>')
    result = inquiry.parse_inquiry_document(content, r'C:\SYNTHETIC\RFQ.DOCX')
    assert result['subject'] == 'RFQ.DOCX'
    assert result['sender'] == result['message_id'] == ''
    assert result['sha256'] == hashlib.sha256(content).hexdigest()
    assert result['source_type'] == 'inquiry_document'
    assert result['blocks'][0]['raw'] == ' SYNTHETIC M1.6\t12.9\nblack zinc\n'
    assert result['blocks'][0]['part'] == 'word/document.xml'
    assert result['blocks'][0]['paragraph_index'] == 1
    assert result['source']['text_analysis_available'] is True
    assert not result['source']['analysis_blockers']
    assert 'items' not in result


def test_paragraphs_tables_repeated_and_nested_cells_keep_order_and_locations():
    table = '<w:tbl><w:tr><w:tc>' + p('SYNTHETIC screw') + p('Grade 12.9') + '</w:tc><w:tc>' + p('300000 PCS') + '</w:tc></w:tr></w:tbl>'
    nested = '<w:tbl><w:tr><w:tc>' + p('outer') + table + p('after inner') + '</w:tc></w:tr></w:tbl>'
    result = inquiry.parse_inquiry_document(synthetic_document(p('before') + table + p('between') + table + nested + p('after')), 'SYNTHETIC.docx')
    assert [b['raw'] for b in result['blocks']] == ['before', 'SYNTHETIC screw', 'Grade 12.9', '300000 PCS', 'between', 'SYNTHETIC screw', 'Grade 12.9', '300000 PCS', 'outer', 'SYNTHETIC screw', 'Grade 12.9', '300000 PCS', 'after inner', 'after']
    assert len(result['source']['tables']) == 4
    second = result['blocks'][1]
    assert (second['table_index'], second['row_index'], second['cell_index']) == (1, 1, 1)
    assert '/tbl[' in second['path'] and '/tr[1]/tc[1]/p[1]' in second['path']
    assert result['blocks'][5]['table_index'] == 2
    assert result['blocks'][9]['table_index'] == 4
    assert result['source']['text_analysis_available']


@pytest.mark.parametrize('feature,reason', [
    ('<w:fldSimple w:instr="SYNTHETIC"><w:r><w:t>999</w:t></w:r></w:fldSimple>', 'unverified_fields'),
    ('<w:r><w:fldChar w:fldCharType="begin"/><w:instrText>SYNTHETIC</w:instrText></w:r>', 'unverified_fields'),
    ('<w:ins><w:r><w:t>new</w:t></w:r></w:ins><w:del><w:r><w:delText>old</w:delText></w:r></w:del>', 'tracked_revisions'),
    ('<w:r><w:rPr><w:vanish/></w:rPr><w:t>hidden</w:t></w:r>', 'hidden_text'),
    ('<w:r><w:sym w:font="Symbol" w:char="F02D"/></w:r>', 'unparsed_symbol'),
    ('<w:r><w:footnoteReference w:id="4"/></w:r>', 'unparsed_supplementary_text'),
    ('<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>', 'unparsed_numbering'),
    ('<w:r><w:drawing><w:txbxContent>' + p('textbox') + '</w:txbxContent></w:drawing></w:r>', 'unparsed_textbox'),
])
def test_ambiguous_word_features_block_analysis_but_keep_auditable_source(feature, reason):
    result = inquiry.parse_inquiry_document(synthetic_document('<w:p><w:r><w:t>SYNTHETIC</w:t></w:r>' + feature + '</w:p>'), 'SYNTHETIC.docx')
    assert not result['source']['text_analysis_available']
    assert reason in result['source']['analysis_blockers']
    assert result['source']['features']
    assert 'SYNTHETIC' in result['body']
    if reason == 'tracked_revisions':
        assert 'newold' in result['blocks'][0]['raw']
    if reason == 'unparsed_textbox':
        assert 'textbox' not in result['blocks'][0]['raw']


@pytest.mark.parametrize('part', ['word/header1.xml', 'word/footer1.xml', 'word/comments.xml', 'word/footnotes.xml', 'word/endnotes.xml'])
def test_supplementary_text_is_metadata_and_blocks_incomplete_analysis(part):
    content = synthetic_document(extra={part: f'<w:hdr xmlns:w="{W}">{p("SYNTHETIC critical tolerance")}</w:hdr>'.encode()})
    result = inquiry.parse_inquiry_document(content, 'SYNTHETIC.docx')
    assert not result['source']['text_analysis_available']
    assert result['source']['supplementary_parts'][0]['part'] == part
    assert result['source']['supplementary_parts'][0]['text_present']
    assert 'critical tolerance' not in result['body']


def test_image_only_and_embedded_objects_never_infer_text():
    content = synthetic_document('<w:p><w:r><w:drawing/></w:r></w:p>', extra={'word/media/SYNTHETIC.png': b'SYNTHETIC picture'})
    result = inquiry.parse_inquiry_document(content, 'SYNTHETIC.docx')
    assert not result['source']['text_analysis_available']
    assert 'no_analyzable_text' in result['source']['analysis_blockers']
    assert result['attachments'][0]['sha256'] == hashlib.sha256(b'SYNTHETIC picture').hexdigest()
    assert result['attachments'][0]['analysis_status'] == 'not_analyzed'
    embedded = inquiry.parse_inquiry_document(synthetic_document(extra={'word/embeddings/SYNTHETIC.bin': b'not executed'}), 'SYNTHETIC.docx')
    assert 'unparsed_embedded_content' in embedded['source']['analysis_blockers']


def test_external_relationships_are_recorded_without_opening_them():
    relation = f'<Relationships xmlns="{inquiry.REL}"><Relationship Id="rId99" Type="SYNTHETIC" Target="https://example.invalid/never-open" TargetMode="External"/></Relationships>'.encode()
    result = inquiry.parse_inquiry_document(synthetic_document(extra={'word/_rels/document.xml.rels': relation}), 'SYNTHETIC.docx')
    assert result['source']['external_relationships'][0]['target'] == 'https://example.invalid/never-open'
    assert result['source']['text_analysis_available']
    assert any('未访问' in w for w in result['warnings'])


@pytest.mark.parametrize('filename', ['', None, 'SYNTHETIC.doc', 'SYNTHETIC.docm', 'SYNTHETIC.xlsx'])
def test_wrong_extension_rejected(filename):
    with pytest.raises(ValueError, match='仅支持'):
        inquiry.parse_inquiry_document(synthetic_document(), filename)


@pytest.mark.parametrize('content', [b'', b'not zip', 'not bytes'])
def test_invalid_input_rejected(content):
    with pytest.raises(ValueError):
        inquiry.parse_inquiry_document(content, 'SYNTHETIC.docx')


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-32'])
def test_dtd_and_entity_are_rejected_in_all_supported_xml_encodings(encoding):
    xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE document [<!ENTITY test "SYNTHETIC">]><w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>&test;</w:t></w:r></w:p></w:body></w:document>'.encode(encoding)
    with pytest.raises(ValueError, match='DTD'):
        inquiry.parse_inquiry_document(synthetic_document(document_xml=xml), 'SYNTHETIC.docx')


@pytest.mark.parametrize('limit', ['MAX_INPUT_BYTES', 'MAX_PARTS', 'MAX_PART_BYTES', 'MAX_UNCOMPRESSED_BYTES', 'MAX_BLOCKS', 'MAX_BODY_CHARACTERS'])
def test_limits_fail_whole_document_without_truncation(monkeypatch, limit):
    monkeypatch.setattr(inquiry, limit, 1)
    with pytest.raises(ValueError, match='上限|超限'):
        inquiry.parse_inquiry_document(synthetic_document(p('SYNTHETIC first') + p('SYNTHETIC last')), 'SYNTHETIC.docx')


@pytest.mark.parametrize('part', ['../SYNTHETIC', '/SYNTHETIC', 'word/../SYNTHETIC', 'word\\SYNTHETIC', 'C:/SYNTHETIC'])
def test_unsafe_zip_paths_rejected(part):
    # Windows ZipInfo normalizes backslashes when writing: patch equal-length
    # local and central names to exercise the actual adversarial ZIP name.
    content = synthetic_document(extra={part.replace('\\', '/'): b'SYNTHETIC'})
    if '\\' in part:
        content = content.replace(part.replace('\\', '/').encode(), part.encode())
    with pytest.raises(ValueError, match='非法部件路径'):
        inquiry.parse_inquiry_document(content, 'SYNTHETIC.docx')


def test_duplicate_parts_symlinks_and_compression_bomb_rejected():
    source = synthetic_document()
    target = BytesIO(source)
    with ZipFile(target, 'a') as package:
        with pytest.warns(UserWarning):
            package.writestr('word/document.xml', b'SYNTHETIC duplicate')
    with pytest.raises(ValueError, match='重复'):
        inquiry.parse_inquiry_document(target.getvalue(), 'SYNTHETIC.docx')
    target = BytesIO(source)
    with ZipFile(target, 'a') as package:
        info = ZipInfo('word/SYNTHETIC-link')
        info.external_attr = 0o120777 << 16
        package.writestr(info, b'../../target')
    with pytest.raises(ValueError, match='符号链接'):
        inquiry.parse_inquiry_document(target.getvalue(), 'SYNTHETIC.docx')
    with pytest.raises(ValueError, match='压缩比'):
        inquiry.parse_inquiry_document(synthetic_document(extra={'word/SYNTHETIC.bin': b'x' * (1024 * 1024 + 1)}), 'SYNTHETIC.docx')


def test_macros_wrong_content_types_and_malformed_xml_rejected():
    with pytest.raises(ValueError, match='无宏'):
        inquiry.parse_inquiry_document(synthetic_document(content_type='application/vnd.ms-word.document.macroEnabled.main+xml'), 'SYNTHETIC.docx')
    with pytest.raises(ValueError, match='宏部件'):
        inquiry.parse_inquiry_document(synthetic_document(extra={'word/vbaProject.bin': b'SYNTHETIC'}), 'SYNTHETIC.docx')
    with pytest.raises(ValueError, match='无效 XML'):
        inquiry.parse_inquiry_document(synthetic_document(document_xml=b'<broken'), 'SYNTHETIC.docx')


def test_unsupported_body_branches_math_and_wrapped_rows_are_blocked():
    unknown = '<SYNTHETIC>' + p('do not silently omit') + '</SYNTHETIC>'
    result = inquiry.parse_inquiry_document(synthetic_document(p('SYNTHETIC') + unknown), 'SYNTHETIC.docx')
    assert 'unparsed_body_container' in result['source']['analysis_blockers']
    math = '<w:p><m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:r><m:t>2x</m:t></m:r></m:oMath></w:p>'
    result = inquiry.parse_inquiry_document(synthetic_document(p('SYNTHETIC') + math), 'SYNTHETIC.docx')
    assert 'unparsed_math' in result['source']['analysis_blockers']
    wrapped = '<w:tbl><w:sdt><w:sdtContent><w:tr><w:tc>' + p('SYNTHETIC hidden row') + '</w:tc></w:tr></w:sdtContent></w:sdt></w:tbl>'
    result = inquiry.parse_inquiry_document(synthetic_document(p('SYNTHETIC') + wrapped), 'SYNTHETIC.docx')
    assert 'unparsed_body_container' in result['source']['analysis_blockers']


def test_encrypted_zip_flag_rejected_before_reading():
    content = bytearray(synthetic_document())
    # SYNTHETIC patched central-directory encryption flag, not a real encrypted file.
    start = content.index(b'PK\x01\x02')
    content[start + 8] |= 1
    with pytest.raises(ValueError, match='加密'):
        inquiry.parse_inquiry_document(bytes(content), 'SYNTHETIC.docx')


def test_alternative_paragraph_branches_do_not_claim_analysis_ready():
    body = '<w:p><mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><mc:Choice><w:r><w:t>SYNTHETIC choice</w:t></w:r></mc:Choice><mc:Fallback><w:r><w:t>SYNTHETIC fallback</w:t></w:r></mc:Fallback></mc:AlternateContent></w:p>'
    result = inquiry.parse_inquiry_document(synthetic_document(body), 'SYNTHETIC.docx')
    assert not result['source']['text_analysis_available']
    assert 'unparsed_alternative_content' in result['source']['analysis_blockers']


@pytest.mark.parametrize('limit', ['MAX_XML_DEPTH', 'MAX_XML_NODES'])
def test_xml_complexity_limits_reject_instead_of_recursion_failure(monkeypatch, limit):
    monkeypatch.setattr(inquiry, limit, 2)
    with pytest.raises(ValueError, match='层级或节点数量超限'):
        inquiry.parse_inquiry_document(synthetic_document(), 'SYNTHETIC.docx')


def style_library(extra_styles='', *, defaults=''):
    """SYNTHETIC typical Word library: unused list/hidden styles are common."""
    return f'''<w:styles xmlns:w="{W}">
      {defaults}
      <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
      <w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"/>
      <w:style w:type="paragraph" w:styleId="ListBullet"><w:basedOn w:val="Normal"/><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
      <w:style w:type="character" w:styleId="SYNTHETICUnusedHidden"><w:rPr><w:vanish/></w:rPr></w:style>
      {extra_styles}
    </w:styles>'''.encode()


def test_unused_word_style_and_numbering_libraries_do_not_block_plain_text():
    numbering = f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:pPr><w:numPr/></w:pPr><w:rPr><w:vanish/></w:rPr></w:lvl></w:abstractNum></w:numbering>'.encode()
    result = inquiry.parse_inquiry_document(synthetic_document(extra={
        'word/styles.xml': style_library(), 'word/numbering.xml': numbering}), 'SYNTHETIC.docx')
    assert result['source']['text_analysis_available']
    assert not result['source']['analysis_blockers']
    assert set(result['source']['style_resolution']['resolved_style_ids']) == {'Normal', 'DefaultParagraphFont'}


@pytest.mark.parametrize('reference,extra_styles,reason', [
    ('<w:pPr><w:pStyle w:val="ListBullet"/></w:pPr>', '', 'unparsed_numbering'),
    ('<w:r><w:rPr><w:rStyle w:val="SYNTHETICUnusedHidden"/></w:rPr><w:t>SYNTHETIC hidden</w:t></w:r>', '', 'hidden_text'),
    ('<w:pPr><w:pStyle w:val="SYNTHETICDerivedList"/></w:pPr>', '<w:style w:type="paragraph" w:styleId="SYNTHETICDerivedList"><w:basedOn w:val="ListBullet"/></w:style>', 'unparsed_numbering'),
    ('<w:pPr><w:pStyle w:val="SYNTHETICDerivedHidden"/></w:pPr>', '<w:style w:type="paragraph" w:styleId="SYNTHETICDerivedHidden"><w:basedOn w:val="SYNTHETICUnusedHidden"/></w:style>', 'hidden_text'),
])
def test_actual_style_references_and_based_on_ancestry_still_block(reference, extra_styles, reason):
    body = '<w:p>' + reference + '<w:r><w:t>SYNTHETIC actual inquiry</w:t></w:r></w:p>'
    result = inquiry.parse_inquiry_document(synthetic_document(body, extra={'word/styles.xml': style_library(extra_styles)}), 'SYNTHETIC.docx')
    assert not result['source']['text_analysis_available']
    assert reason in result['source']['analysis_blockers']
    assert any(f.get('style_id') for f in result['source']['features'])


@pytest.mark.parametrize('style_xml', [
    f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:default="1" w:styleId="SYNTHETICDefaultHidden"><w:rPr><w:vanish/></w:rPr></w:style></w:styles>',
    f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:default="1" w:styleId="SYNTHETICDefault"><w:basedOn w:val="SYNTHETICAncestor"/></w:style><w:style w:type="paragraph" w:styleId="SYNTHETICAncestor"><w:rPr><w:vanish/></w:rPr></w:style></w:styles>',
    f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr><w:vanish/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>',
])
def test_default_styles_and_document_defaults_hide_text_and_block(style_xml):
    result = inquiry.parse_inquiry_document(synthetic_document(extra={'word/styles.xml': style_xml.encode()}), 'SYNTHETIC.docx')
    assert not result['source']['text_analysis_available']
    assert 'hidden_text' in result['source']['analysis_blockers']


def test_unused_default_table_style_does_not_apply_to_document_without_tables():
    library = style_library('<w:style w:type="table" w:default="1" w:styleId="SYNTHETICHiddenTable"><w:rPr><w:vanish/></w:rPr></w:style>')
    result = inquiry.parse_inquiry_document(synthetic_document(extra={'word/styles.xml': library}), 'SYNTHETIC.docx')
    assert result['source']['text_analysis_available']
    table = '<w:tbl><w:tr><w:tc>' + p('SYNTHETIC table') + '</w:tc></w:tr></w:tbl>'
    result = inquiry.parse_inquiry_document(synthetic_document(table, extra={'word/styles.xml': library}), 'SYNTHETIC.docx')
    assert 'hidden_text' in result['source']['analysis_blockers']


@pytest.mark.parametrize('extra_styles', [
    '<w:style w:styleId="SYNTHETICUsed"><w:basedOn w:val="SYNTHETICMissing"/></w:style>',
    '<w:style w:styleId="SYNTHETICUsed"><w:basedOn w:val="SYNTHETICOther"/></w:style><w:style w:styleId="SYNTHETICOther"><w:basedOn w:val="SYNTHETICUsed"/></w:style>',
])
def test_used_missing_or_cyclic_style_ancestry_is_explicitly_unverified(extra_styles):
    body = '<w:p><w:pPr><w:pStyle w:val="SYNTHETICUsed"/></w:pPr><w:r><w:t>SYNTHETIC</w:t></w:r></w:p>'
    result = inquiry.parse_inquiry_document(synthetic_document(body, extra={'word/styles.xml': style_library(extra_styles)}), 'SYNTHETIC.docx')
    assert 'unverified_style' in result['source']['analysis_blockers']


def test_explicit_table_style_hidden_ancestor_is_applied():
    library = style_library('<w:style w:type="table" w:styleId="SYNTHETICTable"><w:basedOn w:val="SYNTHETICUnusedHidden"/></w:style>')
    table = '<w:tbl><w:tblPr><w:tblStyle w:val="SYNTHETICTable"/></w:tblPr><w:tr><w:tc>' + p('SYNTHETIC row') + '</w:tc></w:tr></w:tbl>'
    result = inquiry.parse_inquiry_document(synthetic_document(table, extra={'word/styles.xml': library}), 'SYNTHETIC.docx')
    assert 'hidden_text' in result['source']['analysis_blockers']
    assert 'SYNTHETICTable' in result['source']['style_resolution']['referenced_style_ids']
