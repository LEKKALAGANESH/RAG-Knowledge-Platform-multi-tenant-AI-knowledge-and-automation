"""B8 archives: ZIP / TAR / TAR.GZ / GZ unpacked into member documents, bomb + slip guards."""

import gzip
import io
import stat
import tarfile
import uuid
import zipfile

import httpx
import pytest
from dramatiq.brokers.stub import StubBroker
from sqlalchemy import select

from app.core.db import SessionLocal, use_tenant
from app.ingestion import mime
from app.ingestion.archives import read_archive, unsafe_path
from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import ParseError
from app.ingestion.pipeline import process_document
from app.kb.models import Document
from tests import fixtures as fx
from tests.conftest import Account, register
from tests.test_ingestion import _kb, _upload, stub_broker  # noqa: F401 - autouse fixture

EXE = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64  # Windows executable header


def make_zip(members: list[tuple[str, bytes]], compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compress) as z:
        for name, data in members:
            z.writestr(zipfile.ZipInfo(name), data, compress_type=compress)
    return out.getvalue()


def make_tar(members: list[tarfile.TarInfo | tuple[str, bytes]], mode: str = "w:gz") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode=mode) as t:  # type: ignore[call-overload]
        for m in members:
            if isinstance(m, tarfile.TarInfo):
                t.addfile(m)
            else:
                info = tarfile.TarInfo(m[0])
                info.size = len(m[1])
                t.addfile(info, io.BytesIO(m[1]))
    return out.getvalue()


def collect(kind: str, data: bytes, s: IngestionSettings | None = None, name: str = "a"):  # type: ignore[no-untyped-def]
    got: dict[str, bytes] = {}

    def sink(path: str, body: bytes) -> str | None:
        got[path] = body
        return None

    return read_archive(kind, data, name, s or IngestionSettings(), sink), got


# ---- units: detection, path safety, link types, limits -------------------------------------


def test_detects_zip_tar_tgz_gz_but_office_files_stay_office() -> None:
    assert mime.detect_mime(make_zip([("a.txt", b"x")]), "a.zip") == mime.ZIP
    assert mime.detect_mime(make_tar([("a.txt", b"x")], "w"), "a.tar") == mime.TAR
    assert mime.detect_mime(make_tar([("a.txt", b"x")]), "a.tgz") == mime.GZIP
    assert mime.detect_mime(gzip.compress(b"hello"), "a.txt.gz") == mime.GZIP
    assert mime.detect_mime(fx.docx(), "a.docx") == mime.DOCX
    assert mime.detect_mime(EXE, "a.exe") is None


@pytest.mark.parametrize(
    "name", ["../evil.txt", "a/../../evil.txt", "/abs.txt", "\\abs.txt", "C:\\x.txt", "C:/x.txt",
             "a\\..\\..\\x.txt", "a\x00.txt"],
)  # fmt: skip
def test_unsafe_member_paths_are_refused(name: str) -> None:
    assert unsafe_path(name)


@pytest.mark.parametrize("name", ["a.txt", "dir/b.md", "x..y.txt", "dir/..hidden"])
def test_ordinary_member_paths_pass(name: str) -> None:
    assert not unsafe_path(name)


def test_zip_slip_members_are_refused_and_never_reach_the_sink() -> None:
    data = make_zip([("ok.txt", b"fine"), ("../evil.txt", b"x"), ("/abs.txt", b"y"),
                     ("C:\\win.txt", b"z")])  # fmt: skip
    report, got = collect(mime.ZIP, data)
    assert list(got) == ["ok.txt"]
    refused = [r for _, r in report if r]
    assert refused == ["refused: unsafe path"] * 3


def test_zip_symlink_member_is_refused() -> None:
    link = zipfile.ZipInfo("link")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(link, "/etc/passwd")
        z.writestr("ok.txt", "fine")
    report, got = collect(mime.ZIP, out.getvalue())
    assert dict(report) == {"link": "refused: symbolic link", "ok.txt": None}
    assert list(got) == ["ok.txt"]


def test_tar_links_and_devices_are_refused() -> None:
    sym = tarfile.TarInfo("sym")
    sym.type, sym.linkname = tarfile.SYMTYPE, "/etc/passwd"
    hard = tarfile.TarInfo("hard")
    hard.type, hard.linkname = tarfile.LNKTYPE, "ok.txt"
    dev = tarfile.TarInfo("dev")
    dev.type = tarfile.CHRTYPE
    report, got = collect(mime.GZIP, make_tar([("ok.txt", b"fine"), sym, hard, dev]))
    assert dict(report) == {
        "ok.txt": None,
        "sym": "refused: symbolic link",
        "hard": "refused: hard link",
        "dev": "refused: not a regular file",
    }
    assert got == {"ok.txt": b"fine"}


