import logging
import re
import time
import uuid
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
log = logging.getLogger("app.http")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def not_found(what: str = "Resource") -> AppError:
    return AppError(404, "not_found", f"{what} not found.")


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}
    return JSONResponse(body, status_code=status)


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return _error(exc.status, exc.code, exc.message)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return _error(exc.status_code, f"http_{exc.status_code}", str(exc.detail))


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = ", ".join(".".join(str(p) for p in e["loc"][1:]) or "body" for e in exc.errors())
    return _error(422, "validation_error", f"Invalid request fields: {fields}.")


async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled error", exc_info=exc)
    return _error(500, "internal_error", "Something went wrong. Retry, or contact support.")


class RequestIdMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so the contextvar is visible to the 500 handler."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        request_id_var.set(rid)
        start, status = time.perf_counter(), 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # Path only: query strings can carry tokens.
            ms = round((time.perf_counter() - start) * 1000, 1)
            log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "ms": ms,
                },
            )


def install(app: FastAPI) -> None:
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
