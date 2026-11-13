import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tasks
from app.agent.loop import DEFAULT_MAX_STEPS, HARD_MAX_STEPS
from app.agent.models import ACTIVE, AgentRun, AgentStep, Approval, Report
from app.audit.service import emit
from app.core.db import SessionLocal, use_tenant
from app.core.errors import AppError, not_found
from app.core.ratelimit import by_principal, held, principal, stream_slot
from app.core.tenancy import Tenant, get_tenant, need
from app.ingestion.storage import get_blob_store
from app.retrieval.search import visible_kbs
from app.usage.budget import within_budget

router = APIRouter(tags=["agent"])
POLL_S, STREAM_MAX_S = 1.0, 900.0


class RunIn(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    kb_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=50)
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=HARD_MAX_STEPS)


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    goal: str
    kb_ids: list[uuid.UUID]
    status: str
    max_steps: int
    result: dict[str, JsonValue] | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    n: int
    kind: str
    tool: str | None
    input: dict[str, JsonValue] | None
    output: dict[str, JsonValue] | None
    risk: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    created_at: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    tool: str
    input: dict[str, JsonValue]
    status: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime


class RunDetail(RunOut):
    steps: list[StepOut]
    approvals: list[ApprovalOut]


class RunList(BaseModel):
    items: list[RunOut]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]


async def _run(t: Tenant, run_id: uuid.UUID) -> AgentRun:
    run = await t.session.get(AgentRun, run_id)  # RLS: own runs only
    if run is None:
        raise not_found("Agent run")
    return run


@router.post(
    "/agent/runs",
    status_code=202,
    dependencies=[need("agent:run"), by_principal("answer"), Depends(within_budget)],
)
async def create_run(body: RunIn, t: Tenant = Depends(get_tenant)) -> RunOut:
    """Starts a run in the worker. Follow it with GET .../events or GET .../{id}."""
    kb_ids = await visible_kbs(t.session, body.kb_ids)
    run = AgentRun(
        id=uuid.uuid4(),
        org_id=t.org_id,
        user_id=t.user_id,
        goal=body.goal,
        kb_ids=kb_ids,
        status="planning",
        max_steps=body.max_steps,
    )
    t.session.add(run)
    await emit(t, "agent_run.start", run, {"kb_ids": [str(k) for k in kb_ids]})
    await t.session.commit()
    tasks.run_agent.send(str(run.id), str(t.org_id), str(t.user_id))
    return RunOut.model_validate(await _run(t, run.id))


@router.get("/agent/runs", dependencies=[need("agent:run")])
async def list_runs(
    limit: int = Query(default=50, ge=1, le=200), t: Tenant = Depends(get_tenant)
) -> RunList:
    rows = await t.session.scalars(
        select(AgentRun).order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit)
    )
    return RunList(items=[RunOut.model_validate(r) for r in rows])


@router.get("/agent/runs/{run_id}", dependencies=[need("agent:run")])
async def get_run(run_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> RunDetail:
    run = await _run(t, run_id)
    steps = await t.session.scalars(
        select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.n)
    )
    approvals = await t.session.scalars(
        select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)
    )
    return RunDetail(
        **RunOut.model_validate(run).model_dump(),
        steps=[StepOut.model_validate(s) for s in steps],
        approvals=[ApprovalOut.model_validate(a) for a in approvals],
    )


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _changes(
    s: AsyncSession,
    run_id: uuid.UUID,
    after: int,
    sent: dict[int, tuple[object, ...]],
    decided: dict[uuid.UUID, str],
) -> tuple[list[str], int]:
    """New steps, changed tool_call steps and new/decided approvals since the last poll."""
    out = []
    steps = await s.scalars(
        select(AgentStep)
        .where(AgentStep.run_id == run_id, or_(AgentStep.n > after, AgentStep.kind == "tool_call"))
        .order_by(AgentStep.n)
        .execution_options(populate_existing=True)  # rows loaded by earlier polls are stale
    )
    for step in steps:
        state = (step.started_at, step.finished_at, step.error)
        if sent.get(step.n) != state:
            event = "step" if step.n > after else "step_update"
            after, sent[step.n] = max(after, step.n), state
            out.append(_sse(event, StepOut.model_validate(step).model_dump(mode="json")))
    approvals = await s.scalars(
        select(Approval)
        .where(Approval.run_id == run_id)
        .order_by(Approval.created_at)
        .execution_options(populate_existing=True)
    )
    for a in approvals:
        if decided.get(a.id) != a.status:
            decided[a.id] = a.status
            out.append(_sse("approval", ApprovalOut.model_validate(a).model_dump(mode="json")))
    return out, after


