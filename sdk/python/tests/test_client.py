"""Unit tests: every resource against httpx.MockTransport, plus SSE parsing and retries."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from rag_platform import (
    AuthenticationError,
    BudgetExceededError,
    Citation,
    Client,
    ConflictError,
    Done,
    Meta,
    NotFoundError,
    PermissionDeniedError,
    RagError,
    RateLimitError,
    StreamError,
    Token,
    ValidationError,
)
from rag_platform.sse import parse

Handler = Callable[[httpx.Request], httpx.Response]


class Chunks(httpx.SyncByteStream):
    """Body delivered in the given pieces; `fail` raises a ReadError after them (dropped link)."""

    def __init__(self, *pieces: str, fail: bool = False) -> None:
        self.pieces, self.fail = pieces, fail

    def __iter__(self) -> Iterator[bytes]:
        for p in self.pieces:
            yield p.encode()
        if self.fail:
            raise httpx.ReadError("connection reset")


def sse_frames(*frames: tuple[str, Any]) -> str:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in frames)


class Recorder:
    """Routes `METHOD /path` to canned responses and keeps every request."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes, self.requests = routes, list[httpx.Request]()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        route = self.routes[f"{req.method} {req.url.path}"]
        out = route(req) if callable(route) else route
        if isinstance(out, httpx.Response):
            return out
        return httpx.Response(204) if out is None else httpx.Response(200, json=out)


def make(routes: dict[str, Any], **kw: Any) -> tuple[Client, Recorder]:
    rec = Recorder(routes)
    return Client("http://api.test/", transport=httpx.MockTransport(rec), **kw), rec


def err(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"error": {"code": code, "message": f"{code} msg", "request_id": "rid-1"}}
    return httpx.Response(status, json=body, headers=headers)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("rag_platform.client.time.sleep", slept.append)
    return slept


# -- auth -----------------------------------------------------------------------------------


def test_api_key_is_sent_as_bearer_and_hidden_from_repr() -> None:
    c, rec = make({"GET /v1/me": {"role": "owner"}}, api_key="rag_abc_secret")
    assert c.me() == {"role": "owner"}
    assert rec.requests[0].headers["authorization"] == "Bearer rag_abc_secret"
    assert "secret" not in repr(c)


def test_login_switches_to_the_access_token_then_set_credentials_to_a_key() -> None:
    pair = {"access_token": "jwt1", "refresh_token": "r", "token_type": "bearer", "expires_in": 900}
    c, rec = make({"POST /v1/auth/login": pair, "GET /v1/me": {}})
    assert c.login("a@b.c", "pw")["access_token"] == "jwt1"
    assert "authorization" not in rec.requests[0].headers
    assert json.loads(rec.requests[0].content) == {"email": "a@b.c", "password": "pw"}
    c.me()
    assert rec.requests[1].headers["authorization"] == "Bearer jwt1"
    c.set_credentials("rag_k")
    c.me()
    assert rec.requests[2].headers["authorization"] == "Bearer rag_k"


# -- knowledge bases, documents, search ------------------------------------------------------


def test_knowledge_bases() -> None:
    kb = {"id": "k1", "name": "docs"}
    c, rec = make(
        {
            "GET /v1/kbs": {"items": [kb], "next_cursor": None},
            "POST /v1/kbs": kb,
            "GET /v1/kbs/k1": kb,
            "DELETE /v1/kbs/k1": None,
        }
    )
    assert c.kbs.list(limit=5)["items"] == [kb]
    assert rec.requests[0].url.query == b"limit=5"  # None params are dropped
    assert c.kbs.create("docs")["id"] == "k1"
    assert json.loads(rec.requests[1].content) == {"name": "docs"}
    assert c.kbs.get("k1") == kb
    assert c.kbs.delete("k1") is None


