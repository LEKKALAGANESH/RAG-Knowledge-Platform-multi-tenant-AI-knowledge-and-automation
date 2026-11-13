"""B8 OCR: images and image-only PDF pages via tesseract (mocked; real run if installed)."""

import io
import logging
import shutil
import uuid

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.ingestion import mime
from app.ingestion.config import IngestionSettings, get_ingestion_settings
from app.ingestion.parsers import ParseError, parse, parse_isolated
from app.ingestion.parsers import ocr as ocr_mod
from app.ingestion.pipeline import process_document
from tests import fixtures as fx
from tests.conftest import register
from tests.test_ingestion import _kb, _upload, stub_broker  # noqa: F401 - autouse fixture

HAVE_TESSERACT = shutil.which("tesseract") is not None
MISSING = "no-such-tesseract-binary"


def image(fmt: str = "PNG", size: tuple[int, int] = (200, 80), frames: int = 1) -> bytes:
    imgs = [Image.new("RGB", size, "white") for _ in range(frames)]
    out = io.BytesIO()
    imgs[0].save(out, fmt, save_all=frames > 1, append_images=imgs[1:])
    return out.getvalue()


@pytest.fixture
def fake_ocr(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """tesseract 'installed' and returning fixed text; records the size of each OCR'd image."""
    import pytesseract

    seen: list[tuple[int, int]] = []

    def to_string(img: Image.Image, lang: str) -> str:
        assert lang == "eng+deu"
        seen.append(img.size)
        return f"scanned text {len(seen)}\n"

    monkeypatch.setattr(ocr_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(pytesseract, "image_to_string", to_string)
    return seen


def settings(**kw: object) -> IngestionSettings:
    kw.setdefault("ocr_enabled", None)  # auto; the shared fixture's OCR_ENABLED=false is for others
    return IngestionSettings(ocr_langs="eng+deu", **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fmt", "kind"),
    [("PNG", mime.PNG), ("JPEG", mime.JPEG), ("TIFF", mime.TIFF), ("BMP", mime.BMP),
     ("WEBP", mime.WEBP)],
)  # fmt: skip
def test_image_formats_are_detected(fmt: str, kind: str) -> None:
    assert mime.detect_mime(image(fmt), "x") == kind


def test_text_starting_with_bm_is_not_a_bitmap() -> None:
    assert mime.detect_mime(b"BMW service notes for the fleet\n", "n.txt") == mime.TXT


def test_image_is_ocrd_into_a_page_section(fake_ocr: list[tuple[int, int]]) -> None:
    doc = parse(mime.PNG, image(), settings())
    assert [(x.text, x.loc) for x in doc.sections] == [("scanned text 1", {"page": 1})]
    assert fake_ocr == [(200, 80)]


def test_multi_page_tiff_gives_one_section_per_page(fake_ocr: list[tuple[int, int]]) -> None:
    doc = parse(mime.TIFF, image("TIFF", frames=3), settings())
    assert [x.loc for x in doc.sections] == [{"page": 1}, {"page": 2}, {"page": 3}]
    with pytest.raises(ParseError, match="over 2 pages"):
        parse(mime.TIFF, image("TIFF", frames=3), settings(max_pdf_pages=2))


def test_image_only_pdf_page_is_rendered_and_ocrd(fake_ocr: list[tuple[int, int]]) -> None:
    doc = parse(mime.PDF, fx.pdf(["Typed page", None]), settings())
    assert [(x.text, x.loc) for x in doc.sections] == [
        ("Typed page", {"page": 1}),
        ("scanned text 1", {"page": 2}),
    ]
    (w, h), = fake_ocr  # fmt: skip
    assert (w, round(h, -1)) == (2550, 3300) and doc.warnings == []  # letter page at 300 dpi


def test_pdf_render_is_scaled_down_to_the_pixel_cap(fake_ocr: list[tuple[int, int]]) -> None:
    parse(mime.PDF, fx.pdf([None]), settings(max_image_pixels=1_000_000))
    (w, h), = fake_ocr  # fmt: skip
    assert w * h <= 1_000_000 and w > 800


def test_ocr_finding_nothing_is_a_warning(
    fake_ocr: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ocr_mod, "image_text", lambda img, s: "")
    doc = parse(mime.PDF, fx.pdf(["Typed", None]), settings())
    assert doc.warnings == ["page 2: no text found by OCR"]


def test_image_over_pixel_cap_is_refused(fake_ocr: list[tuple[int, int]]) -> None:
    for cap in (10_000, 5_000):  # 16,000 px: over the cap, then over PIL's 2x bomb threshold
        with pytest.raises(ParseError, match="pixels"):
            parse(mime.PNG, image(size=(200, 80)), settings(max_image_pixels=cap))
    assert fake_ocr == []


def test_without_tesseract_images_fail_readably_and_pdfs_keep_the_warning() -> None:
    s = settings(tesseract_cmd=MISSING)
    assert not ocr_mod.ocr_enabled(settings(ocr_enabled=True, tesseract_cmd=MISSING))
    with pytest.raises(ParseError, match="OCR is not available on this server"):
        parse(mime.PNG, image(), s)
    assert parse(mime.PDF, fx.pdf(["x", None]), s).warnings == ["page 2: no text, OCR not enabled"]


def test_ocr_enabled_false_wins_over_an_installed_binary(fake_ocr: list[tuple[int, int]]) -> None:
    assert ocr_mod.ocr_enabled(settings()) and not ocr_mod.ocr_enabled(settings(ocr_enabled=False))


def test_ocr_langs_are_validated() -> None:
    with pytest.raises(ValueError, match="ocr_langs"):
        IngestionSettings(ocr_langs="eng; rm -rf /")


def test_startup_log_says_why_ocr_is_off(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, "app.ingestion"):
        ocr_mod.log_ocr_status(settings(tesseract_cmd=MISSING))
        ocr_mod.log_ocr_status(settings(ocr_enabled=False))
    assert "tesseract binary 'no-such-tesseract-binary' not found" in caplog.text
    assert "OCR_ENABLED=false" in caplog.text


def test_image_runs_in_the_isolated_child() -> None:
    with pytest.raises(ParseError, match="OCR is not available"):
        parse_isolated(mime.PNG, image(), settings(tesseract_cmd=MISSING))


async def test_image_upload_without_ocr_fails_with_reason(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TESSERACT_CMD", MISSING)
    get_ingestion_settings.cache_clear()
    a = await register(client)
    r = await _upload(client, a, await _kb(client, a), "scan.png", image())
    assert r.status_code == 201 and r.json()["mime"] == mime.PNG
    await process_document(uuid.UUID(r.json()["id"]), a.org_id)
    doc = (await client.get(f"/v1/documents/{r.json()['id']}", headers=a.headers)).json()
    assert doc["status"] == "failed" and doc["error"].startswith("OCR is not available")


@pytest.mark.skipif(not HAVE_TESSERACT, reason="tesseract binary not installed on this machine")
def test_real_tesseract_reads_rendered_text() -> None:
    img = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(img).text((20, 40), "INVOICE 4711", fill="black",
                             font=ImageFont.load_default(size=64))  # fmt: skip
    out = io.BytesIO()
    img.save(out, "PNG")
    doc = parse_isolated(mime.PNG, out.getvalue(), IngestionSettings())
    assert "INVOICE 4711" in doc.sections[0].text