def test_zip_bomb_by_ratio_fails_whole_archive() -> None:
    with pytest.raises(ParseError, match="compressed over 100x"):
        collect(mime.ZIP, make_zip([("zeros.txt", b"\x00" * (20 * 2**20))]))


def test_tgz_bomb_by_ratio_fails_whole_archive() -> None:
    with pytest.raises(ParseError, match="compressed over 100x"):
        collect(mime.GZIP, make_tar([("zeros.txt", b"\x00" * (20 * 2**20))]))


def test_total_unpacked_size_cap() -> None:
    s = IngestionSettings(max_unzipped_mb=1)
    data = make_zip([("a.bin", b"a" * 2**19), ("b.bin", b"b" * 2**19), ("c.bin", b"c")],
                    zipfile.ZIP_STORED)  # fmt: skip
    with pytest.raises(ParseError, match="unpacked size over 1 MB"):
        collect(mime.ZIP, data, s)


def test_tgz_huge_pax_header_is_refused_before_it_is_inflated() -> None:
    """tarfile reads a pax header whole; the bounded view stops it at the unpacked cap."""
    info = tarfile.TarInfo("a.txt")
    info.pax_headers = {"comment": "x" * (3 * 2**20)}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as t:
        t.addfile(info, io.BytesIO(b""))
    s = IngestionSettings(max_unzipped_mb=1, max_zip_ratio=10_000)
    with pytest.raises(ParseError, match="unpacked size over 1 MB"):
        collect(mime.GZIP, out.getvalue(), s)


def test_too_many_members() -> None:
    s = IngestionSettings(archive_max_members=3)
    data = make_tar([(f"{i}.txt", b"x") for i in range(4)], "w")
    with pytest.raises(ParseError, match="over 3 files"):
        collect(mime.TAR, data, s)


def test_member_over_upload_cap_is_skipped_not_read() -> None:
    s = IngestionSettings(max_upload_mb=1)
    data = make_zip([("big.txt", b"x" * (2**20 + 1)), ("ok.txt", b"y")], zipfile.ZIP_STORED)
    report, got = collect(mime.ZIP, data, s)
    assert dict(report)["big.txt"] == "skipped: larger than 1 MB" and list(got) == ["ok.txt"]


def test_single_gzip_file_is_one_member_named_after_the_archive() -> None:
    report, got = collect(mime.GZIP, gzip.compress(b"notes"), name="dir/notes.txt.gz")
    assert report == [("notes.txt", None)] and got == {"notes.txt": b"notes"}


def test_corrupt_archive_is_a_readable_failure() -> None:
    with pytest.raises(ParseError, match="archive could not be read"):
        collect(mime.ZIP, b"PK\x03\x04" + b"\x00" * 40)


# ---- end to end: upload -> archive doc -> member docs ----------------------------------------


async def _docs(a: Account, kb: str) -> dict[str, Document]:
    async with SessionLocal() as s:
        await use_tenant(s, a.org_id, a.user_id)
        rows = await s.scalars(select(Document).where(Document.kb_id == uuid.UUID(kb)))
        return {d.name: d for d in rows}


async def _unpack(client: httpx.AsyncClient, a: Account, kb: str, name: str, data: bytes):  # type: ignore[no-untyped-def]
    r = await _upload(client, a, kb, name, data)
    assert r.status_code == 201, r.text
    await process_document(uuid.UUID(r.json()["id"]), a.org_id)
    return (await client.get(f"/v1/documents/{r.json()['id']}", headers=a.headers)).json()


