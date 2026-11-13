import math

import pytest

from evals.metrics import avg, judge, ndcg_at_k, precision_at_k, recall_at_k, reciprocal_rank

L2, L3, L4 = math.log2(2), math.log2(3), math.log2(4)


def test_judge_matches_doc_and_optional_loc() -> None:
    rel = [{"doc": "a.md"}, {"doc": "b.xlsx", "loc_match": "INV-3"}]
    hits = [("x.md", "t"), ("b.xlsx", "INV-1 row"), ("b.xlsx", "INV-3 row"), ("a.md", "any")]
    assert judge(hits, rel) == [None, None, 1, 0]


def test_hand_computed_ranking() -> None:
    # relevant entries 0 and 1; hits: miss, rel0, miss, rel1, miss
    m = [None, 0, None, 1, None]
    assert recall_at_k(m, 2, 5) == 1.0
    assert recall_at_k(m, 2, 3) == 0.5
    assert precision_at_k(m, 2, 5) == pytest.approx(2 / 5)
    assert reciprocal_rank(m, 2) == 0.5
    expected = (1 / L3 + 1 / math.log2(5)) / (1 / L2 + 1 / L3)
    assert ndcg_at_k(m, 2, 5) == pytest.approx(expected)


def test_all_relevant_at_top_is_perfect() -> None:
    m: list[int | None] = [0, 1, 2]
    assert recall_at_k(m, 3, 3) == 1.0
    assert precision_at_k(m, 3, 3) == 1.0
    assert reciprocal_rank(m, 3) == 1.0
    assert ndcg_at_k(m, 3, 3) == pytest.approx(1.0)


def test_no_relevant_entries_is_undefined_not_zero() -> None:
    m: list[int | None] = [None, None]
    assert recall_at_k(m, 0, 5) is None
    assert precision_at_k(m, 0, 5) is None
    assert reciprocal_rank(m, 0) is None
    assert ndcg_at_k(m, 0, 5) is None
    assert avg([None, None]) is None
    assert avg([None, 1.0, 0.0]) == 0.5


def test_no_hits_found_scores_zero() -> None:
    assert recall_at_k([], 2, 5) == 0.0
    assert reciprocal_rank([None, None], 1) == 0.0
    assert ndcg_at_k([None], 1, 5) == 0.0


def test_ties_same_relevant_doc_twice_counts_once_for_recall_and_gain() -> None:
    # Two chunks of the same relevant doc (equal scores, either order): recall and nDCG must not
    # exceed 1 or double count; precision counts both relevant chunks.
    m: list[int | None] = [0, 0, None]
    assert recall_at_k(m, 1, 3) == 1.0
    assert ndcg_at_k(m, 1, 3) == pytest.approx(1.0)
    assert precision_at_k(m, 1, 3) == pytest.approx(2 / 3)
    # Fewer relevant than k: ideal DCG uses only n_rel positions.
    assert ndcg_at_k([None, 0], 1, 5) == pytest.approx((1 / L3) / (1 / L2))
    assert ndcg_at_k([None, None, None, 0], 1, 3) == 0.0  # beyond k
    assert 1 / L4 < 1 / L3  # sanity: later ranks gain less