def test_document_upload_from_path_bytes_and_stream(tmp_path: Any) -> None:
    doc = {"id": "d1", "status": "queued"}
    c, rec = make({"POST /v1/kbs/k1/documents": doc})
    f = tmp_path / "policy.md"
    f.write_bytes(b"# Refunds")
    assert c.documents.upload("k1", f)["id"] == "d1"
    assert c.documents.upload("k1", b"hello", filename="a.txt", mime="text/plain")["id"] == "d1"
    with f.open("rb") as fh:
        c.documents.upload("k1", fh)
    bodies = [r.content for r in rec.requests]
    assert b'name="file"; filename="policy.md"' in bodies[0] and b"# Refunds" in bodies[0]
    assert b'filename="a.txt"' in bodies[1] and b"Content-Type: text/plain" in bodies[1]
    assert b'filename="policy.md"' in bodies[2]
    with pytest.raises(ValueError):
        c.documents.upload("k1", b"x")


def test_document_reads_and_writes() -> None:
    c, rec = make(
        {
            "GET /v1/kbs/k1/documents": {"items": [], "next_cursor": None},
            "GET /v1/documents/d1": {"id": "d1"},
            "GET /v1/documents/d1/chunks": {"items": [{"id": "c1"}], "next_cursor": "n"},
            "POST /v1/documents/d1/reprocess": {"id": "d1", "status": "queued"},
            "DELETE /v1/documents/d1": None,
        }
    )
    assert c.documents.list("k1", cursor="abc")["items"] == []
    assert rec.requests[0].url.query == b"cursor=abc"
    assert c.documents.get("d1")["id"] == "d1"
    assert c.documents.chunks("d1")["next_cursor"] == "n"
    assert c.documents.reprocess("d1")["status"] == "queued"
    c.documents.delete("d1")
    assert [r.method for r in rec.requests] == ["GET", "GET", "GET", "POST", "DELETE"]


def test_wait_until_ready_polls_then_returns_and_raises_on_failure(no_sleep: list[float]) -> None:
    states = iter(["queued", "processing", "ready"])
    c, _ = make({"GET /v1/documents/d1": lambda r: {"id": "d1", "status": next(states)}})
    assert c.documents.wait_until_ready("d1", poll=0.25)["status"] == "ready"
    assert no_sleep == [0.25, 0.25]
    c, _ = make({"GET /v1/documents/d2": {"id": "d2", "status": "failed", "error": "bad pdf"}})
    with pytest.raises(RagError, match="bad pdf"):
        c.documents.wait_until_ready("d2")
    c, _ = make({"GET /v1/documents/d3": {"id": "d3", "status": "queued"}})
    with pytest.raises(TimeoutError):
        c.documents.wait_until_ready("d3", timeout=0)


def test_search_body_and_results() -> None:
    hit = {"chunk_id": "c1", "score": 0.9}
    c, rec = make({"POST /v1/search": {"results": [hit]}})
    assert c.search("refunds", kb_ids=["k1"], mode="keyword") == [hit]
    assert json.loads(rec.requests[0].content) == {
        "query": "refunds",
        "kb_ids": ["k1"],
        "mode": "keyword",
    }


# -- answer streaming --------------------------------------------------------------------------

ANSWER = sse_frames(
    ("meta", {"conversation_id": "cv", "message_id": "m"}),
    ("token", {"text": "Refunds take "}),
    (
        "citation",
        {
            "n": 1,
            "chunk_id": "c1",
            "document_id": "d1",
            "document_name": "policy.md",
            "kb_id": "k1",
            "loc": {"section": "Policy"},
            "excerpt": "Refunds are issued within 30 days.",
        },
    ),
    ("token", {"text": "30 days [c1]."}),
    ("future_event", {"x": 1}),
    ("done", {"usage": {"in_tok": 10}, "abstained": False}),
)


def sse_response(*pieces: str, status: int = 200, fail: bool = False) -> httpx.Response:
    headers = {"content-type": "text/event-stream"}
    return httpx.Response(status, headers=headers, stream=Chunks(*pieces, fail=fail))


