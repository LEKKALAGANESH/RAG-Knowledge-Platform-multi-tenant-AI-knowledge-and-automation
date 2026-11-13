import trafilatura

from app.ingestion.parsers import ParsedDoc, ParseError, decode
from app.ingestion.parsers.text import markdown_sections


def parse(data: bytes) -> ParsedDoc:
    md = trafilatura.extract(decode(data), output_format="markdown", include_tables=True)
    if not md:
        raise ParseError("No main content could be extracted from the HTML page.")
    return ParsedDoc(markdown_sections(md))
