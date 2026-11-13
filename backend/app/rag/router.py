import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from sqlalchemy import select, tuple_

from app.core.errors import AppError, not_found
from app.core.ratelimit import by_principal, held, principal, stream_slot
from app.core.tenancy import Tenant, get_tenant, need
from app.llm.providers import LLM, get_llm
from app.rag.answer import SSE, AnswerJob, run_answer
from app.rag.models import Conversation, Message
from app.retrieval.search import visible_kbs
from app.usage.budget import within_budget

router = APIRouter(tags=["answer"])


class AnswerIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kb_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=50)
    conversation_id: uuid.UUID | None = None
    k: int = Field(default=8, ge=1, le=30)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    title: str
    kb_ids: list[uuid.UUID]
    created_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: str
    content: str
    citations: list[JsonValue]
    usage: dict[str, JsonValue] | None
    created_at: datetime
    # From usage["abstained"]; None for user messages and assistant rows stored before it existed.
    abstained: bool | None = None

    @model_validator(mode="after")
    def _abstained(self) -> "MessageOut":
        flag = (self.usage or {}).get("abstained")
        self.abstained = flag if isinstance(flag, bool) else None
        return self


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationList(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None = None


async def _sse(events: AsyncIterator[SSE]) -> AsyncIterator[str]:
    async for name, data in events:
        yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _conversation(t: Tenant, body: AnswerIn) -> Conversation:
    if body.conversation_id is None:
        conv = Conversation(
            org_id=t.org_id, user_id=t.user_id, kb_ids=body.kb_ids or [], title=body.question[:80]
        )
        t.session.add(conv)
        return conv
    found = await t.session.get(Conversation, body.conversation_id)  # RLS: own conversations only
    if found is None:
        raise not_found("Conversation")
    return found


@router.post(
    "/answer",
    response_class=StreamingResponse,
    dependencies=[need("answer"), by_principal("answer"), Depends(within_budget)],
)
async def answer(
    body: AnswerIn, t: Tenant = Depends(get_tenant), llm: LLM = Depends(get_llm)
) -> StreamingResponse:
    """Server-sent events: meta, token*, citation*, done | error. See ARCHITECTURE.md."""
    release = await stream_slot(principal(t))  # before any write: a 429 stores nothing
    try:
        job = await _start(t, body)
    except BaseException:
        await release()
        raise
    return StreamingResponse(
        held(release, _sse(run_answer(job, llm))),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _start(t: Tenant, body: AnswerIn) -> AnswerJob:
    conv = await _conversation(t, body)
    kb_ids = await visible_kbs(t.session, body.kb_ids or conv.kb_ids or None)
    await t.session.flush()  # assigns conv.id and inserts it before the message referencing it
    user_msg = Message(org_id=t.org_id, conversation_id=conv.id, role="user", content=body.question)
    t.session.add(user_msg)
    await t.session.commit()
    return AnswerJob(
        org_id=t.org_id,
        user_id=t.user_id,
        conversation_id=conv.id,
        user_message_id=user_msg.id,
        message_id=uuid.uuid4(),
        question=body.question,
        kb_ids=kb_ids,
        k=body.k,
    )


@router.get("/conversations", dependencies=[need("answer")])
async def list_conversations(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    t: Tenant = Depends(get_tenant),
) -> ConversationList:
    """Keyset pagination, newest first; cursor = base64("<created_at iso>|<id>") as in kb/router."""
    stmt = select(Conversation)
    if cursor:
        try:
            at, _, raw_id = base64.urlsafe_b64decode(cursor).decode().partition("|")
            key = (datetime.fromisoformat(at), uuid.UUID(raw_id))
        except ValueError as exc:
            raise AppError(422, "invalid_cursor", "The pagination cursor is invalid.") from exc
        stmt = stmt.where(tuple_(Conversation.created_at, Conversation.id) < key)
    stmt = stmt.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(limit + 1)
    rows = list((await t.session.scalars(stmt)).all())
    nxt = None
    if len(rows) > limit:
        rows, last = rows[:limit], rows[limit - 1]
        nxt = base64.urlsafe_b64encode(f"{last.created_at.isoformat()}|{last.id}".encode()).decode()
    return ConversationList(
        items=[ConversationOut.model_validate(c) for c in rows], next_cursor=nxt
    )


@router.get("/conversations/{conversation_id}", dependencies=[need("answer")])
async def get_conversation(
    conversation_id: uuid.UUID, t: Tenant = Depends(get_tenant)
) -> ConversationDetail:
    conv = await t.session.get(Conversation, conversation_id)
    if conv is None:
        raise not_found("Conversation")
    msgs = await t.session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
    )
    return ConversationDetail(
        **ConversationOut.model_validate(conv).model_dump(),
        messages=[MessageOut.model_validate(m) for m in msgs],
    )
