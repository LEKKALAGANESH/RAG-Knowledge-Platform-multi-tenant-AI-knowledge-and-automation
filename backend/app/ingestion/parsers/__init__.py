"""One parser per format; each turns raw bytes into located text sections."""

import io
import multiprocessing
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from multiprocessing.connection import Connection

from app.ingestion import mime
from app.ingestion.config import IngestionSettings, get_ingestion_settings

Loc = dict[str, str | int | None]


class ParseError(Exception):
    """Permanent: the file itself is unreadable; retrying cannot help."""


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    header_row: int = 1  # sheet row number of the header
    cols: tuple[str, str] | None = None  # (first, last) column letters, when a sheet range exists


@dataclass
class Section:
    text: str
    loc: Loc
    table: Table | None = None


@dataclass
class ParsedDoc:
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, str | int | float] = field(default_factory=dict)  # merged into Document.meta


def render_table(header: list[str], rows: list[list[str]]) -> str:
    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |"

    return "\n".join([line(header), line(["---"] * len(header)), *(line(r) for r in rows)])


def table_section(t: Table, loc: Loc) -> Section:
    return Section(render_table(t.header, t.rows), loc, t)


def decode(data: bytes) -> str:
    return data.decode("utf-8-sig")


def _registry(s: IngestionSettings) -> dict[str, Callable[[bytes], ParsedDoc]]:
    from app.ingestion.parsers import docx, html, jsonl, media, ocr, pdf, sheet, subtitles, text

    images = dict.fromkeys(mime.IMAGES, partial(ocr.parse_image, s=s))
    images |= dict.fromkeys(mime.MEDIA, partial(media.parse_media, s=s))
    return images | {
        mime.SRT: partial(subtitles.parse, s=s),
        mime.VTT: partial(subtitles.parse, s=s),
        mime.PDF: partial(pdf.parse, s=s),
        mime.DOCX: docx.parse,
        mime.XLSX: partial(sheet.parse_xlsx, max_cells=s.max_sheet_cells),
        mime.CSV: sheet.parse_csv,
        mime.TXT: text.parse_txt,
        mime.MD: text.parse_md,
        mime.HTML: html.parse,
        mime.JSON: jsonl.parse_json,
        mime.JSONL: jsonl.parse_jsonl,
    }


TOO_BIG = "The file expands to more content than this server accepts"


def check_zip(data: bytes, s: IngestionSettings) -> None:
    """Zip-bomb guard for DOCX/XLSX, from the central directory only (nothing is inflated)."""
    members = zipfile.ZipFile(io.BytesIO(data)).infolist()
    if len(members) > s.max_zip_members:
        raise ParseError(f"{TOO_BIG} ({len(members)} parts; the limit is {s.max_zip_members}).")
    total = sum(m.file_size for m in members)
    if total > s.max_unzipped_mb * 2**20:
        raise ParseError(f"{TOO_BIG} (unpacked size over {s.max_unzipped_mb} MB).")
    for m in members:  # small parts compress oddly; only big, highly compressed ones are bombs
        if m.file_size > 2**20 and m.file_size > s.max_zip_ratio * max(m.compress_size, 1):
            raise ParseError(f"{TOO_BIG} (part {m.filename} is compressed over "
                             f"{s.max_zip_ratio}x).")  # fmt: skip


def parse(mime_type: str, data: bytes, s: IngestionSettings | None = None) -> ParsedDoc:
    s = s or get_ingestion_settings()
    parser = _registry(s).get(mime_type)
    if parser is None:
        raise ParseError(f"Unsupported file type: {mime_type}.")
    try:
        if mime_type in (mime.DOCX, mime.XLSX):
            check_zip(data, s)
        doc = parser(data)
    except ParseError:
        raise
    except MemoryError:
        raise ParseError(f"{TOO_BIG} (out of memory while reading it).") from None
    except Exception as exc:  # any library failure on a bad file is a permanent parse failure
        raise ParseError(f"The file could not be read ({type(exc).__name__}: {exc})"[:500]) from exc
    if sum(len(x.text) for x in doc.sections) > s.max_text_chars:
        raise ParseError(f"{TOO_BIG} (over {s.max_text_chars:,} characters of text).")
    return doc


# Formats whose libraries do unbounded C-level work per input byte; plain text, CSV and JSON
# are linear in the upload size, which the upload cap already bounds.
ISOLATED = frozenset({mime.PDF, mime.DOCX, mime.XLSX, mime.HTML, *mime.IMAGES, *mime.MEDIA})


def _child(conn: Connection, mime_type: str, data: bytes, s: IngestionSettings) -> None:
    media = mime_type in mime.MEDIA
    if sys.platform != "win32":
        import resource

        cap = (s.transcribe_memory_mb if media else s.parse_memory_mb) * 2**20
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    try:
        if media:  # duration first: the parent stretches its timeout to fit the media length
            from app.ingestion.parsers.media import probe_seconds, transcribe_enabled

            if transcribe_enabled(s):
                conn.send(probe_seconds(data, s))
        conn.send(parse(mime_type, data, s))
    except ParseError as exc:
        conn.send(exc)


def parse_isolated(mime_type: str, data: bytes, s: IngestionSettings | None = None) -> ParsedDoc:
    """`parse` in a spawned child with a wall-clock timeout (and RLIMIT_AS on POSIX). A bomb
    kills only the child, never the shared worker; a kill is a permanent ParseError, no retry.
    ponytail: one spawn per risky document (~1-2 s); a warm pool if parse latency ever matters."""
    s = s or get_ingestion_settings()
    if mime_type not in ISOLATED:
        return parse(mime_type, data, s)
    ctx = multiprocessing.get_context("spawn")  # fork is unsafe in a threaded worker
    recv, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_child, args=(send, mime_type, data, s))
    child.start()
    send.close()

    def receive(timeout: float) -> object:
        if not recv.poll(timeout):
            raise ParseError(f"Reading the file took longer than {timeout:g} s; it is "
                             "too large or too complex to process.")  # fmt: skip
        return recv.recv()

    try:
        result = receive(s.parse_timeout_s)
        if isinstance(result, float):  # media duration: transcription may take ~factor x that
            result = receive(s.parse_timeout_s + result * s.transcribe_timeout_factor)
    except EOFError:  # the child died without answering: killed by the memory limit / OOM
        raise ParseError(f"{TOO_BIG} (the reader ran out of memory).") from None
    finally:
        child.kill()
        child.join()
        recv.close()
    if not isinstance(result, ParsedDoc):
        raise result if isinstance(result, ParseError) else ParseError("Unreadable file.")
    return result
