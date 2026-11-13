import csv
import io

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.ingestion.parsers import ParsedDoc, ParseError, Table, decode, table_section


def _cell(v: object) -> str:
    return "" if v is None else str(v).strip()


def _table(rows: list[list[str]], first_row: int, first_col: int) -> Table | None:
    """rows[0] is the header; first_row/first_col are the 1-based sheet coords of rows[0][0]."""
    while rows and not any(rows[-1]):
        rows.pop()
    while rows and not any(rows[0]):
        rows, first_row = rows[1:], first_row + 1
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    cols = (get_column_letter(first_col), get_column_letter(first_col + width - 1))
    return Table(rows[0], rows[1:], first_row, cols)


def parse_csv(data: bytes) -> ParsedDoc:
    t = _table([list(r) for r in csv.reader(io.StringIO(decode(data)))], 1, 1)
    return ParsedDoc([table_section(t, {})] if t else [])


def parse_xlsx(data: bytes, max_cells: int) -> ParsedDoc:
    # read_only streams the sheet XML: memory follows the cells kept, capped at max_cells.
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    doc, cells = ParsedDoc(), 0
    try:
        for ws in wb.worksheets:
            rows: list[list[str]] = []
            # iter_rows starts at A1 by default; rows[0][0] must be (min_row, min_column).
            for r in ws.iter_rows(min_row=ws.min_row, min_col=ws.min_column, values_only=True):
                cells += len(r)
                if cells > max_cells:
                    raise ParseError(f"The workbook has more than {max_cells:,} cells.")
                rows.append([_cell(v) for v in r])
            t = _table(rows, ws.min_row, ws.min_column)
            if t is None:
                doc.warnings.append(f"sheet {ws.title}: empty")
            else:
                doc.sections.append(table_section(t, {"sheet": ws.title}))
    finally:
        wb.close()
    return doc
