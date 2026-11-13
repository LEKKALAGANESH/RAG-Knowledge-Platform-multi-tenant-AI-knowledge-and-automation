"""Dramatiq entry point: `uv run dramatiq app.worker`."""

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings
from app.core.logging import setup_logging

settings = get_settings()
setup_logging(settings.log_level, settings.secret_values())
broker = RedisBroker(url=settings.redis_url)  # type: ignore[no-untyped-call]  # untyped lib
dramatiq.set_broker(broker)

# Every actor module is listed here, after `broker` exists: package __init__s must not import
# tasks, or importing a leaf (e.g. ingestion.pipeline) cycles back into this module.
import app.agent.tasks  # noqa: E402
import app.connectors.sync  # noqa: E402
import app.ingestion.tasks  # noqa: E402
import app.retrieval.tasks  # noqa: E402, F401
