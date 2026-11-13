import io
import time
import zipfile
from collections.abc import Iterator
from itertools import pairwise

import pytest

from app.ingestion import mime
from app.ingestion.chunking import chunk_sections, split_text, table_chunks
from app.ingestion.config import get_ingestion_settings
from app.ingestion.mime import detect_mime
from app.ingestion.parsers import ParseError, Section, Table, parse, table_section
from tests import fixtures as fx

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def rezip(base: bytes, member: str, xml: bytes) -> bytes:
    """`base` with one part replaced: a tiny upload whose XML expands hugely."""
    src, out = zipfile.ZipFile(io.BytesIO(base)), io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i in src.infolist():
            z.writestr(i.filename, xml if i.filename == member else src.read(i.filename))
    return out.getvalue()


def xlsx_bomb(rows: int, cols: int = 10) -> bytes:
    row = b"<row>" + b"<c><v>1</v></c>" * cols + b"</row>"
    xml = f'<worksheet xmlns="{SHEET_NS}"><sheetData>'.encode() + row * rows
    return rezip(fx.xlsx(), "xl/worksheets/sheet1.xml", xml + b"</sheetData></worksheet>")


def docx_bomb(paragraphs: int) -> bytes:
    p = b"<w:p><w:r><w:t>x</w:t></w:r></w:p>"
    xml = f'<w:document xmlns:w="{WORD_NS}"><w:body>'.encode() + p * paragraphs
    return rezip(fx.docx(), "word/document.xml", xml + b"</w:body></w:document>")


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    get_ingestion_settings.cache_clear()
    yield monkeypatch  # tests setenv a limit, then cache_clear
    get_ingestion_settings.cache_clear()


def _set(m: pytest.MonkeyPatch, **env: object) -> None:
    for k, v in env.items():
        m.setenv(k, str(v))
    get_ingestion_settings.cache_clear()


def test_split_text_respects_size_and_overlaps() -> None:
    text = "\n".join(f"line {i}" + " word" * 8 for i in range(40))  # 40 lines x 10 words
    chunks = split_text(text, size=50, overlap=10)
    assert len(chunks) > 1 and all(len(c.split()) <= 50 for c in chunks)
    for prev, nxt in pairwise(chunks):
        assert prev.splitlines()[-1] == nxt.splitlines()[0]  # one carried line of overlap


def test_split_text_breaks_a_single_huge_line() -> None:
    assert [len(c.split()) for c in split_text("w " * 120, size=50, overlap=0)] == [50, 50, 20]


def test_big_table_splits_by_rows_with_header_and_ranges() -> None:
    rows = [[f"r{i}", "x " * 9] for i in range(30)]
    t = Table(["id", "val"], rows, header_row=1, cols=("A", "B"))
    chunks = table_chunks(table_section(t, {"sheet": "S"}), t, cap=100)
    assert len(chunks) > 1 and all(c[0].startswith("| id | val |") for c in chunks)
    ranges = [str(c[1]["range"]) for c in chunks]
    assert ranges[0].startswith("A2:") and ranges[-1].endswith(":B31")
    small = table_chunks(table_section(t, {}), t, cap=10_000)
    assert [c[1] for c in small] == [{"range": "A1:B31"}]


def test_detect_mime_uses_content_not_extension() -> None:
    assert detect_mime(fx.pdf(["x"]), "notes.txt") == mime.PDF
    assert detect_mime(fx.docx(), "file.xlsx") == mime.DOCX
    assert detect_mime(fx.xlsx(), "file.docx") == mime.XLSX
    assert detect_mime(fx.JSON, "data.txt") == mime.JSON
    assert detect_mime(fx.HTML, "page.md") == mime.HTML
    assert detect_mime(b"MZ\x90\x00binary", "evil.pdf") is None
    assert detect_mime(b"PK\x03\x04junk", "a.docx") is None
    assert detect_mime(b"\xff\xfe\xfa", "a.txt") is None


