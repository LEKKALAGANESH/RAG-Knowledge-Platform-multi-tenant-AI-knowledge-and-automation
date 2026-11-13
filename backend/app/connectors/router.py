import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import emit
from app.connectors import oauth, openapi
from app.connectors.base import Config, Connector, ConnectorError, ConnectorMeta
from app.connectors.models import RunStatus, Source, SourceStatus, SyncRun
from app.connectors.registry import REGISTRY
from app.connectors.ssrf import PRIVATE_DISABLED
from app.connectors.sync import drop_blob_if_orphan, reap_stale_runs, sync_source
from app.core.config import get_settings
from app.core.crypto import decrypt, encrypt
from app.core.errors import AppError, not_found
from app.core.tenancy import EDITORS, Tenant, get_tenant, need, require_role
from app.ingestion.storage import get_blob_store
from app.kb.models import Document, KnowledgeBase

router = APIRouter(tags=["sources"])


class SourceIn(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any]
    secret: str | None = Field(default=None, max_length=10_000)  # write-only
    schedule_minutes: int | None = Field(default=None, ge=15)


class SourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict[str, Any] | None = None
    secret: str | None = Field(default=None, max_length=10_000)
    schedule_minutes: int | None = Field(default=None, ge=15)
    enabled: bool | None = None


class SourceOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    type: str
    name: str
    config: dict[str, JsonValue]
    has_secret: bool  # the secret itself is never returned
    status: SourceStatus
    last_sync_at: datetime | None
    schedule_minutes: int | None
    created_at: datetime
    last_error: str | None  # why the last run failed or was partial; None after a clean run
    needs_reconnect: bool  # OAuth grant revoked/expired: POST /sources/{id}/oauth/reconnect

    @classmethod
    def of(cls, s: Source) -> "SourceOut":
        return cls(
            id=s.id, kb_id=s.kb_id, type=s.type, name=s.name, config=s.config,
            has_secret=s.secret_enc is not None, status=s.status, last_sync_at=s.last_sync_at,
            schedule_minutes=s.schedule_minutes, created_at=s.created_at, last_error=s.last_error,
            needs_reconnect=s.needs_reconnect,
        )  # fmt: skip


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    source_id: uuid.UUID
    status: RunStatus
    started_at: datetime
    finished_at: datetime | None
    stats: dict[str, JsonValue]
    error: str | None


class TableOut(BaseModel):
    name: str
    columns: list[str]


class SchemaOut(BaseModel):
    tables: list[TableOut]


class TestOut(BaseModel):
    ok: bool
    message: str
    # `schema` would shadow a BaseModel attribute; the JSON key is still "schema".
    schema_: SchemaOut | None = Field(default=None, serialization_alias="schema")


class Items[T](BaseModel):
    items: list[T]


def _connector(type_: str) -> Connector:
    if type_ not in REGISTRY:
        raise AppError(422, "unknown_connector", f"Unknown connector type: {type_[:50]}.")
    return REGISTRY[type_]


def _validated(c: Connector, raw: dict[str, Any]) -> dict[str, Any]:
    try:
        return c.validate_config(raw)
    except ValidationError as exc:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise AppError(422, "invalid_config", f"Invalid configuration: {fields}"[:500]) from exc
    except ConnectorError as exc:
        raise AppError(422, "invalid_config", str(exc)) from exc


def _secret(c: Connector, secret: str | None) -> bytes | None:
    if secret is None:
        return None
    if c.meta.auth == "none":
        raise AppError(422, "secret_not_allowed", "This connector does not take credentials.")
    if c.meta.auth == "oauth2":  # tokens come only from the OAuth callback (they bind source+org)
        raise AppError(422, "oauth_only", "Connect this source with its sign-in flow.")
    return encrypt(secret)


async def _run_test(c: Connector, config: Config, secret: str | None) -> TestOut:
    try:
        result = await c.test(config, secret)
    except ConnectorError as exc:
        return TestOut(ok=False, message=str(exc))
    if isinstance(result, str):
        return TestOut(ok=True, message=result)
    schema = None
    if result.tables is not None:
        tables = [TableOut(name=n, columns=cols) for n, cols in result.tables.items()]
        schema = SchemaOut(tables=tables)
    return TestOut(ok=True, message=result.message, schema_=schema)


