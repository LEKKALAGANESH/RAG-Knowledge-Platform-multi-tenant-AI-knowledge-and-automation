"""Small real fixture files, built in code so they stay reviewable (no opaque binaries)."""

import io
import json

from docx import Document as new_docx
from openpyxl import Workbook


def pdf(pages: list[str | None]) -> bytes:
    """A valid PDF; each entry is one page's text (None = page with no text layer)."""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET" if text else ""
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>"
        )
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    out.write("".join(f"{o:010d} 00000 n \n" for o in offsets).encode())
    out.write(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


CORRUPT_PDF = b"%PDF-1.4\n" + bytes(range(256)) * 4 + b"\n%%EOF"


def docx() -> bytes:
    d = new_docx()
    d.add_heading("Leave Policy", level=1)
    d.add_paragraph("Employees receive 25 days of annual leave.")
    d.add_heading("Expenses", level=1)
    d.add_paragraph("Submit receipts within 30 days.")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "Item", "Limit"
    t.cell(1, 0).text, t.cell(1, 1).text = "Hotel", "200"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Invoices"
    for row in [("Invoice", "Amount"), ("INV-1", 100), ("INV-2", 250)]:
        ws.append(row)
    wb.create_sheet("Vendors").append(("Vendor", "Country"))
    wb["Vendors"].append(("Acme", "DE"))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


CSV = b"name,role\nAda,engineer\nGrace,admiral\n"
MD = b"# Intro\nWelcome to the handbook.\n\n## Security\nRotate keys every 90 days.\n"
TXT = b"Plain notes about the quarterly offsite.\n"
HTML = (
    b"<!DOCTYPE html><html><head><title>Guide</title></head><body><nav>Home | About</nav>"
    b"<article><h1>Onboarding guide</h1><p>"
    + b"New hires get a laptop on day one and meet their buddy in the first week. " * 5
    + b"</p><p>"
    + b"Security training must be completed within fourteen days of the start date. " * 5
    + b"</p></article><footer>Copyright</footer></body></html>"
)
JSON = json.dumps(
    [{"sku": "A-1", "desc": "Blue widget"}, {"sku": "B-2", "desc": "Red gadget"}]
).encode()
JSONL = b'{"q": "reset password", "a": "Use the portal"}\n{"q": "vpn", "a": "Install client"}\n'
