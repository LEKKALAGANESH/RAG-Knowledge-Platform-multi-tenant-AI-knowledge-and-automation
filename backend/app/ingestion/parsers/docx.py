import io

from docx import Document as load_docx
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph

from app.ingestion.parsers import ParsedDoc, Section, Table, table_section


def parse(data: bytes) -> ParsedDoc:
    d = load_docx(io.BytesIO(data))
    doc, heading, buf, n_tables = ParsedDoc(), None, list[str](), 0

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            doc.sections.append(Section(body, {"section": heading}))
        buf.clear()

    for el in d.element.body.iterchildren():
        if el.tag.endswith("}p"):
            p = Paragraph(el, d)
            style = p.style.name if p.style is not None and p.style.name else ""
            if style.startswith(("Heading", "Title")) and p.text.strip():
                flush()
                heading = p.text.strip()
            buf.append(p.text)
        elif el.tag.endswith("}tbl"):
            flush()
            rows = [[c.text.strip() for c in r.cells] for r in DocxTable(el, d).rows]
            if rows:
                n_tables += 1
                t = Table(rows[0], rows[1:])
                doc.sections.append(table_section(t, {"section": heading, "table": n_tables}))
    flush()
    return doc