async def test_zip_members_become_documents_and_every_skip_is_reported(
    client: httpx.AsyncClient,
    stub_broker: StubBroker,  # noqa: F811
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    nested = make_zip([("inner.txt", b"inner")])
    data = make_zip([
        ("docs/policy.pdf", fx.pdf(["Refund policy is 30 days"])), ("docs/leave.docx", fx.docx()),
        ("tool.exe", EXE), ("../evil.txt", b"evil"), ("inner.zip", nested), ("dup.md", fx.MD),
        ("copy/dup.md", fx.MD), ("empty.txt", b""),
    ])  # fmt: skip
    doc = await _unpack(client, a, kb, "bundle.zip", data)
    assert doc["status"] == "ready" and doc["error"] is None
    summary = doc["meta"]["archive_summary"]
    assert (summary["ingested"], summary["skipped"]) == (3, 5)
    assert doc["warnings"] == [
        "tool.exe: skipped: unsupported file type",
        "../evil.txt: refused: unsafe path",
        "inner.zip: skipped: archive inside an archive (nesting limit 1)",
        "copy/dup.md: skipped: same content as dup.md",
        "empty.txt: skipped: empty file",
    ]
    docs = await _docs(a, kb)
    members = {n for n, d in docs.items() if d.meta.get("archive") == doc["id"]}
    assert members == {
        "bundle.zip/docs/policy.pdf",
        "bundle.zip/docs/leave.docx",
        "bundle.zip/dup.md",
    }
    assert not any("evil" in n for n in docs)
    assert stub_broker.queues["ingest"].qsize() == 1 + 3  # archive + each new member
    pdf = docs["bundle.zip/docs/policy.pdf"]
    assert pdf.meta == {"size": pdf.meta["size"], "archive": doc["id"], "member": "docs/policy.pdf",
                        "depth": 1}  # fmt: skip
    await process_document(pdf.id, a.org_id)
    got = (await client.get(f"/v1/documents/{pdf.id}", headers=a.headers)).json()
    assert got["status"] == "ready" and got["mime"] == mime.PDF

    again = (await client.post(f"/v1/documents/{doc['id']}/reprocess", headers=a.headers)).json()
    await process_document(uuid.UUID(again["id"]), a.org_id)  # reprocess: no duplicates
    assert len(await _docs(a, kb)) == len(docs)
    redo = (await client.get(f"/v1/documents/{doc['id']}", headers=a.headers)).json()
    assert redo["meta"]["archive_summary"]["ingested"] == 3


async def test_tgz_with_mixed_members(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    data = make_tar([("notes/a.md", fx.MD), ("data.csv", fx.CSV), ("bin.exe", EXE)])
    doc = await _unpack(client, a, kb, "pack.tar.gz", data)
    assert doc["status"] == "ready" and doc["mime"] == mime.GZIP
    assert doc["warnings"] == ["bin.exe: skipped: unsupported file type"]
    names = {n for n, d in (await _docs(a, kb)).items() if d.meta.get("archive") == doc["id"]}
    assert names == {"pack.tar.gz/notes/a.md", "pack.tar.gz/data.csv"}


async def test_nested_archive_is_unpacked_when_depth_allows(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ingestion.config import get_ingestion_settings

    monkeypatch.setenv("ARCHIVE_MAX_DEPTH", "2")
    get_ingestion_settings.cache_clear()
    a = await register(client)
    kb = await _kb(client, a)
    inner = make_zip([("deep.zip", make_zip([("x.txt", b"x")])), ("inner.txt", b"inner")])
    doc = await _unpack(client, a, kb, "outer.zip", make_zip([("inner.zip", inner)]))
    child = (await _docs(a, kb))["outer.zip/inner.zip"]
    await process_document(child.id, a.org_id)
    docs = await _docs(a, kb)
    assert doc["status"] == "ready" and docs["outer.zip/inner.zip/inner.txt"].meta["depth"] == 2
    assert docs["outer.zip/inner.zip"].warnings == [
        "deep.zip: skipped: archive inside an archive (nesting limit 2)"
    ]


async def test_bomb_archive_fails_with_reason_and_creates_nothing(
    client: httpx.AsyncClient,
) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    doc = await _unpack(client, a, kb, "bomb.zip", make_zip([("z", b"\x00" * (20 * 2**20))]))
    assert doc["status"] == "failed" and "compressed over 100x" in doc["error"]
    assert list(await _docs(a, kb)) == ["bomb.zip"]


async def test_archive_with_nothing_usable_fails(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    doc = await _unpack(client, a, kb, "x.zip", make_zip([("a.exe", EXE)]))
    assert doc["status"] == "failed"
    assert doc["error"] == (
        "No supported files were found in this archive. (a.exe: skipped: unsupported file type)"
    )


async def test_deleting_the_archive_deletes_its_members(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    keep = await _upload(client, a, kb, "keep.txt", b"unrelated upload")
    doc = await _unpack(client, a, kb, "b.zip", make_zip([("a.md", fx.MD), ("b.csv", fx.CSV)]))
    assert len(await _docs(a, kb)) == 4
    r = await client.delete(f"/v1/documents/{doc['id']}", headers=a.headers)
    assert r.status_code == 204
    assert [d.id for d in (await _docs(a, kb)).values()] == [uuid.UUID(keep.json()["id"])]


async def test_member_already_uploaded_is_skipped_as_duplicate(client: httpx.AsyncClient) -> None:
    a = await register(client)
    kb = await _kb(client, a)
    await _upload(client, a, kb, "handbook.md", fx.MD)
    doc = await _unpack(client, a, kb, "c.zip", make_zip([("h.md", fx.MD), ("t.txt", fx.TXT)]))
    assert doc["warnings"] == ["h.md: skipped: same content as handbook.md"]
    assert doc["meta"]["archive_summary"]["ingested"] == 1
