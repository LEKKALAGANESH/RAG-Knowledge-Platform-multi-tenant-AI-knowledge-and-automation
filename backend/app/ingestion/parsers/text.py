import re

from app.ingestion.parsers import ParsedDoc, Section, decode

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def markdown_sections(text: str) -> list[Section]:
    sections: list[Section] = []
    heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(body, {"section": heading}))

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            heading, buf = m.group(1), []
        buf.append(line)
    flush()
    return sections


def parse_md(data: bytes) -> ParsedDoc:
    return ParsedDoc(markdown_sections(decode(data)))


def parse_txt(data: bytes) -> ParsedDoc:
    text = decode(data).strip()
    return ParsedDoc([Section(text, {})] if text else [])