def test_detect_mime_survives_deeply_nested_json() -> None:  # N7: was RecursionError -> 500
    assert detect_mime(b"[" * 100_000, "a.json") == mime.TXT
    assert detect_mime(b"{" * 100_000 + b"\n" + b"{" * 100_000, "a.txt") == mime.TXT


def test_xlsx_zip_bomb_fails_fast_without_inflating() -> None:
    data = xlsx_bomb(rows=200_000)  # 2M cells in a few KB
    assert len(data) < 200_000
    start = time.perf_counter()
    with pytest.raises(ParseError, match="expands to more content"):
        parse(mime.XLSX, data)
    assert time.perf_counter() - start < 2


def test_docx_zip_bomb_fails_fast() -> None:
    start = time.perf_counter()
    with pytest.raises(ParseError, match="expands to more content"):
        parse(mime.DOCX, docx_bomb(1_000_000))
    assert time.perf_counter() - start < 2


def test_xlsx_cell_cap_streams_and_stops(limits: pytest.MonkeyPatch) -> None:
    _set(limits, MAX_SHEET_CELLS=1_000)
    with pytest.raises(ParseError, match="more than 1,000 cells"):
        parse(mime.XLSX, xlsx_bomb(rows=500))
    assert parse(mime.XLSX, fx.xlsx()).sections  # a normal workbook is untouched


def test_zip_member_count_and_unpacked_size_caps(limits: pytest.MonkeyPatch) -> None:
    _set(limits, MAX_ZIP_MEMBERS=3)
    with pytest.raises(ParseError, match="parts; the limit is 3"):
        parse(mime.DOCX, fx.docx())
    _set(limits, MAX_ZIP_MEMBERS=10_000, MAX_UNZIPPED_MB=1)
    with pytest.raises(ParseError, match="unpacked size over 1 MB"):
        parse(mime.XLSX, xlsx_bomb(rows=10_000))


def test_pdf_page_cap(limits: pytest.MonkeyPatch) -> None:
    _set(limits, MAX_PDF_PAGES=5)
    with pytest.raises(ParseError, match="6 pages; the limit is 5"):
        parse(mime.PDF, fx.pdf(["p"] * 6))
    assert len(parse(mime.PDF, fx.pdf(["p"] * 5)).sections) == 5


def test_extracted_text_cap(limits: pytest.MonkeyPatch) -> None:
    _set(limits, MAX_TEXT_CHARS=100)
    with pytest.raises(ParseError, match="over 100 characters"):
        parse(mime.TXT, b"word " * 100)


def test_whitespace_free_text_is_hard_split() -> None:  # S5: one "token" of 50k chars
    pieces = chunk_sections([Section("x" * 50_000, {"page": 1})], 400, 50, 1200)
    assert len(pieces) > 1 and all(len(t) <= 4_000 for t, _ in pieces)
    assert "".join(t for t, _ in pieces) == "x" * 50_000


def test_isolated_parse_returns_result_and_enforces_timeout(limits: pytest.MonkeyPatch) -> None:
    from app.ingestion.parsers import parse_isolated

    assert parse_isolated(mime.PDF, fx.pdf(["hello"])).sections[0].text == "hello"
    _set(limits, PARSE_TIMEOUT_S=0.01)  # the child cannot even start in 10 ms
    with pytest.raises(ParseError, match=r"took longer than 0.01 s"):
        parse_isolated(mime.PDF, fx.pdf(["hello"]))


def test_xlsx_range_when_data_starts_below_row_1_and_right_of_a() -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    assert ws is not None
    for r, row in enumerate([["id", "val"], ["a", "1"], ["b", "2"]], start=3):
        for c, v in enumerate(row, start=3):
            ws.cell(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    [sec] = parse(mime.XLSX, buf.getvalue()).sections
    assert sec.table is not None and sec.table.header == ["id", "val"]
    assert [c[1]["range"] for c in table_chunks(sec, sec.table, cap=10_000)] == ["C3:D5"]
