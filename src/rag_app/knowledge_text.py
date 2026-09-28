"""Bounded, non-executing intake parsers and disclosed heuristic redaction."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 10000
MAX_TEXT_CHARS = 500000
MAX_LINES = 20000
MAX_ROWS = 20000
MAX_CELLS = 100000
MAX_COLUMNS = 256
MAX_PAGES = 500
TEXT_TYPES = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SECRET_NAME = r"(?:api[_-]?key|access[_-]?(?:token|key(?:[_-]?(?:id|secret))?)|refresh[_-]?token|client[_-]?secret|password|passwd|secret|token|authorization|cookie|set-cookie)"
ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![\w:])" + SECRET_NAME + r"\b[\"']?\s*[:=]\s*)"
    r"(?:\"(?P<double>[^\"\r\n]+)\"|'(?P<single>[^'\r\n]+)'|(?P<bare>[^\s,;&<>`}\]]+))", re.I,
)
URL = re.compile(r"(?:https?|ftp)://[^\s<>\"'`]+", re.I)
REDACTIONS = [
    ("private_key", re.compile(r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----", re.S)),
    ("api_key", re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")),
    ("bearer_token", re.compile(r"\bBearer\s+([A-Za-z0-9_.~+/=-]{8,})", re.I)),
    ("platform_account", re.compile(r"\bpdd\d{5,}\b", re.I)),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("phone_candidate", re.compile(r"(?<![A-Za-z0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")),
    ("identity_candidate", re.compile(r"(?<![A-Za-z0-9])\d{17}[\dXx](?![A-Za-z0-9])")),
]


def credential_name(name: str) -> bool:
    base = PurePosixPath(name.replace("\\", "/")).name.lower()
    return base.startswith(".env") or base in {
        "auth.json", "credentials.json", "cookies.txt", "cookies.json", "id_rsa", "id_ed25519",
    } or base.endswith((".pem", ".key", ".p12", ".pfx"))


def clean_text(text: str) -> tuple[str, dict]:
    """Preserve content/indentation; stable pseudonyms are not a privacy review."""
    counts = {}

    def hide(kind, value):
        if "[已隐藏:" in value:
            return value
        counts[kind] = counts.get(kind, 0) + 1
        canonical = value
        if kind == "phone_candidate":
            canonical = re.sub(r"\D", "", value)[-11:]
        # ponytail: deterministic pseudonyms preserve references, not anonymization;
        # use an access-controlled mapping if resistance to guessing is required.
        digest = hashlib.sha256((kind + "\0" + canonical).encode()).hexdigest()[:12]
        return "[已隐藏:" + kind + ":" + digest + "]"

    def assignments(match):
        group = next(key for key in ("double", "single", "bare") if match.group(key) is not None)
        value = match.group(group)
        if "[已隐藏:" in value:
            return match.group()
        quote = {"double": '"', "single": "'", "bare": ""}[group]
        return match.group("prefix") + quote + hide("credential_assignment", value) + quote

    def scrub(part, in_url=False):
        part = ASSIGNMENT.sub(assignments, part)
        for kind, pattern in REDACTIONS:
            if in_url and kind in {"phone_candidate", "identity_candidate"}:
                continue
            if kind == "bearer_token":
                part = pattern.sub(lambda m: "Bearer " + hide(kind, m.group(1)), part)
            else:
                part = pattern.sub(lambda m: hide(kind, m.group()), part)
        return part

    def scrub_url(value):
        value = re.sub(r"(?<=//)[^/@?#]+@", lambda m: hide("url_credentials", m.group()[:-1]) + "@", value)
        # Inspect decoded query keys without decoding/reformatting ordinary URLs.
        value = re.sub(
            r"([?&#])([^=&#]+)=([^&#]*)",
            lambda m: m.group(1) + m.group(2) + "=" + (
                hide("credential_assignment", unquote(m.group(3)))
                if re.fullmatch(SECRET_NAME, unquote(m.group(2)), re.I) else m.group(3)
            ), value,
        )
        return scrub(value, in_url=True)

    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    parts, start = [], 0
    for match in URL.finditer(text):
        parts.extend((scrub(text[start:match.start()]), scrub_url(match.group())))
        start = match.end()
    parts.append(scrub(text[start:]))
    return "".join(parts), counts


class _ParseFailure(ValueError):
    """Only fixed, non-sensitive error codes may cross the parser boundary."""


def _issue(issues, code, **location):
    item = {"code": code, **location}
    if item not in issues:
        issues.append(item)


def _office_archive(data, issues):
    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS or sum(item.file_size for item in infos) > MAX_EXPANDED_BYTES:
            raise _ParseFailure("office_expanded_size_limit")
        names = set()
        for item in infos:
            name = item.filename.replace("\\", "/")
            if (name in names or name.startswith(("/", "~")) or ".." in PurePosixPath(name).parts
                    or re.match(r"^[A-Za-z]:", name) or stat.S_ISLNK(item.external_attr >> 16)):
                raise _ParseFailure("unsafe_office_archive")
            names.add(name)
            if item.flag_bits & 1:
                raise _ParseFailure("encrypted_document")
            if "vbaproject" in name.lower():
                _issue(issues, "macros_not_executed")
            if "/embeddings/" in name.lower() or name.lower().endswith(".zip"):
                _issue(issues, "embedded_content_not_parsed")
            if name.lower().endswith((".xml", ".rels")):
                raw = archive.read(item)
                if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.I):
                    raise _ParseFailure("unsafe_xml_declaration")
                if name.lower().endswith(".rels"):
                    if any(node.get("TargetMode", "").lower() == "external" for node in ET.fromstring(raw)):
                        _issue(issues, "external_links_not_fetched")
        return archive
    except Exception:
        archive.close()
        raise


def _paragraph(node):
    return "".join(
        item.text or "" if item.tag == W + "t" else "\t" if item.tag == W + "tab" else "\n"
        for item in node.iter() if item.tag in {W + "t", W + "tab", W + "br", W + "cr"}
    )


def _docx(archive, issues):
    root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(W + "body")
    if body is None:
        raise _ParseFailure("invalid_docx_body")
    if any(node.tag in {W + "drawing", W + "pict", W + "altChunk", W + "object"} for node in body.iter()):
        _issue(issues, "docx_nontext_content_not_parsed")
    if any(re.match(r"word/(?:header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", name) for name in archive.namelist()):
        _issue(issues, "docx_auxiliary_parts_not_parsed")
    paragraph, table = 0, 0
    for block in body:
        if block.tag == W + "p":
            paragraph += 1
            yield _paragraph(block), {"kind": "paragraph", "paragraph": paragraph}
        elif block.tag == W + "tbl":
            table += 1
            if block.find(".//" + W + "tbl") is not None:
                _issue(issues, "docx_nested_table_flattened", table=table)
            for row_number, row in enumerate(block.findall(W + "tr"), 1):
                cells = ["\n".join(_paragraph(p) for p in cell.iter(W + "p")) for cell in row.findall(W + "tc")]
                yield "[表格 %d 行 %d] %s" % (table, row_number, json.dumps(cells, ensure_ascii=False)), {
                    "kind": "table_row", "table": table, "row": row_number,
                }
        elif block.tag != W + "sectPr":
            _issue(issues, "docx_unsupported_block_not_parsed")


def _xlsx(data, issues):
    import openpyxl

    formula_book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    cached_book = None
    try:
        cached_book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        total_rows, total_cells = 0, 0
        for sheet_number, (sheet, cached) in enumerate(zip(formula_book.worksheets, cached_book.worksheets), 1):
            yield "# 工作表 %d：%s" % (sheet_number, sheet.title), {"kind": "sheet", "sheet_index": sheet_number}
            # Ignore inaccurate dimension metadata; iterate the actual XML rows.
            sheet.reset_dimensions()
            cached.reset_dimensions()
            for row_number, (row, cached_row) in enumerate(zip(sheet.iter_rows(), cached.iter_rows()), 1):
                total_rows += 1
                total_cells += len(row)
                if total_rows > MAX_ROWS or total_cells > MAX_CELLS:
                    _issue(issues, "xlsx_rows_or_cells_limit", sheet_index=sheet_number, row=row_number)
                    return
                if len(row) > MAX_COLUMNS:
                    _issue(issues, "xlsx_columns_limit", sheet_index=sheet_number, row=row_number)
                values = []
                for cell, cached_cell in zip(row[:MAX_COLUMNS], cached_row[:MAX_COLUMNS]):
                    if cell.value is None:
                        continue
                    actual = cell.value
                    if cell.data_type == "f" and not isinstance(actual, str):
                        actual = getattr(actual, "text", None)
                        if actual is None:
                            _issue(issues, "xlsx_formula_type_not_parsed", sheet_index=sheet_number, row=row_number)
                    value = json.dumps(actual, ensure_ascii=False, default=str)
                    if cell.data_type == "f":
                        value = "formula=" + value + "; cached_value=" + json.dumps(cached_cell.value, ensure_ascii=False, default=str)
                        _issue(issues, "xlsx_formulas_not_recalculated")
                        if cached_cell.value is None:
                            _issue(issues, "xlsx_formula_cache_missing", sheet_index=sheet_number, row=row_number)
                    if cell.number_format != "General":
                        value += "; number_format=" + json.dumps(cell.number_format, ensure_ascii=False)
                    values.append(cell.coordinate + "=" + value)
                if values:
                    yield "[表 %d 行 %d] %s" % (sheet_number, row_number, " | ".join(values)), {
                        "kind": "sheet_row", "sheet_index": sheet_number, "row": row_number,
                    }
    finally:
        formula_book.close()
        if cached_book is not None:
            cached_book.close()


def _pdf(data, issues):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise _ParseFailure("encrypted_document")
    root = reader.trailer["/Root"]
    if "/OpenAction" in root or "/AA" in root or "/Names" in root:
        _issue(issues, "pdf_actions_and_attachments_not_executed")
    for number, page in enumerate(reader.pages, 1):
        if number > MAX_PAGES:
            _issue(issues, "pdf_page_limit", limit=MAX_PAGES)
            return
        text = page.extract_text() or ""
        if not text.strip():
            _issue(issues, "needs_ocr", page=number)
            yield "", {"kind": "page", "page": number}
        else:
            yield "[第 %d 页]\n%s" % (number, text), {"kind": "page", "page": number}


def _collect(parts, issues):
    fragments, locations, size, lines = [], [], 0, 0
    try:
        for fragment, location in parts:
            separator = int(bool(fragments))
            if lines >= MAX_LINES or size + separator > MAX_TEXT_CHARS:
                _issue(issues, "text_line_limit" if lines >= MAX_LINES else "text_character_limit",
                       limit=MAX_LINES if lines >= MAX_LINES else MAX_TEXT_CHARS)
                break
            room = max(0, MAX_TEXT_CHARS - size - separator)
            clipped = fragment[:room]
            if len(clipped) < len(fragment):
                _issue(issues, "text_character_limit", limit=MAX_TEXT_CHARS)
            split = clipped.split("\n", max(0, MAX_LINES - lines))
            if len(split) > MAX_LINES - lines:
                clipped = "\n".join(split[:MAX_LINES - lines])
                _issue(issues, "text_line_limit", limit=MAX_LINES)
            fragments.append(clipped)
            locations.append(location)
            size += len(clipped) + separator
            lines += clipped.count("\n") + 1
            if clipped != fragment:
                break
    finally:
        if hasattr(parts, "close"):
            parts.close()
    text = "\n".join(fragments)
    if not text.strip():
        _issue(issues, "no_text_extracted")
    return {"text": text, "locations": locations, "issues": issues}


def parse_text_file(name: str, data: bytes) -> dict:
    """Return unredacted text with locations/issues; caller must run clean_text.

    No execution, external fetches, OCR, decryption, or formula recalculation.
    Truncated output always carries a limit issue; failures never echo input.
    """
    if not isinstance(name, str) or not isinstance(data, bytes):
        raise ValueError("invalid_parser_input")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("file_size_limit")
    if credential_name(name):
        raise ValueError("credential_file_skipped")
    suffix = PurePosixPath(name).suffix.lower()
    issues = []
    try:
        if suffix in TEXT_TYPES:
            if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
                raise _ParseFailure("unsupported_text_encoding")
            encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            try:
                text = data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            except UnicodeError:
                if encoding == "utf-16":
                    raise _ParseFailure("invalid_text_encoding") from None
                raise
            if encoding == "utf-16":
                _issue(issues, "encoding_fallback", encoding="utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be")
            if "\x00" in text:
                raise _ParseFailure("non_text_content")
            return _collect(iter([(text, {"kind": "lines", "start_line": 1})]), issues)
        if suffix in {".docx", ".xlsx"}:
            with _office_archive(data, issues) as archive:
                return _collect(_docx(archive, issues) if suffix == ".docx" else _xlsx(data, issues), issues)
        if suffix == ".pdf":
            return _collect(_pdf(data, issues), issues)
        raise _ParseFailure("unsupported_file_type")
    except _ParseFailure as exc:
        raise ValueError(str(exc)) from None
    except UnicodeError:
        raise ValueError("non_utf8_text") from None
    except Exception:
        raise ValueError("parse_failed") from None
