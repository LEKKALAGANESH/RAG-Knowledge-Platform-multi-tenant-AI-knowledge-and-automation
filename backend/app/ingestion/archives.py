"""ZIP / TAR / TAR.GZ / GZ reading for B8. Nothing touches the disk: each accepted member's bytes
go to a caller-supplied sink (which stores them under a content-addressed blob key)."""

import gzip
import io
import re
import stat
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.ingestion import mime
from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import TOO_BIG, ParseError

Sink = Callable[[str, bytes], str | None]  # (member path, bytes) -> skip reason, None = accepted
Report = list[tuple[str, str | None]]  # (member path, skip reason or None)
_DRIVE = re.compile(r"^[A-Za-z]:")
_READ_ERRORS = (
    OSError, EOFError, RuntimeError, NotImplementedError,
    zipfile.BadZipFile, tarfile.TarError, zlib.error,
)  # fmt: skip


@dataclass
class _Entry:
    path: str
    size: int  # declared uncompressed size
    reason: str | None
    ref: zipfile.ZipInfo | tarfile.TarInfo | None = None


Reader = Callable[[_Entry, int], bytes]  # (entry, max bytes) -> member bytes


class _Budget:
    """Archive-level limits, checked as headers are walked so a bomb stops the walk early."""

    def __init__(self, s: IngestionSettings, packed: int) -> None:
        self.s, self.packed, self.count, self.total = s, packed, 0, 0

    def add(self, size: int) -> None:
        s = self.s
        self.count, self.total = self.count + 1, self.total + size
        if self.count > s.archive_max_members:
            raise ParseError(f"{TOO_BIG} (over {s.archive_max_members:,} files in the archive).")
        if self.total > s.max_unzipped_mb * 2**20:
            raise ParseError(f"{TOO_BIG} (unpacked size over {s.max_unzipped_mb} MB).")
        if self.total > 2**20 and self.total > s.max_zip_ratio * self.packed:
            raise ParseError(f"{TOO_BIG} (the archive is compressed over {s.max_zip_ratio}x).")


def unsafe_path(name: str) -> bool:
    """Absolute, drive-letter, parent-escaping or NUL-carrying member names (zip slip)."""
    parts = re.split(r"[\\/]", name)
    return (
        "\x00" in name or name.startswith(("/", "\\")) or bool(_DRIVE.match(name)) or ".." in parts
    )


def _kind_reason(name: str, *, link: bool, hardlink: bool, regular: bool) -> str | None:
    if unsafe_path(name):
        return "refused: unsafe path"
    if link:
        return "refused: symbolic link"
    if hardlink:
        return "refused: hard link"
    return None if regular else "refused: not a regular file"


def _zip_entries(zf: zipfile.ZipFile, b: _Budget) -> list[_Entry]:
    out = []
    for i in zf.infolist():
        if i.is_dir():
            continue
        b.add(i.file_size)
        if i.file_size > 2**20 and i.file_size > b.s.max_zip_ratio * max(i.compress_size, 1):
            raise ParseError(f"{TOO_BIG} (a file is compressed over {b.s.max_zip_ratio}x).")
        mode = (i.external_attr >> 16) & 0o170000  # 0 = no Unix mode recorded (Windows zips)
        reason = _kind_reason(
            i.filename, link=mode == stat.S_IFLNK, hardlink=False,
            regular=mode in (0, stat.S_IFREG),
        ) or ("skipped: encrypted" if i.flag_bits & 1 else None)  # fmt: skip
        out.append(_Entry(i.filename, i.file_size, reason, i))
    return out


def _tar_entries(tf: tarfile.TarFile, b: _Budget) -> list[_Entry]:
    out = []
    for m in tf:  # lazy header walk; the budget raises before a huge member is skipped over
        if m.isdir():
            continue
        b.add(m.size if m.isfile() else 0)
        reason = _kind_reason(m.name, link=m.issym(), hardlink=m.islnk(), regular=m.isfile())
        out.append(_Entry(m.name, m.size, reason, m))
    return out


