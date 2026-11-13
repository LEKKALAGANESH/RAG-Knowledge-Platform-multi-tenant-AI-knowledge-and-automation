import io

import pypdfium2 as pdfium
from pypdf import PdfReader

from app.ingestion.config import IngestionSettings
from app.ingestion.parsers import ParsedDoc, ParseError, Section
from app.ingestion.parsers.ocr import ocr_enabled, pdf_page_text


def parse(data: bytes, s: IngestionSettings) -> ParsedDoc:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ParseError("The PDF is password-protected.")
    if len(reader.pages) > s.max_pdf_pages:
        raise ParseError(
            f"The PDF has {len(reader.pages):,} pages; the limit is {s.max_pdf_pages:,}."
        )
    ocr = ocr_enabled(s)
    rendered: pdfium.PdfDocument | None = None  # opened only once a page needs OCR
    doc = ParsedDoc()
    for n, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text and ocr:
            rendered = rendered or pdfium.PdfDocument(data)
            text = pdf_page_text(rendered, n, s)
            if not text:
                doc.warnings.append(f"page {n}: no text found by OCR")
                continue
        if text:
            doc.sections.append(Section(text, {"page": n}))
        else:
            doc.warnings.append(f"page {n}: no text, OCR not enabled")
    return doc
