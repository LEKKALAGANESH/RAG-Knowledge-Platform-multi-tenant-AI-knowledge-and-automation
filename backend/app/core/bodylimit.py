"""Request body cap enforced before routing, auth and form parsing (S3).

Starlette spools multipart file parts to disk without a limit, and FastAPI parses the form before
running dependencies, so an unauthenticated client could otherwise fill the temp disk."""

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import request_id_var

_UPLOAD = re.compile(r"^/v1/kbs/[^/]+/documents/?$")


def _too_large(limit: int) -> JSONResponse:
    message = f"The request body must be at most {max(limit // 2**20, 1)} MB."
    rid = request_id_var.get()
    body = {"error": {"code": "body_too_large", "message": message, "request_id": rid}}
    return JSONResponse(body, status_code=413, headers={"Connection": "close"})


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, default_bytes: int, upload_bytes: int) -> None:
        self.app, self.default_bytes, self.upload_bytes = app, default_bytes, upload_bytes

    def _limit(self, scope: Scope) -> int:
        upload = scope["method"] == "POST" and _UPLOAD.match(scope["path"])
        return self.upload_bytes if upload else self.default_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            await _too_large(limit)(scope, receive, send)
            return
        seen, started, refused = 0, False, False

        async def guarded_receive() -> Message:
            nonlocal seen, refused
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:  # chunked body: answer now, tell the app the client went away
                    if not started:
                        await _too_large(limit)(scope, receive, send)
                    refused = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if refused:
                return  # the app's own error response for the cut-off body is dropped
            started = started or message["type"] == "http.response.start"
            await send(message)

        await self.app(scope, guarded_receive, guarded_send)