async def _check_before_save(c: Connector, config: Config, secret: str | None) -> None:
    """A source is saved only when the operator gate allows its endpoint and test() passes, so
    e.g. a write-capable database role is refused at save time, not at the first sync."""
    gate = get_settings().connectors_allow_private_endpoints
    if config.get("allow_private_endpoint") is True and not gate:
        raise AppError(422, "private_endpoints_disabled", PRIVATE_DISABLED)
    result = await _run_test(c, config, secret)
    if not result.ok:
        raise AppError(422, "source_test_failed", result.message)


def _target_changed(c: Connector, old: Config, new: Config) -> bool:
    """A change of where the stored secret would be sent. Connectors list those fields in
    `target_fields`; one that lists none counts every config change."""
    fields: tuple[str, ...] | None = getattr(c, "target_fields", None)
    if fields is None:
        return old != new
    return any(old.get(f) != new.get(f) for f in fields)


def _plain(secret_enc: bytes | None) -> str | None:
    return decrypt(secret_enc) if secret_enc else None


async def _get_source(t: Tenant, source_id: uuid.UUID) -> Source:
    source = await t.session.get(Source, source_id)  # RLS hides other orgs' rows -> 404
    if source is None:
        raise not_found("Source")
    return source


@router.get("/connectors", dependencies=[need("sources:read")])
async def list_connectors(t: Tenant = Depends(get_tenant)) -> Items[ConnectorMeta]:
    return Items(items=[c.meta for c in REGISTRY.values()])


class OpenApiIn(BaseModel):
    spec: str = Field(min_length=2, max_length=openapi.MAX_SPEC_CHARS)  # JSON or YAML text


@router.post("/connectors/rest/openapi", dependencies=[need("sources:write")])
async def rest_from_openapi(
    body: OpenApiIn, t: Tenant = Depends(require_role(*EDITORS))
) -> dict[str, Any]:
    """Suggested REST source config from an OpenAPI/Swagger spec (GET operations only). The spec
    is parsed as data; the API is never called. Returns {config, skipped}."""
    if "rest" not in REGISTRY:
        raise AppError(404, "not_found", "The REST connector is not enabled.")
    try:
        return await asyncio.to_thread(openapi.suggest, body.spec)  # parsing is CPU-bound
    except openapi.SpecError as exc:
        raise AppError(422, "invalid_spec", str(exc)) from None


@router.post("/kbs/{kb_id}/sources", status_code=201, dependencies=[need("sources:write")])
async def create_source(
    kb_id: uuid.UUID, body: SourceIn, t: Tenant = Depends(require_role(*EDITORS))
) -> SourceOut:
    if await t.session.get(KnowledgeBase, kb_id) is None:
        raise not_found("Knowledge base")
    c = _connector(body.type)
    config, secret_enc = _validated(c, body.config), _secret(c, body.secret)
    await _check_before_save(c, config, body.secret)
    source = Source(
        org_id=t.org_id, kb_id=kb_id, type=body.type, name=body.name,
        config=config, secret_enc=secret_enc, schedule_minutes=body.schedule_minutes, cursor={},
    )  # fmt: skip
    t.session.add(source)
    await emit(t, "source.create", source, {"type": body.type, "kb_id": str(kb_id)})
    await t.session.commit()
    await t.session.refresh(source)
    return SourceOut.of(source)


