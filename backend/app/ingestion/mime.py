import io
import json
import re
import zipfile
import zlib
from pathlib import PurePath

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TXT, MD, CSV, HTML = "text/plain", "text/markdown", "text/csv", "text/html"
JSON, JSONL = "application/json", "application/x-ndjson"
PNG, JPEG, TIFF, BMP, WEBP = "image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"
ZIP, TAR, GZIP = "application/zip", "application/x-tar", "application/gzip"
MP3, WAV, M4A, AAC = "audio/mpeg", "audio/wav", "audio/mp4", "audio/aac"
MP4, MOV, WEBM = "video/mp4", "video/quicktime", "video/webm"
SRT, VTT = "application/x-subrip", "text/vtt"
LABELS = {
    PDF: "PDF", DOCX: "DOCX", XLSX: "XLSX", CSV: "CSV", TXT: "TXT",
    MD: "MD", HTML: "HTML", JSON: "JSON", JSONL: "JSONL",
    PNG: "PNG", JPEG: "JPEG", TIFF: "TIFF", BMP: "BMP", WEBP: "WEBP",
    ZIP: "ZIP", TAR: "TAR", GZIP: "TAR.GZ/GZ",
    MP3: "MP3", WAV: "WAV", M4A: "M4A", AAC: "AAC", MP4: "MP4", MOV: "MOV", WEBM: "WEBM",
    SRT: "SRT", VTT: "VTT",
}  # fmt: skip
SUPPORTED = frozenset(LABELS)
IMAGES = frozenset({PNG, JPEG, TIFF, BMP, WEBP})
ARCHIVES = frozenset({ZIP, TAR, GZIP})
MEDIA = frozenset({MP3, WAV, M4A, AAC, MP4, MOV, WEBM})
_IMAGE_BRANDS = {b"heic", b"heix", b"mif1", b"msf1", b"avif"}  # ftyp, but still images
# A cue timing line ("00:01:02,500 --> 00:01:04,000"), the only reliable SRT signature.
CUE_TIMING = re.compile(r"^\s*(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->", re.MULTILINE)
_BMP_DIB_SIZES = {12, 40, 52, 56, 64, 108, 124}  # "BM" alone also starts ordinary text


def _zip_kind(data: bytes) -> str | None:
    try:
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    except zipfile.BadZipFile:
        return None
    if "word/document.xml" in names:
        return DOCX
    return XLSX if "xl/workbook.xml" in names else ZIP


def is_tar(head: bytes) -> bool:
    return head[257:262] == b"ustar" and b"\x00" in head[:257]  # names are NUL-padded


def gunzip_head(data: bytes, n: int = 512) -> bytes:
    """First n decompressed bytes of a gzip stream (bounded: never inflates more)."""
    try:
        return zlib.decompressobj(47).decompress(data[: 64 * 1024], n)
    except zlib.error:
        return b""


def _image_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if data.startswith(b"\xff\xd8\xff"):
        return JPEG
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return TIFF
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    if data[:2] == b"BM" and int.from_bytes(data[14:18], "little") in _BMP_DIB_SIZES:
        return BMP
    return None


def _media_kind(data: bytes) -> str | None:
    if data[4:8] == b"ftyp":  # ISO base media: the major brand tells audio / QuickTime / video
        brand = data[8:12]
        if brand in _IMAGE_BRANDS:
            return None
        return M4A if brand in (b"M4A ", b"M4B ") else MOV if brand == b"qt  " else MP4
    if data[0:1] == b"\x00" and data[4:8] in (b"moov", b"mdat", b"wide"):  # pre-ftyp QuickTime
        return MOV
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return WAV
    if data.startswith(b"\x1a\x45\xdf\xa3"):  # EBML: WebM / Matroska
        return WEBM
    if data.startswith(b"ID3"):
        return MP3
    if len(data) < 3 or data[0] != 0xFF:
        return None
    if data[1] & 0xF6 == 0xF0 and data[2] >> 2 & 0xF < 13:  # ADTS sync, layer 0, real rate
        return AAC
    # MPEG audio frame: sync, a real layer, valid bitrate + sample rate ("\xff\xfe" = UTF-16 BOM)
    if data[1] & 0xE0 == 0xE0 and data[1] & 0x06 and data[2] >> 4 != 0xF and data[2] & 0x0C != 0x0C:
        return MP3
    return None


def _binary_kind(data: bytes) -> str | None:
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"\x1f\x8b"):
        return GZIP
    if is_tar(data):
        return TAR
    return _image_kind(data) or _media_kind(data)


def _is_jsonl(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    try:
        return bool(lines) and all(isinstance(json.loads(ln), dict) for ln in lines)
    except (ValueError, RecursionError):  # deeply nested JSON is simply "not JSONL"
        return False


def _text_kind(text: str, filename: str) -> str:
    head = text.lstrip()[:200].lower()
    if head.startswith(("<!doctype html", "<html")):
        return HTML
    if text.lstrip().startswith("WEBVTT"):
        return VTT
    if CUE_TIMING.search(text[:2000]):
        return SRT
    if head.startswith(("{", "[")):
        try:
            json.loads(text)
            return JSON
        except (ValueError, RecursionError):
            if _is_jsonl(text):
                return JSONL
    # Plain-text families are indistinguishable by content; the extension only picks among them.
    ext = PurePath(filename).suffix.lower()
    return {".csv": CSV, ".md": MD, ".markdown": MD}.get(ext, TXT)


def detect_mime(data: bytes, filename: str) -> str | None:
    """MIME from content (magic bytes / structure); None = unsupported."""
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):  # 2nd: empty zip; corrupt zip = None
        return _zip_kind(data)
    kind = _binary_kind(data)
    if kind is not None or b"\x00" in data[:8192]:
        return kind
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:  # legacy-encoded subtitles are common; other text must be UTF-8
        return SRT if CUE_TIMING.search(data[:2000].decode("latin-1")) else None
    return _text_kind(text, filename) if text.strip() else None
