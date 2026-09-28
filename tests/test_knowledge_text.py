import io
import json
import zipfile

import pytest

from rag_app import knowledge_text as kt


pytestmark = pytest.mark.offline


def office(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def docx(body, **files):
    return office({"word/document.xml": '<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (kt.W[1:-1], body), **files})


def codes(result):
    return {item["code"] for item in result["issues"]}


def test_cleaning_is_disclosed_stable_and_preserves_meaning_and_urls():
    source = ('# 未验证\r\n    不能将 0/16 写成通过；12.5 mg，2026-09-27。\n'
              '号码 13900000000 / 13900000001 / +86 13900000000\n'
              '联系 demo@example.test；password="short value"；Bearer abcdefghijklmnop\n'
              'https://example.test/13900000000?id=13900000000&%74oken=demo-secret\n'
              'https://user:demo-password@example.test/path')
    cleaned, counts = kt.clean_text(source)
    assert '# 未验证\n    不能将 0/16 写成通过；12.5 mg，2026-09-27。' in cleaned
    phones = cleaned.splitlines()[2].split(' / ')
    assert phones[0].removeprefix('号码 ') == phones[2] and phones[1] != phones[2]
    assert 'https://example.test/13900000000?id=13900000000' in cleaned
    for private in ('demo@example.test', 'short value', 'abcdefghijklmnop', 'demo-secret', 'demo-password'):
        assert private not in cleaned and private not in json.dumps(counts)
    assert counts['phone_candidate'] == 3
    assert kt.clean_text(source) == (cleaned, counts)
    assert kt.clean_text(cleaned) == (cleaned, {})
    email_url, _ = kt.clean_text('https://example.test?email=demo@example.test')
    assert email_url.startswith('https://example.test?email=') and 'demo@example.test' not in email_url


@pytest.mark.parametrize('suffix', ['md', 'txt', 'csv', 'json', 'yaml', 'yml', 'xml', 'html'])
def test_utf8_formats_are_literal_text_not_code(suffix):
    content = '# 不执行\n    <script>throw Error("unchanged")</script>\n0/16，不采用'
    result = kt.parse_text_file('test.' + suffix, content.encode())
    assert result['text'] == content and result['locations'] == [{'kind': 'lines', 'start_line': 1}]
    assert result['issues'] == []


@pytest.mark.parametrize('bom,encoding', [(b'\xff\xfe', 'utf-16-le'), (b'\xfe\xff', 'utf-16-be')])
def test_utf16_requires_explicit_bom_and_reports_encoding_fallback(bom, encoding):
    content = '# 编码示例\r\n    不采用 0/16，12.5 mg'
    result = kt.parse_text_file('example.txt', bom + content.encode(encoding))
    assert result['text'] == content.replace('\r\n', '\n')
    assert result['issues'] == [{'code': 'encoding_fallback', 'encoding': encoding}]
    with pytest.raises(ValueError, match='^(non_utf8_text|non_text_content)$'):
        kt.parse_text_file('example.txt', content.encode(encoding))
    with pytest.raises(ValueError, match='^invalid_text_encoding$'):
        kt.parse_text_file('example.txt', bom + b'x')


def test_unknown_legacy_encoding_and_utf32_are_not_guessed():
    with pytest.raises(ValueError, match='^non_utf8_text$'):
        kt.parse_text_file('example.txt', '示例文本'.encode('gb18030'))
    with pytest.raises(ValueError, match='^unsupported_text_encoding$'):
        kt.parse_text_file('example.txt', '示例文本'.encode('utf-32'))


def test_docx_keeps_paragraph_table_order_and_discloses_omissions():
    raw = docx('<w:p><w:r><w:t>前文</w:t><w:tab/><w:t>否</w:t></w:r></w:p>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>数量</w:t></w:r></w:p></w:tc>'
               '<w:tc><w:p><w:r><w:t>0/16</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
               '<w:p><w:r><w:t>后文</w:t></w:r></w:p>', **{
                   'word/header1.xml': '<header/>', 'word/vbaProject.bin': b'never execute',
                   'word/_rels/document.xml.rels': '<Relationships><Relationship TargetMode="External" Target="https://example.test/private"/></Relationships>',
               })
    result = kt.parse_text_file('test.docx', raw)
    assert result['text'].startswith('前文\t否\n[表格 1 行 1] ["数量", "0/16"]\n后文')
    assert [x['kind'] for x in result['locations']] == ['paragraph', 'table_row', 'paragraph']
    assert codes(result) == {'docx_auxiliary_parts_not_parsed', 'macros_not_executed', 'external_links_not_fetched'}
    assert 'private' not in json.dumps(result['issues'])


def test_xlsx_keeps_headers_rows_formula_and_cached_value_without_recalculation():
    from openpyxl import Workbook

    book = Workbook()
    book.active.title = '实测'
    book.active.append(['数量', '公式'])
    book.active.append([3, '=A2*2'])
    book.active['A2'].number_format = '0.0" mg"'
    book.create_sheet('边界').append(['不适用'])
    output = io.BytesIO()
    book.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist()}
    files['xl/worksheets/sheet1.xml'] = files['xl/worksheets/sheet1.xml'].replace(b'<f>A2*2</f><v></v>', b'<f>A2*2</f><v>6</v>')
    result = kt.parse_text_file('test.xlsx', office(files))
    assert '# 工作表 1：实测' in result['text'] and '# 工作表 2：边界' in result['text']
    assert 'A1="数量"' in result['text'] and 'B2=formula="=A2*2"; cached_value=6' in result['text']
    assert 'A2=3; number_format="0.0\\" mg\\""' in result['text']
    assert codes(result) == {'xlsx_formulas_not_recalculated'}
    assert {'kind': 'sheet_row', 'sheet_index': 1, 'row': 2} in result['locations']
    missing = kt.parse_text_file('test.xlsx', output.getvalue())
    assert 'xlsx_formula_cache_missing' in codes(missing)


def test_pdf_blank_page_needs_ocr_and_encryption_is_rejected():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    result = kt.parse_text_file('blank.pdf', output.getvalue())
    assert result['text'] == '' and result['locations'] == [{'kind': 'page', 'page': 1}]
    assert {'code': 'needs_ocr', 'page': 1} in result['issues']
    writer.encrypt('demo-pass')
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match='^encrypted_document$'):
        kt.parse_text_file('locked.pdf', output.getvalue())


def test_limits_are_explicit_instead_of_silent_partial_success(monkeypatch):
    monkeypatch.setattr(kt, 'MAX_TEXT_CHARS', 8)
    result = kt.parse_text_file('test.txt', b'123456789')
    assert result['text'] == '12345678' and 'text_character_limit' in codes(result)
    monkeypatch.setattr(kt, 'MAX_LINES', 2)
    result = kt.parse_text_file('test.txt', b'a\nb\nc')
    assert result['text'] == 'a\nb' and 'text_line_limit' in codes(result)
    monkeypatch.setattr(kt, 'MAX_EXPANDED_BYTES', 8)
    with pytest.raises(ValueError, match='^office_expanded_size_limit$'):
        kt.parse_text_file('test.docx', docx('<w:p/>'))
    monkeypatch.setattr(kt, 'MAX_FILE_BYTES', 8)
    with pytest.raises(ValueError, match='^file_size_limit$'):
        kt.parse_text_file('test.txt', b'123456789')


def test_spreadsheet_limits_and_false_dimensions_are_disclosed(monkeypatch):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(['header', 'second'])
    book.active.append([2, 3])
    output = io.BytesIO()
    book.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist()}
    files['xl/worksheets/sheet1.xml'] = files['xl/worksheets/sheet1.xml'].replace(b'ref="A1:B2"', b'ref="A1:A1"')
    raw = office(files)
    assert 'B2=3' in kt.parse_text_file('test.xlsx', raw)['text']
    monkeypatch.setattr(kt, 'MAX_ROWS', 1)
    assert 'xlsx_rows_or_cells_limit' in codes(kt.parse_text_file('test.xlsx', raw))
    monkeypatch.setattr(kt, 'MAX_ROWS', 20)
    monkeypatch.setattr(kt, 'MAX_COLUMNS', 1)
    assert 'xlsx_columns_limit' in codes(kt.parse_text_file('test.xlsx', raw))


