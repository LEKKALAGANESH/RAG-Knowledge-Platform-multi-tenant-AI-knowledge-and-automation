from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.agent.router import router as agent_router
from app.audit.router import router as audit_router
from app.auth.keys import router as api_keys_router
from app.auth.router import router as auth_router
from app.connectors.router import router as sources_router
from app.core import errors
from app.core.bodylimit import BodyLimitMiddleware
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import setup_logging
from app.core.ratelimit import RateLimited, rate_limited_handler
from app.core.security import SecurityHeadersMiddleware
from app.ingestion.config import get_ingestion_settings
from app.kb.router import router as kb_router
from app.orgs.router import invites_router
from app.orgs.router import router as orgs_router
from app.rag.router import router as rag_router
from app.retrieval.router import router as retrieval_router
from app.usage.router import router as usage_router

settings = get_settings()
setup_logging(settings.log_level, settings.secret_values())

app = FastAPI(title="RAG Platform API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID", "Retry-After", "X-Budget-Warning"],
)
app.add_middleware(
    BodyLimitMiddleware,
    default_bytes=settings.max_request_body_kb * 1024,
    upload_bytes=(get_ingestion_settings().max_upload_mb + 1) * 2**20,
)  # inside the request-id middleware, so a 413 is logged and carries the id
errors.install(app)  # outermost of the app's own layers: every response (incl. CORS) gets an id
app.add_middleware(SecurityHeadersMiddleware)
# S11: request.client = the real client only when the TCP peer is a trusted proxy (the BFF); runs
# first so rate-limit buckets, audit and session IP hashes all see it. Under any ASGI server.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.forwarded_allow_ips)
app.add_exception_handler(RateLimited, rate_limited_handler)

v1 = APIRouter(prefix="/v1")
for r in (
    auth_router,
    orgs_router,
    invites_router,
    kb_router,
    retrieval_router,
    rag_router,
    sources_router,
    agent_router,
    api_keys_router,
    audit_router,
    usage_router,
):
    v1.include_router(r)
app.include_router(v1)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, str]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ready"}