@router.get("/kbs/{kb_id}/sources", dependencies=[need("sources:read")])
async def list_sources(kb_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> Items[SourceOut]:
    if await t.session.get(KnowledgeBase, kb_id) is None:
        raise not_found("Knowledge base")
    # ponytail: unpaged, a KB has a handful of sources; add a cursor if that stops being true.
    stmt = select(Source).where(Source.kb_id == kb_id).order_by(Source.created_at.desc())
    return Items(items=[SourceOut.of(s) for s in await t.session.scalars(stmt.limit(500))])


@router.get("/sources/{source_id}", dependencies=[need("sources:read")])
async def get_source(source_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> SourceOut:
    return SourceOut.of(await _get_source(t, source_id))


@router.patch("/sources/{source_id}", dependencies=[need("sources:write")])
async def update_source(
    source_id: uuid.UUID, body: SourcePatch, t: Tenant = Depends(require_role(*EDITORS))
) -> SourceOut:
    source = await _get_source(t, source_id)
    c = _connector(source.type)
    if body.name is not None:
        source.name = body.name
    if body.config is not None or body.secret is not None:
        if body.config is not None and source.status == "syncing":  # it would save a stale cursor
            raise AppError(409, "sync_running", "Wait for the running sync to finish, then edit.")
        config = source.config if body.config is None else _validated(c, body.config)
        if (
            body.secret is None
            and source.secret_enc is not None  # nothing stored, nothing to leak (REST, auth none)
            and c.meta.auth not in ("none", "oauth2")
            and _target_changed(c, source.config, config)
        ):  # else test() would send the stored secret to a host the editor just chose
            raise AppError(
                422,
                "secret_required_for_target_change",
                "Re-enter the credentials when changing where this source connects.",
            )
        secret_enc = source.secret_enc if body.secret is None else _secret(c, body.secret)
        secret = body.secret if body.secret is not None else _plain(source.secret_enc)
        await _check_before_save(c, config, secret)
        if body.config is not None:
            source.config, source.cursor = config, {}  # new tables/prefix: no stale watermark
        source.secret_enc = secret_enc
    if "schedule_minutes" in body.model_fields_set:
        source.schedule_minutes = body.schedule_minutes
    if body.enabled is not None and source.status != "syncing":
        source.status = "idle" if body.enabled else "disabled"
    await emit(t, "source.update", source, {"fields": ",".join(sorted(body.model_fields_set))})
    await t.session.commit()
    await t.session.refresh(source)
    return SourceOut.of(source)


@router.delete("/sources/{source_id}", status_code=204, dependencies=[need("sources:write")])
async def delete_source(
    source_id: uuid.UUID, t: Tenant = Depends(require_role(*EDITORS))
) -> Response:
    source = await _get_source(t, source_id)
    if source.status == "syncing":
        raise AppError(409, "sync_running", "Wait for the running sync to finish, then delete.")
    keys = set(
        await t.session.scalars(select(Document.blob_key).where(Document.source_id == source.id))
    )
    provider = getattr(REGISTRY.get(source.type), "oauth", None)
    secret = _plain(source.secret_enc) if provider else None
    await emit(t, "source.delete", source)
    await t.session.delete(source)  # documents -> chunks -> vectors and sync_runs cascade in the DB
    await t.session.commit()
    if provider:
        await oauth.revoke(provider, secret)
    for key in keys:
        await drop_blob_if_orphan(t.session, get_blob_store(), key)
    return Response(status_code=204)


@router.post("/sources/{source_id}/test", dependencies=[need("sources:write")])
async def test_source(source_id: uuid.UUID, t: Tenant = Depends(require_role(*EDITORS))) -> TestOut:
    source = await _get_source(t, source_id)
    return await _run_test(_connector(source.type), source.config, _plain(source.secret_enc))


@router.post("/sources/{source_id}/sync", status_code=202, dependencies=[need("sources:write")])
async def start_sync(
    source_id: uuid.UUID,
    full: bool = Query(default=False, description="Ignore the saved cursor: read everything"),
    t: Tenant = Depends(require_role(*EDITORS)),
) -> RunOut:
    source = await _get_source(t, source_id)
    if source.status == "disabled":
        raise AppError(409, "source_disabled", "Enable the source before syncing it.")
    if full:  # an empty cursor means a full view; committed (or rolled back) with the run row
        source.cursor = {}
    await reap_stale_runs(t.session, source.id)  # a killed worker's run must not block forever
    run = SyncRun(org_id=t.org_id, source_id=source.id, stats={})
    t.session.add(run)
    try:
        await emit(t, "source.sync", source, {"full": full})
        await t.session.commit()
    except IntegrityError as exc:  # sync_runs_one_running
        await t.session.rollback()
        raise AppError(409, "sync_running", "A sync is already running for this source.") from exc
    await t.session.refresh(run)
    sync_source.send(str(run.id), str(t.org_id))
    return RunOut.model_validate(run)


@router.get("/sources/{source_id}/runs", dependencies=[need("sources:read")])
async def list_runs(source_id: uuid.UUID, t: Tenant = Depends(get_tenant)) -> Items[RunOut]:
    await _get_source(t, source_id)
    stmt = select(SyncRun).where(SyncRun.source_id == source_id)
    rows = await t.session.scalars(stmt.order_by(SyncRun.started_at.desc()).limit(50))
    return Items(items=[RunOut.model_validate(r) for r in rows])


# ---------------------------------------------------------------- OAuth connect (A7a)


class OAuthStartIn(BaseModel):
    type: str
    name: str = Field(min_length=1, max_length=200)
    config: dict[str, Any]
    schedule_minutes: int | None = Field(default=None, ge=15)


class OAuthStartOut(BaseModel):
    authorize_url: str


class OAuthFinishIn(BaseModel):
    state: str = Field(max_length=100)
    code: str = Field(default="", max_length=2000)
    error: str = Field(default="", max_length=200)


class OAuthFinishOut(BaseModel):
    redirect: str  # frontend path, e.g. /kbs/{id}?tab=sources&connected={source_id}


# The BFF sends its httpOnly per-browser cookie here; the browser itself cannot set it.
BindingHeader = Header(default="", alias="X-OAuth-Binding")


def _binding(raw: str) -> str:
    low, high = oauth.BINDING_LEN
    if not low <= len(raw) <= high:
        raise AppError(422, "oauth_binding_required", "Start the sign-in from the web app.")
    return raw


@router.post("/kbs/{kb_id}/sources/oauth/start", dependencies=[need("sources:write")])
async def oauth_start(
    kb_id: uuid.UUID,
    body: OAuthStartIn,
    t: Tenant = Depends(require_role(*EDITORS)),
    binding: str = BindingHeader,
) -> OAuthStartOut:
    """Step 1: send the browser to `authorize_url`. The source is created by the finish call."""
    if await t.session.get(KnowledgeBase, kb_id) is None:
        raise not_found("Knowledge base")
    c = _connector(body.type)
    provider = getattr(c, "oauth", None)
    if provider is None:
        raise AppError(422, "not_oauth", "This connector does not use a sign-in flow.")
    pending = {
        "org": str(t.org_id), "user": str(t.user_id), "kb": str(kb_id), "type": body.type,
        "name": body.name, "config": _validated(c, body.config),
        "schedule_minutes": body.schedule_minutes,
    }  # fmt: skip
    return OAuthStartOut(authorize_url=await oauth.start(provider, pending, _binding(binding)))


def _back(path: str, **query: str) -> OAuthFinishOut:
    # Only fixed paths and server-made values: nothing request-controlled goes in the path.
    q = "&".join(f"{k}={v}" for k, v in {"tab": "sources", **query}.items())
    return OAuthFinishOut(redirect=f"{path}?{q}")


@router.post("/sources/{source_id}/oauth/reconnect", dependencies=[need("sources:write")])
async def oauth_reconnect(
    source_id: uuid.UUID,
    t: Tenant = Depends(require_role(*EDITORS)),
    binding: str = BindingHeader,
) -> OAuthStartOut:
    """New sign-in for an existing source: the finish call re-binds tokens onto the same row."""
    source = await _get_source(t, source_id)
    provider = getattr(REGISTRY.get(source.type), "oauth", None)
    if provider is None:
        raise AppError(422, "not_oauth", "This connector does not use a sign-in flow.")
    pending = {
        "org": str(t.org_id), "user": str(t.user_id), "kb": str(source.kb_id),
        "type": source.type, "source": str(source.id),
    }  # fmt: skip
    return OAuthStartOut(authorize_url=await oauth.start(provider, pending, _binding(binding)))


async def _queue_sync(session: AsyncSession, org: uuid.UUID, source_id: uuid.UUID) -> None:
    """Start a sync unless one is running (sync_runs_one_running); the caller has committed."""
    await reap_stale_runs(session, source_id)
    run = SyncRun(org_id=org, source_id=source_id, stats={})
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return
    sync_source.send(str(run.id), str(org))


def _bind(tokens: dict[str, Any], source: Source) -> bytes:
    # source_id/org_id ride inside the ciphertext: TokenSession writes refreshes back to them.
    # `grant` is new per connect/reconnect and survives refreshes: sync.run_sync uses it to tell
    # a run that failed on a grant replaced mid-run from one whose grant is really gone.
    bound = {
        **tokens, "source_id": str(source.id), "org_id": str(source.org_id),
        "grant": uuid.uuid4().hex,
    }  # fmt: skip
    return encrypt(json.dumps(bound))


async def _create(t: Tenant, pending: dict[str, Any], tokens: dict[str, Any]) -> str:
    kb_id = uuid.UUID(pending["kb"])
    if await t.session.get(KnowledgeBase, kb_id) is None:
        return "forbidden"
    source = Source(
        org_id=t.org_id, kb_id=kb_id, type=pending["type"], name=pending["name"],
        config=pending["config"], schedule_minutes=pending["schedule_minutes"], cursor={},
    )  # fmt: skip
    t.session.add(source)
    await t.session.flush()
    source.secret_enc = _bind(tokens, source)
    await emit(t, "source.create", source, {"type": source.type, "kb_id": str(kb_id)})
    await t.session.commit()
    await _queue_sync(t.session, t.org_id, source.id)  # first sync starts by itself
    return f"connected={source.id}"


async def _rebind(t: Tenant, pending: dict[str, Any], tokens: dict[str, Any]) -> str:
    """Reconnect: new tokens on the same source (documents kept). The old grant is not revoked:
    for Google that would revoke the app's whole grant, the new tokens included."""
    source = await t.session.get(Source, uuid.UUID(pending["source"]))  # RLS: own org only
    if source is None or str(source.kb_id) != pending["kb"] or source.type != pending["type"]:
        return "source_gone"
    was_error = source.status == "error"
    source.secret_enc = _bind(tokens, source)
    source.needs_reconnect, source.last_error = False, None
    if source.status == "error":
        source.status = "idle"
    await emit(t, "source.reconnect", source, {"type": source.type})
    await t.session.commit()
    if was_error:  # it stopped on the dead grant; a source that was fine waits for its schedule
        await _queue_sync(t.session, t.org_id, source.id)
    return f"reconnected={source.id}"


@router.post("/connectors/oauth/finish", dependencies=[need("sources:write")])
async def oauth_finish(
    body: OAuthFinishIn,
    t: Tenant = Depends(require_role(*EDITORS)),
    binding: str = BindingHeader,
) -> OAuthFinishOut:
    """Step 2: the frontend finish page posts the provider's code+state for the signed-in user.
    Only the user, org and browser that started the flow may finish it (S2); anything else is
    refused before the code is exchanged, so no grant is created for the wrong account."""
    pending = await oauth.consume_state(body.state)
    if pending is None:
        raise AppError(400, "invalid_state", "That sign-in link expired or was already used.")
    if not oauth.bound(pending, t.user_id, t.org_id, binding):
        raise AppError(
            403, "oauth_not_yours", "This sign-in was started by another account or browser."
        )
    dest = f"/kbs/{uuid.UUID(pending['kb'])}"
    provider = getattr(REGISTRY.get(pending["type"]), "oauth", None)
    if provider is None:
        return _back(dest, oauth_error="unavailable")
    if body.error or not body.code:
        return _back(dest, oauth_error="denied")
    try:
        tokens = await oauth.exchange(provider, body.code, pending["verifier"])
    except ConnectorError:
        return _back(dest, oauth_error="exchange_failed")
    if not oauth.granted(provider, tokens):  # user unticked a data scope / no offline access
        await oauth.revoke(provider, json.dumps(tokens))
        return _back(dest, oauth_error="scope_denied")
    outcome = await (_rebind if "source" in pending else _create)(t, pending, tokens)
    if "=" not in outcome:  # refused: the fresh grant must not outlive the flow
        await oauth.revoke(provider, json.dumps(tokens))
        return _back(dest, oauth_error=outcome)
    key, value = outcome.split("=")
    return _back(dest, **{key: value})
