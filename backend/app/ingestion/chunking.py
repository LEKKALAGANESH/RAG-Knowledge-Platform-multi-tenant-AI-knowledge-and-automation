"""Section-aware chunking: chunks never cross a section/page/sheet boundary."""

from collections.abc import Iterator

from app.ingestion.parsers import Loc, Section, Table, render_table


def count_tokens(text: str) -> int:
    # ponytail: whitespace words ~ tokens; use the embedder tokenizer if limits bite.
    return len(text.split())


def _units(text: str, size: int) -> Iterator[str]:
    """Lines, with any line longer than `size` split into word windows."""
    for line in text.splitlines():
        words = line.split()
        if len(words) <= size:
            yield line
        else:
            yield from (" ".join(words[i : i + size]) for i in range(0, len(words), size))


def split_text(text: str, size: int, overlap: int) -> list[str]:
    overlap = min(overlap, size // 2)
    chunks: list[str] = []
    cur: list[str] = []
    for unit in _units(text, size):
        if cur and count_tokens("\n".join([*cur, unit])) > size:
            chunks.append("\n".join(cur).strip())
            tail: list[str] = []
            for prev in reversed(cur):  # carry trailing lines as overlap
                if count_tokens("\n".join([prev, *tail])) > overlap:
                    break
                tail.insert(0, prev)
            cur = tail
        cur.append(unit)
    if "\n".join(cur).strip():
        chunks.append("\n".join(cur).strip())
    return [c for c in chunks if c]


def _range_loc(loc: Loc, t: Table, first: int, last: int) -> Loc:
    return {**loc, "range": f"{t.cols[0]}{first}:{t.cols[1]}{last}"} if t.cols else loc


def table_chunks(s: Section, t: Table, cap: int) -> list[tuple[str, Loc]]:
    """Whole table if it fits `cap`, else row groups that each repeat the header."""
    if count_tokens(s.text) <= cap or not t.rows:
        return [(s.text, _range_loc(s.loc, t, t.header_row, t.header_row + len(t.rows)))]
    out: list[tuple[str, Loc]] = []
    budget = cap - count_tokens(render_table(t.header, []))
    start, used = 0, 0
    for i, row in enumerate(t.rows):
        n = count_tokens(" | ".join(row)) + 1
        if i > start and used + n > budget:
            out.append(_group(s, t, start, i))
            start, used = i, 0
        used += n
    out.append(_group(s, t, start, len(t.rows)))
    return out


def _group(s: Section, t: Table, start: int, end: int) -> tuple[str, Loc]:
    first_data_row = t.header_row + 1
    loc = _range_loc(s.loc, t, first_data_row + start, first_data_row + end - 1)
    return render_table(t.header, t.rows[start:end]), loc


# Word counts miss text without whitespace (one 40k-char "word" = 1 token here, thousands at the
# provider, which then rejects the whole batch). Normal prose is ~6 chars/word.
CHARS_PER_TOKEN_CAP = 10


def chunk_sections(
    sections: list[Section], size: int, overlap: int, table_cap: int
) -> list[tuple[str, Loc]]:
    out: list[tuple[str, Loc]] = []
    for s in sections:
        if s.table is not None:
            pieces, cap = table_chunks(s, s.table, table_cap), table_cap * CHARS_PER_TOKEN_CAP
        else:
            pieces = [(text, s.loc) for text in split_text(s.text, size, overlap)]
            cap = size * CHARS_PER_TOKEN_CAP
        out += [(text[i : i + cap], loc) for text, loc in pieces for i in range(0, len(text), cap)]
    return out