async def _events(
    org: uuid.UUID, user: uuid.UUID, run_id: uuid.UUID, after: int
) -> AsyncIterator[str]:
    """Polls the DB: `step` per new step, `step_update` when a step already sent changes
    (tool_call started/finished/error; same StepOut, replace by n), `approval` when one is
    created or decided, `status` on change, `end` once the run is over. On (re)connect, tool_call
    steps with n <= after and all approvals are re-sent once with their current state, so
    `?after=` never loses an update.
    ponytail: 1 s polling; switch to LISTEN/NOTIFY if many viewers watch runs at once."""
    status, waited = None, 0.0
    sent: dict[int, tuple[object, ...]] = {}
    decided: dict[uuid.UUID, str] = {}
    while True:
        # S8: a short session per poll; no pooled connection is held while the client reads or
        # while this stream sleeps.
        async with SessionLocal() as s:
            await use_tenant(s, org, user)
            run = await s.get(AgentRun, run_id)
            if run is None:
                return
            frames, after = await _changes(s, run_id, after, sent, decided)
            if run.status != status:
                status = run.status
                frames.append(_sse("status", RunOut.model_validate(run).model_dump(mode="json")))
        for frame in frames:
            yield frame
        if status not in ACTIVE:
            yield _sse("end", {"status": status})
            return
        if waited >= STREAM_MAX_S:
            yield _sse("timeout", {"after": after})  # reconnect with ?after=
            return
        await asyncio.sleep(POLL_S)
        waited += POLL_S


@router.get(
    "/agent/runs/{run_id}/events",
    response_class=StreamingResponse,
    dependencies=[need("agent:run")],
)
async def run_events(
    run_id: uuid.UUID, after: int = Query(default=0, ge=0), t: Tenant = Depends(get_tenant)
) -> StreamingResponse:
    await _run(t, run_id)
    release = await stream_slot(principal(t))
    return StreamingResponse(
        held(release, _events(t.org_id, t.user_id, run_id, after)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/agent/runs/{run_id}/cancel", dependencies=[need("agent:run")])
async def cancel_run(run_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> RunOut:
    await _run(t, run_id)
    done = await t.session.execute(
        update(AgentRun)
        .where(AgentRun.id == run_id, AgentRun.status.in_(ACTIVE))
        .values(status="cancelled", finished_at=func.now(), error="Cancelled by the user.")
        .returning(AgentRun.id)
    )
    if done.scalar_one_or_none() is None:
        raise AppError(409, "run_finished", "This run has already finished.")
    await t.session.commit()
    return RunOut.model_validate(await t.session.get(AgentRun, run_id, populate_existing=True))


@router.post("/approvals/{approval_id}", dependencies=[need("agent:run")])
async def decide(
    approval_id: uuid.UUID, body: DecisionIn, t: Tenant = Depends(get_tenant)
) -> ApprovalOut:
    """A person approves or rejects a paused tool call: the run's owner or an org owner/admin.
    The pending -> decided update is atomic, so a double click resumes the run once."""
    approval = await t.session.get(Approval, approval_id)  # RLS: own org only
    if approval is None or (approval.user_id != t.user_id and t.role not in ("owner", "admin")):
        raise not_found("Approval")
    status = "approved" if body.decision == "approve" else "rejected"
    done = await t.session.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == "pending")
        .values(status=status, decided_by=t.user_id, decided_at=func.now())
        .returning(Approval.id)
    )
    if done.scalar_one_or_none() is None:
        raise AppError(409, "already_decided", "This action was already approved or rejected.")
    await emit(t, f"approval.{status}", approval, {"run_id": str(approval.run_id)})
    await t.session.commit()
    tasks.run_agent.send(str(approval.run_id), str(t.org_id), str(approval.user_id))
    return ApprovalOut.model_validate(
        await t.session.get(Approval, approval_id, populate_existing=True)
    )


@router.get("/reports/{report_id}", response_class=Response, dependencies=[need("agent:run")])
async def download_report(report_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> Response:
    report = await t.session.get(Report, report_id)  # RLS: reports of own runs only
    if report is None:
        raise not_found("Report")
    data = await asyncio.to_thread(get_blob_store().get, report.blob_key)
    return Response(
        data,
        media_type=report.mime,
        headers={"Content-Disposition": f'attachment; filename="{report.name}"'},
    )