def test_answer_stream_yields_typed_events_across_split_chunks() -> None:
    pieces = [ANSWER[i : i + 7] for i in range(0, len(ANSWER), 7)]  # frames split mid-line
    c, rec = make({"POST /v1/answer": lambda r: sse_response(*pieces)})
    events = list(c.answer.stream("refund?", kb_ids=["k1"], k=4))
    assert json.loads(rec.requests[0].content) == {"question": "refund?", "kb_ids": ["k1"], "k": 4}
    assert [type(e) for e in events] == [Meta, Token, Citation, Token, Done]
    cit = events[2]
    assert isinstance(cit, Citation) and cit.document_name == "policy.md" and cit.n == 1


def test_ask_collects_text_and_citations() -> None:
    c, _ = make({"POST /v1/answer": lambda r: sse_response(ANSWER)})
    a = c.answer.ask("refund?")
    assert a.text == "Refunds take 30 days [c1]."
    assert [x.chunk_id for x in a.citations] == ["c1"]
    assert (a.conversation_id, a.message_id, a.abstained) == ("cv", "m", False)
    assert a.usage == {"in_tok": 10}


def test_error_event_ends_the_stream_and_ask_raises() -> None:
    body = sse_frames(
        ("meta", {"conversation_id": "cv", "message_id": "m"}),
        ("token", {"text": "partial"}),
        ("error", {"code": "llm_timeout", "message": "The model timed out."}),
    )
    c, _ = make({"POST /v1/answer": lambda r: sse_response(body)})
    assert list(c.answer.stream("q"))[-1] == StreamError("llm_timeout", "The model timed out.")
    with pytest.raises(RagError) as exc:
        c.answer.ask("q")
    assert exc.value.code == "llm_timeout"


def test_answer_refused_before_the_stream_is_not_retried() -> None:
    c, rec = make({"POST /v1/answer": err(402, "budget_exceeded")})
    with pytest.raises(BudgetExceededError) as exc:
        c.answer.ask("q")
    assert exc.value.status == 402 and exc.value.request_id == "rid-1"
    c, rec = make({"POST /v1/answer": err(429, "rate_limited", {"Retry-After": "7"})})
    with pytest.raises(RateLimitError) as rl:
        list(c.answer.stream("q"))
    assert rl.value.retry_after == 7 and len(rec.requests) == 1


def test_sse_parser_multiline_data_comments_crlf_and_a_cut_frame() -> None:
    lines = [
        ": keep-alive",
        "event: step",
        'data: {"n": 1,',
        'data:  "kind": "plan"}',
        "",
        "event: status\r",
        'data: {"status": "running"}\r',
        "\r",
        "event: step",
        'data: {"n": 2',  # stream cut here: no blank line
    ]
    events = list(parse(lines))
    assert [(e.event, e.data) for e in events] == [
        ("step", {"n": 1, "kind": "plan"}),
        ("status", {"status": "running"}),
    ]


# -- agent ----------------------------------------------------------------------------------


def test_agent_run_lifecycle_and_approval_and_report() -> None:
    run = {"id": "r1", "status": "planning"}
    c, rec = make(
        {
            "POST /v1/agent/runs": run,
            "GET /v1/agent/runs": {"items": [run]},
            "GET /v1/agent/runs/r1": {**run, "steps": [], "approvals": []},
            "POST /v1/agent/runs/r1/cancel": {**run, "status": "cancelled"},
            "POST /v1/approvals/a1": {"id": "a1", "status": "approved"},
            "GET /v1/reports/rep1": httpx.Response(200, content=b"PK\x03\x04xlsx"),
        }
    )
    assert c.agent.create("Compare invoices", kb_ids=["k1"], max_steps=10)["id"] == "r1"
    assert json.loads(rec.requests[0].content) == {
        "goal": "Compare invoices",
        "kb_ids": ["k1"],
        "max_steps": 10,
    }
    assert c.agent.list(limit=3) == [run]
    assert c.agent.get("r1")["steps"] == []
    assert c.agent.cancel("r1")["status"] == "cancelled"
    assert c.approvals.decide("a1", "approve")["status"] == "approved"
    assert json.loads(rec.requests[4].content) == {"decision": "approve"}
    assert c.reports.download("rep1").startswith(b"PK")