@pytest.mark.parametrize('name,data,code', [
    ('auth.json', b'demo-private-value', 'credential_file_skipped'),
    ('run.py', b'raise Exception("demo-private-value")', 'unsupported_file_type'),
    ('bad.txt', b'\xffdemo-private-value', 'non_utf8_text'),
    ('bad.pdf', b'demo-private-value', 'parse_failed'),
    ('bad.docx', b'demo-private-value', 'parse_failed'),
    ('bad.xlsx', office({'a.xml': '<!DOCTYPE root [<!ENTITY secret "demo-private-value">]><root/>'}), 'unsafe_xml_declaration'),
    ('bad.xlsx', office({'a.xml': '<!DOCTYPE root [<!ENTITY secret "demo-private-value">]><root/>'.encode('utf-16')}), 'unsafe_xml_declaration'),
    ('bad.docx', office({'../outside.xml': 'demo-private-value'}), 'unsafe_office_archive'),
])
def test_untrusted_failures_never_echo_document_content(name, data, code):
    with pytest.raises(ValueError) as error:
        kt.parse_text_file(name, data)
    assert str(error.value) == code


def test_credentials_recognized_across_path_styles():
    for name in ['.env', '.env.production', r'a\credentials.json', 'keys/id_ed25519', 'keys/CERT.P12']:
        assert kt.credential_name(name)
    assert not kt.credential_name('docs/key-concepts.md')