def _drain(entries: list[_Entry], read: Reader, s: IngestionSettings, sink: Sink) -> Report:
    cap = s.max_upload_mb * 2**20
    too_big = f"skipped: larger than {s.max_upload_mb} MB"
    report: Report = []
    for e in entries:
        reason = e.reason or (too_big if e.size > cap else None)
        if reason is None:
            try:
                body = read(e, cap + 1)
            except _READ_ERRORS:
                body, reason = b"", "skipped: could not be read"
            if reason is None:
                empty = "skipped: empty file" if not body else None
                reason = too_big if len(body) > cap else empty or sink(e.path, body)
        report.append((e.path[:500], reason))
    return report


def _open_zip(data: bytes, s: IngestionSettings) -> tuple[list[_Entry], Reader]:
    zf = zipfile.ZipFile(io.BytesIO(data))

    def read(e: _Entry, n: int) -> bytes:
        if not isinstance(e.ref, zipfile.ZipInfo):
            return b""
        with zf.open(e.ref) as f:  # ZipExtFile stops at the declared size and checks the CRC
            return f.read(n)

    return _zip_entries(zf, _Budget(s, len(data))), read


class _Bounded:
    """Read-only file view that refuses to go past `cap` bytes. tarfile reads pax / GNU long-name
    headers whole, so a tiny .tar.gz could otherwise inflate a multi-GB header into memory."""

    def __init__(self, f: io.BufferedIOBase, cap: int) -> None:
        self.f, self.cap = f, cap

    def read(self, n: int = -1) -> bytes:
        if n < 0 or self.f.tell() + n > self.cap:
            raise ParseError(f"{TOO_BIG} (unpacked size over {self.cap // 2**20} MB).")
        return self.f.read(n)

    def seek(self, pos: int, whence: int = 0) -> int:
        return self.f.seek(pos, whence)

    def tell(self) -> int:
        return self.f.tell()


def _open_tar(data: bytes, s: IngestionSettings) -> tuple[list[_Entry], Reader]:
    raw: io.BufferedIOBase = io.BytesIO(data)
    if data.startswith(b"\x1f\x8b"):
        raw = gzip.GzipFile(fileobj=raw)
    # Headers add 512 B per member on top of the data the budget counts.
    cap = s.max_unzipped_mb * 2**20 + (s.archive_max_members + 2) * 1024
    tf = tarfile.open(fileobj=_Bounded(raw, cap), mode="r:")  # type: ignore[call-overload]

    def read(e: _Entry, n: int) -> bytes:
        f = tf.extractfile(e.ref) if isinstance(e.ref, tarfile.TarInfo) else None
        return f.read(n) if f else b""

    return _tar_entries(tf, _Budget(s, len(data))), read


def _open_gz(data: bytes, name: str, s: IngestionSettings) -> tuple[list[_Entry], Reader]:
    """A single gzip-compressed file: one member named after the archive minus `.gz`."""
    path = PurePosixPath(name.replace("\\", "/")).name.removesuffix(".gz") or "file"
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
        body = f.read(s.max_upload_mb * 2**20 + 1)
    _Budget(s, len(data)).add(len(body))
    return [_Entry(path, len(body), None)], lambda _e, _n: body


def read_archive(kind: str, data: bytes, name: str, s: IngestionSettings, sink: Sink) -> Report:
    """Every non-directory member with its skip reason (None = handed to `sink`). Archive-level
    limits (member count, unpacked total, compression ratio) fail the whole archive up front."""
    try:
        if kind == mime.ZIP:
            entries, read = _open_zip(data, s)
        elif kind == mime.TAR or mime.is_tar(mime.gunzip_head(data)):
            entries, read = _open_tar(data, s)
        else:
            entries, read = _open_gz(data, name, s)
    except _READ_ERRORS as exc:
        raise ParseError(f"The archive could not be read ({type(exc).__name__}).") from None
    return _drain(entries, read, s, sink)  # sink errors (blob store) propagate: they retry