def events_route(*responses: httpx.Response) -> tuple[dict[str, Any], list[str]]:
    afters: list[str] = []
    it = iter(responses)

    def route(req: httpx.Request) -> httpx.Response:
        afters.append(req.url.params["after"])
        return next(it)

    return {"GET /v1/agent/runs/r1/events": route}, afters


def test_agent_events_reconnect_with_after_on_drop_timeout_and_close() -> None:
    first = sse_frames(("step", {"n": 1}), ("step", {"n": 2}))
    routes, afters = events_route(
        sse_response(first, fail=True),  # connection reset after step 2
        sse_response(sse_frames(("step", {"n": 3}), ("timeout", {"after": 3}))),
        sse_response(sse_frames(("approval", {"id": "a1", "status": "pending"}))),  # closed early
        sse_response(
            sse_frames(("status", {"status": "succeeded"}), ("end", {"status": "succeeded"}))
        ),
    )
    c, _ = make(routes)
    seen = [(e.event, e.data.get("n")) for e in c.agent.events("r1")]
    assert afters == ["0", "2", "3", "3"]
    assert seen == [
        ("step", 1),
        ("step", 2),
        ("step", 3),
        ("timeout", None),
        ("approval", None),
        ("status", None),
        ("end", None),
    ]


def test_agent_events_give_up_after_max_reconnects_and_raise_real_errors() -> None:
    routes, afters = events_route(*[sse_response("", fail=True) for _ in range(3)])
    c, _ = make(routes)
    with pytest.raises(RagError) as exc:
        list(c.agent.events("r1", after=5, max_reconnects=2))
    assert exc.value.code == "stream_lost" and afters == ["5", "5", "5"]
    c, _ = make({"GET /v1/agent/runs/r1/events": err(404, "not_found")})
    with pytest.raises(NotFoundError):
        list(c.agent.events("r1"))


# -- sources, keys, conversations --------------------------------------------------------------

REST_CONFIG = {
    "base_url": "https://api.example.com/v1",
    "endpoints": [{"path": "/articles", "id_field": "id", "text_fields": ["body"]}],
    "auth": {"kind": "bearer"},
}


def test_sources() -> None:
    src = {"id": "s1", "type": "rest"}
    run = {"id": "run1", "status": "running"}
    c, rec = make(
        {
            "GET /v1/connectors": {"items": [{"type": "rest"}]},
            "POST /v1/connectors/rest/openapi": {"config": REST_CONFIG, "skipped": []},
            "POST /v1/kbs/k1/sources": src,
            "GET /v1/kbs/k1/sources": {"items": [src]},
            "GET /v1/sources/s1": src,
            "PATCH /v1/sources/s1": {**src, "name": "new"},
            "POST /v1/sources/s1/test": {"ok": True, "message": "ok", "schema": None},
            "POST /v1/sources/s1/sync": run,
            "GET /v1/sources/s1/runs": {"items": [run]},
            "DELETE /v1/sources/s1": None,
        }
    )
    assert c.sources.connectors()[0]["type"] == "rest"
    assert c.sources.suggest_rest_config("openapi: 3.0.0")["config"] == REST_CONFIG
    created = c.sources.create(
        "k1", "rest", "Articles", REST_CONFIG, secret="tok", schedule_minutes=60
    )
    assert created == src
    assert json.loads(rec.requests[2].content) == {
        "type": "rest",
        "name": "Articles",
        "config": REST_CONFIG,
        "secret": "tok",
        "schedule_minutes": 60,
    }
    assert c.sources.list("k1") == [src]
    assert c.sources.get("s1") == src
    assert c.sources.update("s1", name="new", enabled=False)["name"] == "new"
    assert json.loads(rec.requests[5].content) == {"name": "new", "enabled": False}
    assert c.sources.test("s1")["ok"] is True
    assert c.sources.sync("s1", full=True) == run
    assert rec.requests[7].url.query == b"full=true"
    c.sources.sync("s1")
    assert rec.requests[8].url.query == b""
    assert c.sources.runs("s1") == [run]
    c.sources.delete("s1")


