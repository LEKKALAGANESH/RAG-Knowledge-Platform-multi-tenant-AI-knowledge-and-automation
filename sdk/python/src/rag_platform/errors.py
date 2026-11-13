"""API errors. Every non-2xx response becomes a RagError (or a subclass picked by status)."""

from __future__ import annotations

import httpx


class RagError(Exception):
    """`{"error": {"code", "message", "request_id"}}` from the API, plus the HTTP status."""

    def __init__(
        self, code: str, message: str, request_id: str | None = None, status: int | None = None
    ) -> None:
        super().__init__(f"{code}: {message}" + (f" (request {request_id})" if request_id else ""))
        self.code, self.message, self.request_id, self.status = code, message, request_id, status


class AuthenticationError(RagError):
    """401: missing, expired or invalid token / API key."""


class BudgetExceededError(RagError):
    """402 budget_exceeded: the org's monthly budget is spent (block mode)."""


class PermissionDeniedError(RagError):
    """403: role too low (`forbidden`) or the API key lacks a scope (`insufficient_scope`)."""


class NotFoundError(RagError):
    """404: also returned for ids that belong to another org."""


class ConflictError(RagError):
    """409: e.g. sync_running, already_decided, run_finished, email_taken."""


class ValidationError(RagError):
    """422: request body or query failed validation."""


class RateLimitError(RagError):
    """429: `retry_after` is the server's Retry-After in seconds (None if absent)."""

    def __init__(
        self,
        code: str,
        message: str,
        request_id: str | None = None,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(code, message, request_id, status)
        self.retry_after = retry_after


_BY_STATUS: dict[int, type[RagError]] = {
    401: AuthenticationError,
    402: BudgetExceededError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
}


def retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After header (the API only sends seconds)."""
    try:
        return max(0.0, float(r.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def from_response(r: httpx.Response) -> RagError:
    """Build the error for a non-2xx response whose body has been read."""
    rid = r.headers.get("x-request-id")
    code, message = f"http_{r.status_code}", r.reason_phrase or "request failed"
    try:
        err = r.json().get("error")
    except ValueError:
        err = None
    if isinstance(err, dict):
        code = str(err.get("code") or code)
        message = str(err.get("message") or message)
        rid = err.get("request_id") or rid
    if r.status_code == 429:
        return RateLimitError(code, message, rid, 429, retry_after(r))
    return _BY_STATUS.get(r.status_code, RagError)(code, message, rid, r.status_code)
