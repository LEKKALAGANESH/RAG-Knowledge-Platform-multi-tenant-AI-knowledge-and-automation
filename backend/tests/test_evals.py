"""Seed eval through the real pipeline and the local embedding model (AC4)."""

from collections.abc import AsyncIterator

import pytest

from app.llm.providers import LLM, ChatMessage, Delta, Usage
from evals.run_eval import (
    ABSTAIN_COSINE,
    JUDGE_SYSTEM,
    Tenant,
    answer_eval,
    answer_summary,
    eval_orgs,
    load_dataset,
    retrieve,
    summarize,
)

K = 5


@pytest.fixture(scope="module")
async def orgs() -> AsyncIterator[tuple[Tenant, Tenant]]:
    async with eval_orgs() as pair:
        yield pair


def _overall(rows: list[dict[str, object]], mode: str) -> dict[str, object]:
    return next(r for r in rows if r["mode"] == mode and r["type"] == "overall")


async def test_ac4_seed_retrieval(orgs: tuple[Tenant, Tenant]) -> None:
    items = load_dataset("seed")
    assert len(items) >= 40
    assert {i.type for i in items} >= {
        "single", "multi", "table", "cross_lang", "unanswerable",
        "ambiguous", "conflict", "injection", "permission",
    }  # fmt: skip
    results = await retrieve(*orgs, items, ["hybrid", "vector"], K, ABSTAIN_COSINE)
    rows = summarize(results, ["hybrid", "vector"])
    hybrid, vector = _overall(rows, "hybrid"), _overall(rows, "vector")
    print("AC4:", hybrid, vector)
    assert hybrid["leaked"] == 0  # the other org's docs never surface (AC1)
    assert isinstance(hybrid["recall"], float) and isinstance(vector["recall"], float)
    assert isinstance(hybrid["mrr"], float)
    assert hybrid["recall"] >= 0.80
    assert hybrid["mrr"] >= 0.6
    # Hybrid must beat vector-only on Recall@5 or MRR (A3.2: 0.974/0.942 vs 0.953/0.938).
    assert isinstance(vector["mrr"], float)
    assert hybrid["recall"] > vector["recall"] or hybrid["mrr"] > vector["mrr"]
    assert hybrid["recall"] >= vector["recall"]


class JudgedDouble:
    """Test double we own: answers with one citation, and grades as a fixed judge would."""

    provider, model = "scripted", "scripted-1"

    async def stream(
        self,
        messages: list[ChatMessage],
        system: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[Delta | Usage]:
        if system == JUDGE_SYSTEM:
            yield Delta(
                '```json\n{"faithful": true, "unsupported_claims": [], '
                '"answer_relevance": 4, "reason": "supported"}\n```'
            )
        else:
            yield Delta("Full refunds are possible within 30 days [c1].")
        yield Usage(self.provider, self.model, 10, 5)


async def test_answer_level_metrics_wiring(orgs: tuple[Tenant, Tenant]) -> None:
    """Runs /v1/answer with a scripted double: proves the parsing and scoring, not model quality."""
    double = JudgedDouble()
    items = [i for i in load_dataset("seed") if i.id in ("s01", "i02")]
    res = await answer_eval(orgs[0], items, LLM(chat=[double], rewrite=double), K)
    s = answer_summary(res)
    assert [r.id for r in res] == ["s01", "i02"]
    assert res[0].citations == 1 and res[0].citations_ok == 1  # c1 = refund_policy.md
    assert s["citation_correctness"] == 1.0  # i02's c1 is vendor_notice.txt, its top hit
    assert s["faithfulness"] == 1.0 and s["judge_invalid"] == 0
    assert s["injection_followed"] == 0
    assert isinstance(s["first_token_p50_s"], float)