def test_api_keys_and_conversations() -> None:
    key = {"id": "key1", "key": "rag_p_s", "scopes": ["search"]}
    c, rec = make(
        {
            "POST /v1/api-keys": key,
            "GET /v1/api-keys": [key],
            "DELETE /v1/api-keys/key1": None,
            "GET /v1/conversations": {"items": [], "next_cursor": None},
            "GET /v1/conversations/cv": {"id": "cv", "messages": []},
        }
    )
    assert c.api_keys.create("ci", ["search", "answer"])["key"] == "rag_p_s"
    assert json.loads(rec.requests[0].content) == {"name": "ci", "scopes": ["search", "answer"]}
    assert c.api_keys.list() == [key]
    c.api_keys.revoke("key1")
    assert c.conversations.list(limit=2)["items"] == []
    assert c.conversations.get("cv")["messages"] == []


# -- errors and retries ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (401, "invalid_api_key", AuthenticationError),
        (402, "budget_exceeded", BudgetExceededError),
        (403, "insufficient_scope", PermissionDeniedError),
        (404, "not_found", NotFoundError),
        (409, "sync_running", ConflictError),
        (422, "validation_error", ValidationError),
        (500, "internal_error", RagError),
    ],
)
def test_error_mapping(status: int, code: str, cls: type[RagError]) -> None:
    c, _ = make({"POST /v1/kbs": err(status, code)})
    with pytest.raises(cls) as exc:
        c.kbs.create("x")
    assert (exc.value.code, exc.value.status, exc.value.request_id) == (code, status, "rid-1")
    assert exc.value.message == f"{code} msg"


def test_non_json_error_body_falls_back_to_the_status() -> None:
    resp = httpx.Response(502, text="<html>bad gateway</html>", headers={"x-request-id": "edge"})
    c, _ = make({"GET /v1/kbs/k1": resp})
    with pytest.raises(RagError) as exc:
        c.kbs.get("k1")
    assert (exc.value.code, exc.value.request_id) == ("http_502", "edge")


def test_get_retries_429_and_503_honoring_retry_after(no_sleep: list[float]) -> None:
    replies = iter(
        [
            err(429, "rate_limited", {"Retry-After": "3"}),
            err(503, "busy"),
            httpx.Response(200, json={}),
        ]
    )
    c, rec = make({"GET /v1/kbs/k1": lambda r: next(replies)})
    assert c.kbs.get("k1") == {}
    assert len(rec.requests) == 3 and no_sleep == [3.0, 1.0]  # Retry-After, then backoff 0.5*2


def test_retries_are_bounded_and_capped(no_sleep: list[float]) -> None:
    c, rec = make(
        {"GET /v1/kbs/k1": err(429, "rate_limited", {"Retry-After": "3600"})}, max_retries=1
    )
    with pytest.raises(RateLimitError) as exc:
        c.kbs.get("k1")
    assert exc.value.retry_after == 3600 and len(rec.requests) == 2 and no_sleep == [60.0]


def test_non_get_is_never_retried() -> None:
    c, rec = make({"POST /v1/search": err(429, "rate_limited", {"Retry-After": "1"})})
    with pytest.raises(RateLimitError):
        c.search("q")
    c, rec2 = make({"POST /v1/kbs": err(503, "busy")})
    with pytest.raises(RagError):
        c.kbs.create("x")
    assert len(rec.requests) == len(rec2.requests) == 1
