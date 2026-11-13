"""Sync client for the RAG platform API.

ponytail: sync-only (httpx.Client). Async callers can wrap calls in `asyncio.to_thread`; add an
httpx.AsyncClient twin when an async-native consumer shows up.
"""

from __future__ import annotations

import builtins
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Literal

import httpx

from . import sse
from .errors import RagError, from_response, retry_after
from .types import (
    KB,
    ApiKey,
    ApiKeyCreated,
    Approval,
    ChunkPage,
    ConversationPage,
    Document,
    DocumentPage,
    KBPage,
    Run,
    RunDetail,
    Scope,
    SearchResult,
    Source,
    SourceRun,
    SourceTest,
    TokenPair,
)

FileInput = str | os.PathLike[str] | bytes | IO[bytes]
RETRY_STATUSES = (429, 503)
MAX_RETRY_DELAY = 60.0


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class Client:
    """`Client(base_url, api_key=...)` (recommended) or `access_token=...`, or call `login()`.

    GETs that get 429/503 are retried up to `max_retries` times, waiting Retry-After (capped at
    60 s) or exponential backoff. Other methods are never retried automatically.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "rag-platform-python/0.1.0"},
        )
        self._token = api_key or access_token
        self.max_retries = max_retries
        self.kbs = KnowledgeBases(self)
        self.documents = Documents(self)
        self.answer = Answers(self)
        self.conversations = Conversations(self)
        self.agent = Agent(self)
        self.approvals = Approvals(self)
        self.reports = Reports(self)
        self.sources = Sources(self)
        self.api_keys = ApiKeys(self)

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:  # never show the credential
        return f"Client({str(self._http.base_url)!r})"

    def set_credentials(self, token: str) -> None:
        """Switch to another API key or access token."""
        self._token = token

    def login(self, email: str, password: str) -> TokenPair:
        """Password login (JWT, 15 min). Later calls use the access token."""
        pair: TokenPair = self.request(
            "POST", "/v1/auth/login", json_body={"email": email, "password": password}
        )
        self._token = pair["access_token"]
        return pair

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/v1/me")

    def search(
        self,
        query: str,
        kb_ids: list[str] | None = None,
        k: int | None = None,
        mode: Literal["hybrid", "vector", "keyword"] | None = None,
    ) -> list[SearchResult]:
        body = _clean({"query": query, "kb_ids": kb_ids, "k": k, "mode": mode})
        return self.request("POST", "/v1/search", json_body=body)["results"]

    # -- transport -------------------------------------------------------------------------

    def _send(self, method: str, path: str, *, stream: bool = False, **kw: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        attempt = 0
        while True:
            req = self._http.build_request(method, path, headers=headers, **kw)
            r = self._http.send(req, stream=stream)
            if r.status_code < 400:
                return r
            r.read()
            r.close()
            retryable = method == "GET" and r.status_code in RETRY_STATUSES
            if not retryable or attempt >= self.max_retries:
                raise from_response(r)
            wait = retry_after(r)
            time.sleep(min(MAX_RETRY_DELAY, 0.5 * 2**attempt if wait is None else wait))
            attempt += 1

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        files: Any = None,
    ) -> Any:
        """Raw JSON call for routes without a helper. Returns None for 204."""
        r = self._send(method, path, params=_clean(params or {}), json=json_body, files=files)
        return None if r.status_code == 204 or not r.content else r.json()

    @contextmanager
    def stream(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: Any = None
    ) -> Iterator[Iterator[sse.ServerEvent]]:
        """Open an SSE route and yield its parsed frames."""
        r = self._send(method, path, stream=True, params=_clean(params or {}), json=json_body)
        try:
            yield sse.parse(r.iter_lines())
        finally:
            r.close()


class _Resource:
    def __init__(self, client: Client) -> None:
        self._c = client


class KnowledgeBases(_Resource):
    def list(self, cursor: str | None = None, limit: int | None = None) -> KBPage:
        return self._c.request("GET", "/v1/kbs", params={"cursor": cursor, "limit": limit})

    def create(self, name: str, settings: dict[str, Any] | None = None) -> KB:
        return self._c.request(
            "POST", "/v1/kbs", json_body=_clean({"name": name, "settings": settings})
        )

    def get(self, kb_id: str) -> KB:
        return self._c.request("GET", f"/v1/kbs/{kb_id}")

    def delete(self, kb_id: str) -> None:
        self._c.request("DELETE", f"/v1/kbs/{kb_id}")


class Documents(_Resource):
    def upload(
        self, kb_id: str, file: FileInput, filename: str | None = None, mime: str | None = None
    ) -> Document:
        """Upload a path, bytes or binary file object. Bytes need `filename`. Returns the queued
        document (or the existing one when the same bytes were uploaded before)."""
        if isinstance(file, (str, os.PathLike)):
            path = Path(file)
            with path.open("rb") as fh:
                return self.upload(kb_id, fh, filename or path.name, mime)
        if filename is None:
            filename = Path(getattr(file, "name", "")).name
        if not filename:
            raise ValueError("filename is required when uploading bytes or a nameless stream")
        part = (filename, file, mime) if mime else (filename, file)
        return self._c.request("POST", f"/v1/kbs/{kb_id}/documents", files={"file": part})

    def list(self, kb_id: str, cursor: str | None = None, limit: int | None = None) -> DocumentPage:
        params = {"cursor": cursor, "limit": limit}
        return self._c.request("GET", f"/v1/kbs/{kb_id}/documents", params=params)

    def get(self, doc_id: str) -> Document:
        return self._c.request("GET", f"/v1/documents/{doc_id}")

    def chunks(self, doc_id: str, cursor: str | None = None, limit: int | None = None) -> ChunkPage:
        params = {"cursor": cursor, "limit": limit}
        return self._c.request("GET", f"/v1/documents/{doc_id}/chunks", params=params)

    def reprocess(self, doc_id: str) -> Document:
        return self._c.request("POST", f"/v1/documents/{doc_id}/reprocess")

    def delete(self, doc_id: str) -> None:
        self._c.request("DELETE", f"/v1/documents/{doc_id}")

    def wait_until_ready(self, doc_id: str, timeout: float = 300.0, poll: float = 1.0) -> Document:
        """Poll until `ready`. Raises RagError(`document_failed`) or TimeoutError."""
        deadline = time.monotonic() + timeout
        while True:
            doc = self.get(doc_id)
            if doc["status"] == "ready":
                return doc
            if doc["status"] in ("failed", "deleted"):
                raise RagError("document_failed", doc.get("error") or doc["status"])
            if time.monotonic() >= deadline:
                raise TimeoutError(f"document {doc_id} still {doc['status']} after {timeout}s")
            time.sleep(poll)


class Answers(_Resource):
    def stream(
        self,
        question: str,
        kb_ids: list[str] | None = None,
        conversation_id: str | None = None,
        k: int | None = None,
    ) -> Iterator[sse.AnswerEvent]:
        """Typed events in order: Meta, then Citation/Token interleaved, then Done. A StreamError
        can end the stream at any point. POST is never retried."""
        body = _clean(
            {"question": question, "kb_ids": kb_ids, "conversation_id": conversation_id, "k": k}
        )
        with self._c.stream("POST", "/v1/answer", json_body=body) as events:
            for e in events:
                typed = sse.answer_event(e)
                if typed is not None:
                    yield typed

    def ask(
        self,
        question: str,
        kb_ids: list[str] | None = None,
        conversation_id: str | None = None,
        k: int | None = None,
    ) -> sse.Answer:
        """Whole answer at once. Raises RagError on an `error` event."""
        out = sse.Answer(text="")
        for e in self.stream(question, kb_ids, conversation_id, k):
            if isinstance(e, sse.Meta):
                out.conversation_id, out.message_id = e.conversation_id, e.message_id
            elif isinstance(e, sse.Token):
                out.text += e.text
            elif isinstance(e, sse.Citation):
                out.citations.append(e)
            elif isinstance(e, sse.Done):
                out.abstained, out.usage = e.abstained, e.usage
            else:
                raise RagError(e.code, e.message)
        return out


class Conversations(_Resource):
    def list(self, cursor: str | None = None, limit: int | None = None) -> ConversationPage:
        params = {"cursor": cursor, "limit": limit}
        return self._c.request("GET", "/v1/conversations", params=params)

    def get(self, conversation_id: str) -> dict[str, Any]:
        return self._c.request("GET", f"/v1/conversations/{conversation_id}")


class Agent(_Resource):
    def create(
        self, goal: str, kb_ids: list[str] | None = None, max_steps: int | None = None
    ) -> Run:
        body = _clean({"goal": goal, "kb_ids": kb_ids, "max_steps": max_steps})
        return self._c.request("POST", "/v1/agent/runs", json_body=body)

    def list(self, limit: int | None = None) -> list[Run]:
        return self._c.request("GET", "/v1/agent/runs", params={"limit": limit})["items"]

    def get(self, run_id: str) -> RunDetail:
        return self._c.request("GET", f"/v1/agent/runs/{run_id}")

    def cancel(self, run_id: str) -> Run:
        return self._c.request("POST", f"/v1/agent/runs/{run_id}/cancel")

    def events(
        self, run_id: str, after: int = 0, max_reconnects: int = 5
    ) -> Iterator[sse.ServerEvent]:
        """`step`, `step_update`, `approval`, `status`, `timeout` and finally `end` events.

        Reconnects with `?after=<last step n>` on a server `timeout` event (unlimited) and on a
        dropped connection (up to `max_reconnects` in a row). Replays after a reconnect are
        duplicates by design: apply `step_update` by `n` and `approval` by `id`.
        """
        drops = 0
        while True:
            try:
                with self._c.stream(
                    "GET", f"/v1/agent/runs/{run_id}/events", params={"after": after}
                ) as events:
                    for e in events:
                        if e.event == "step":
                            after, drops = max(after, int(e.data["n"])), 0
                        yield e
                        if e.event == "end":
                            return
                        if e.event == "timeout":
                            break
                    else:
                        drops += 1  # closed without `end`
            except (httpx.TransportError, RagError) as exc:
                if isinstance(exc, RagError) and exc.status not in (None, 502, 503, 504):
                    raise
                drops += 1
            if drops > max_reconnects:
                raise RagError("stream_lost", f"agent events for {run_id} dropped {drops} times")


class Approvals(_Resource):
    def decide(self, approval_id: str, decision: Literal["approve", "reject"]) -> Approval:
        return self._c.request(
            "POST", f"/v1/approvals/{approval_id}", json_body={"decision": decision}
        )


class Reports(_Resource):
    def download(self, report_id: str) -> bytes:
        """The XLSX bytes of an agent report."""
        return self._c._send("GET", f"/v1/reports/{report_id}").content


class Sources(_Resource):
    def connectors(self) -> list[dict[str, Any]]:
        """Enabled connector types with their JSON Schema config."""
        return self._c.request("GET", "/v1/connectors")["items"]

    def suggest_rest_config(self, spec: str) -> dict[str, Any]:
        """OpenAPI/Swagger text -> `{config, skipped}`: a starting `rest` source config."""
        return self._c.request("POST", "/v1/connectors/rest/openapi", json_body={"spec": spec})

    def create(
        self,
        kb_id: str,
        type: str,
        name: str,
        config: dict[str, Any],
        secret: str | dict[str, Any] | None = None,
        schedule_minutes: int | None = None,
    ) -> Source:
        body = _clean(
            {
                "type": type,
                "name": name,
                "config": config,
                "secret": secret,
                "schedule_minutes": schedule_minutes,
            }
        )
        return self._c.request("POST", f"/v1/kbs/{kb_id}/sources", json_body=body)

    def list(self, kb_id: str) -> list[Source]:
        return self._c.request("GET", f"/v1/kbs/{kb_id}/sources")["items"]

    def get(self, source_id: str) -> Source:
        return self._c.request("GET", f"/v1/sources/{source_id}")

    def update(self, source_id: str, **fields: Any) -> Source:
        """PATCH name/config/secret/schedule_minutes/enabled."""
        return self._c.request("PATCH", f"/v1/sources/{source_id}", json_body=fields)

    def delete(self, source_id: str) -> None:
        self._c.request("DELETE", f"/v1/sources/{source_id}")

    def test(self, source_id: str) -> SourceTest:
        return self._c.request("POST", f"/v1/sources/{source_id}/test")

    def sync(self, source_id: str, full: bool = False) -> SourceRun:
        params = {"full": "true"} if full else None
        return self._c.request("POST", f"/v1/sources/{source_id}/sync", params=params)

    def runs(self, source_id: str) -> builtins.list[SourceRun]:
        return self._c.request("GET", f"/v1/sources/{source_id}/runs")["items"]


class ApiKeys(_Resource):
    def create(
        self, name: str, scopes: list[Scope], expires_at: str | None = None
    ) -> ApiKeyCreated:
        """`key` in the result is shown once; store it as a secret."""
        body = _clean({"name": name, "scopes": scopes, "expires_at": expires_at})
        return self._c.request("POST", "/v1/api-keys", json_body=body)

    def list(self) -> list[ApiKey]:
        return self._c.request("GET", "/v1/api-keys")

    def revoke(self, key_id: str) -> None:
        self._c.request("DELETE", f"/v1/api-keys/{key_id}")
