"""CSV text for downloads that is safe to open in a spreadsheet."""

import csv
import io
from collections.abc import Iterable

# OWASP CSV injection: a cell starting with one of these runs as a formula in Excel/Sheets.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(FORMULA_START) else s


def csv_text(rows: Iterable[Iterable[object]]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows([_cell(v) for v in row] for row in rows)
    return buf.getvalue()
