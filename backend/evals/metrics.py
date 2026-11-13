"""Retrieval metrics as pure functions over one ranked list.

`matches[i]` is the index of the relevant entry that hit i satisfies, or None. A relevant entry
counts once: a second chunk of the same relevant doc adds precision but no recall or gain.
Metrics with no relevant entries (unanswerable items) are undefined and return None."""

import math
from statistics import mean


def judge(hits: list[tuple[str, str]], relevant: list[dict[str, str]]) -> list[int | None]:
    """hits: (document_name, chunk_text). A hit matches an entry when the doc is equal and the
    entry's optional `loc_match` substring occurs in the chunk text."""
    out: list[int | None] = []
    for name, text in hits:
        found = None
        for i, rel in enumerate(relevant):
            if rel["doc"] == name and rel.get("loc_match", "") in text:
                found = i
                break
        out.append(found)
    return out


def recall_at_k(matches: list[int | None], n_rel: int, k: int) -> float | None:
    if n_rel == 0:
        return None
    return len({m for m in matches[:k] if m is not None}) / n_rel


def precision_at_k(matches: list[int | None], n_rel: int, k: int) -> float | None:
    if n_rel == 0:
        return None
    return sum(m is not None for m in matches[:k]) / k


def reciprocal_rank(matches: list[int | None], n_rel: int) -> float | None:
    if n_rel == 0:
        return None
    return next((1 / r for r, m in enumerate(matches, start=1) if m is not None), 0.0)


def ndcg_at_k(matches: list[int | None], n_rel: int, k: int) -> float | None:
    """Binary gain; a relevant entry's first hit gains 1, repeats gain 0 (keeps nDCG <= 1)."""
    if n_rel == 0:
        return None
    seen: set[int] = set()
    dcg = 0.0
    for i, m in enumerate(matches[:k]):
        if m is not None and m not in seen:
            seen.add(m)
            dcg += 1 / math.log2(i + 2)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(n_rel, k)))
    return dcg / idcg


def avg(values: list[float | None]) -> float | None:
    """Mean over defined values; None when every value is undefined."""
    defined = [v for v in values if v is not None]
    return mean(defined) if defined else None
