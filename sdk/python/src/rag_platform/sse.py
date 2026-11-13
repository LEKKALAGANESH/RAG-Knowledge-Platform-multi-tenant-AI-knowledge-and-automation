"""Server-sent events: line parser plus the typed `/v1/answer` events."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServerEvent:
    """One SSE frame: `event:` name and the JSON-decoded `data:` payload."""

    event: str
    data: Any


def parse(lines: Iterable[str]) -> Iterator[ServerEvent]:
    """Frames from already-split lines. Multi-line `data:` is joined with \\n; comments and
    unknown fields are ignored; a frame cut off before its blank line is dropped (the stream ended
    mid-frame, so the payload may be incomplete)."""
    name, data = "message", list[str]()
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield ServerEvent(name, json.loads("\n".join(data)))
            name, data = "message", []
            continue
        key, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "event":
            name = value
        elif key == "data":
            data.append(value)


@dataclass(frozen=True)
class Meta:
    conversation_id: str
    message_id: str


@dataclass(frozen=True)
class Token:
    text: str


@dataclass(frozen=True)
class Citation:
    n: int
    chunk_id: str
    document_id: str | None
    document_name: str | None
    kb_id: str | None
    loc: dict[str, Any] | None
    excerpt: str


@dataclass(frozen=True)
class Done:
    usage: dict[str, Any] | None
    abstained: bool
    budget_warning: bool = False


@dataclass(frozen=True)
class StreamError:
    """An `error` event: the server ended the stream (e.g. llm_timeout, budget_exceeded)."""

    code: str
    message: str


AnswerEvent = Meta | Token | Citation | Done | StreamError


def answer_event(e: ServerEvent) -> AnswerEvent | None:
    """Typed answer event, or None for an event this SDK version does not know."""
    d: dict[str, Any] = e.data if isinstance(e.data, dict) else {}
    if e.event == "meta":
        return Meta(d["conversation_id"], d["message_id"])
    if e.event == "token":
        return Token(d["text"])
    if e.event == "citation":
        return Citation(
            d["n"],
            d["chunk_id"],
            d.get("document_id"),
            d.get("document_name"),
            d.get("kb_id"),
            d.get("loc"),
            d.get("excerpt", ""),
        )
    if e.event == "done":
        return Done(d.get("usage"), bool(d.get("abstained")), bool(d.get("budget_warning")))
    if e.event == "error":
        return StreamError(d.get("code", "internal_error"), d.get("message", ""))
    return None


@dataclass
class Answer:
    """What `answer.ask()` returns: the full text, its citations and the done payload."""

    text: str
    citations: list[Citation] = field(default_factory=list)
    conversation_id: str | None = None
    message_id: str | None = None
    abstained: bool = False
    usage: dict[str, Any] | None = None
