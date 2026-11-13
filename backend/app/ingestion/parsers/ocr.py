"""OCR through the tesseract binary (pytesseract); called inside the isolated parse child."""

import io
import logging
import math
import shutil

import pypdfium2 as pdfium
from PIL import Image, ImageSequence

from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import TOO_BIG, ParsedDoc, ParseError, Section

log = logging.getLogger("app.ingestion")
UNAVAILABLE = "OCR is not available on this server, so text in images cannot be extracted."


def ocr_enabled(s: IngestionSettings) -> bool:
    return s.ocr_enabled is not False and shutil.which(s.tesseract_cmd) is not None


def log_ocr_status(s: IngestionSettings) -> None:
    if ocr_enabled(s):
        log.info("OCR enabled (tesseract %s, languages %s)", s.tesseract_cmd, s.ocr_langs)
    elif s.ocr_enabled is False:
        log.info("OCR disabled (OCR_ENABLED=false): images fail, scanned PDF pages are skipped")
    else:
        log.warning(
            "OCR disabled: tesseract binary %r not found; install tesseract-ocr or set "
            "TESSERACT_CMD. Images fail, scanned PDF pages are skipped with a warning.",
            s.tesseract_cmd,
        )


def image_text(img: Image.Image, s: IngestionSettings) -> str:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = s.tesseract_cmd
    return str(pytesseract.image_to_string(img, lang=s.ocr_langs)).strip()


def _check_pixels(size: tuple[int, int], s: IngestionSettings) -> None:
    if size[0] * size[1] > s.max_image_pixels:
        raise ParseError(f"{TOO_BIG} (an image over {s.max_image_pixels:,} pixels).")


def parse_image(data: bytes, s: IngestionSettings) -> ParsedDoc:
    """One section per frame (multi-page TIFF); frames share the PDF page cap."""
    if not ocr_enabled(s):
        raise ParseError(UNAVAILABLE)
    Image.MAX_IMAGE_PIXELS = s.max_image_pixels  # PIL refuses > 2x this at open()
    try:
        img = Image.open(io.BytesIO(data))  # lazy: header only, nothing decoded yet
    except Image.DecompressionBombError:
        raise ParseError(f"{TOO_BIG} (an image over {s.max_image_pixels:,} pixels).") from None
    doc = ParsedDoc()
    for n, frame in enumerate(ImageSequence.Iterator(img), start=1):
        if n > s.max_pdf_pages:
            raise ParseError(f"The image has over {s.max_pdf_pages:,} pages; that is the limit.")
        _check_pixels(frame.size, s)
        text = image_text(frame, s)
        if text:
            doc.sections.append(Section(text, {"page": n}))
        else:
            doc.warnings.append(f"page {n}: no text found by OCR")
    return doc


def pdf_page_text(pdf: pdfium.PdfDocument, page_no: int, s: IngestionSettings) -> str:
    """Renders one PDF page (pdfium ships in the wheel: no poppler needed) and OCRs it."""
    page = pdf[page_no - 1]
    w, h = page.get_size()  # points (1/72 in); huge pages are scaled down to the pixel cap
    fit = 0.99 * math.sqrt(s.max_image_pixels / max(w * h, 1.0))  # 0.99: pdfium rounds up
    scale = min(s.ocr_dpi / 72, fit)
    return image_text(page.render(scale=scale).to_pil(), s)
