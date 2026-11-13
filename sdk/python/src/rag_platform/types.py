"""Response shapes from docs/API_DOCUMENTATION.md. Plain dicts at runtime (TypedDict), so new
server fields pass through untouched."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Scope = Literal[
    "kb:read",
    "kb:write",
    "documents:write",
    "search",
    "answer",
    "agent:run",
    "sources:read",
    "sources:write",
    "admin",
]


class TokenPair(TypedDict):
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


class KB(TypedDict):
    id: str
    name: str
    settings: dict[str, Any]
    embedding_index_id: str | None
    created_at: str


class Document(TypedDict):
    id: str
    kb_id: str
    source_id: str | None
    name: str
    mime: str
    sha256: str
    version: int
    status: Literal["queued", "processing", "ready", "failed", "deleted"]
    error: str | None
    warnings: list[Any]
    meta: dict[str, Any]
    created_at: str


class Chunk(TypedDict):
    id: str
    ord: int
    text: str
    loc: dict[str, Any] | None


class KBPage(TypedDict):
    items: list[KB]
    next_cursor: str | None


class DocumentPage(TypedDict):
    items: list[Document]
    next_cursor: str | None


class ChunkPage(TypedDict):
    items: list[Chunk]
    next_cursor: str | None


class SearchResult(TypedDict):
    chunk_id: str
    document_id: str
    document_name: str
    kb_id: str
    text: str
    loc: dict[str, Any] | None
    score: float


class Conversation(TypedDict):
    id: str
    title: str | None
    kb_ids: list[str]
    created_at: str


class ConversationPage(TypedDict):
    items: list[Conversation]
    next_cursor: str | None


class Run(TypedDict):
    id: str
    goal: str
    kb_ids: list[str]
    status: Literal["planning", "running", "awaiting_approval", "succeeded", "failed", "cancelled"]
    max_steps: int
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    finished_at: str | None


class Approval(TypedDict):
    id: str
    run_id: str
    step_id: str
    tool: str
    input: dict[str, Any]
    status: Literal["pending", "approved", "rejected"]
    decided_by: str | None
    decided_at: str | None
    created_at: str


class RunDetail(Run):
    steps: list[dict[str, Any]]
    approvals: list[Approval]


class Source(TypedDict):
    id: str
    kb_id: str
    type: str
    name: str
    config: dict[str, Any]
    has_secret: bool
    status: str
    last_sync_at: str | None
    schedule_minutes: int | None
    created_at: str
    last_error: str | None
    needs_reconnect: bool


class SourceRun(TypedDict):
    id: str
    source_id: str
    status: Literal["running", "succeeded", "failed", "partial"]
    started_at: str
    finished_at: str | None
    stats: dict[str, Any]
    error: str | None


class SourceTest(TypedDict):
    ok: bool
    message: str
    schema: dict[str, Any] | None


class ApiKey(TypedDict):
    id: str
    name: str
    prefix: str
    scopes: list[Scope]
    user_id: str
    created_at: str
    expires_at: str | None
    last_used_at: str | None
    revoked_at: str | None


class ApiKeyCreated(ApiKey):
    key: str
