import json
import logging
import re
from datetime import UTC, datetime

from app.core.errors import request_id_var

_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
# key=value / "key": "value" pairs whose value must never reach a log line.
_SENSITIVE_PAIR = re.compile(
    r"""(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|access_token|refresh_token)"""
    r"""(["']?\s*[:=]\s*["']?)(bearer\s+)?[^\s"',}&]+"""
)


class JsonFormatter(logging.Formatter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if len(s) >= 8]

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return _SENSITIVE_PAIR.sub(lambda m: f"{m[1]}{m[2]}[REDACTED]", text)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": "rag-api",
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _STD_ATTRS})
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return self._scrub(json.dumps(payload, default=str))


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Our middleware logs requests without query strings; uvicorn's access log would include them.
    logging.getLogger("uvicorn.access").disabled = True
