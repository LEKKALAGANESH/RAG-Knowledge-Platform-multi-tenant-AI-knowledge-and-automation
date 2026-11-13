# ADR 0003: Relative score fusion instead of RRF for hybrid search

Status: Accepted (A3.2)

## Context

Hybrid search merges a vector list and a keyword list. The first design used reciprocal rank fusion
(RRF). After fixing the keyword query (it AND-ed every word, so it returned nothing for natural
questions), hybrid with RRF scored **below** vector-only on the 47-item seed eval: R@5 0.923 / MRR
0.865 vs 0.953 / 0.938. On cross-language items a weak keyword match that also sat in the vector
top-20 outranked the vector's rank-1 hit.

## Decision

Relative score fusion (`retrieval/search.py::fuse`): min-max scale each list to [0, 1], then sum with
weights; each vector list weighs 1.0, keyword 0.5. Hybrid result: R@5 0.974, MRR 0.942, nDCG@5 0.936.

Also: answers abstain without calling the model when every vector hit is below a per-model cosine
cut (`MIN_COSINE`, MiniLM 0.32). Untuned models get 0.0 (gate off).

## Consequences

- Hybrid beats vector-only on both metrics (AC4 met for real, not trivially).
- Scores are relative within one query: the fused score (0 to 1.5) is not comparable across queries,
  so the abstain gate reads the raw cosine kept on each hit.
- Weights and the gate are tuned on the same small set they are scored on. Grow the eval set before
  changing them.

## Alternatives considered

- **RRF:** tried with keyword weight 1 / 0.5 / 0.3 and k 60 / 20; best 0.940 / 0.871, still below
  vector-only.
- **Keyword weight 1.0** (0.974 / 0.906) and **0.33** (0.953 / 0.968): each wins one metric only.
- **Per-document cap of 2 or 3 hits:** no change; reverted.
- **Gate at 0.39:** best abstention score (0.957) but abstains on an answerable item.
