import asyncio
import uuid

import dramatiq

from app.agent.loop import advance
from app.core.db import worker_sessions
from app.llm.providers import get_tool_provider
from app.worker import broker


# No Dramatiq retries: `advance` ends the run with a readable error itself, and a resume is
# enqueued only by an approval decision. Tool calls are claimed in the DB, so even a duplicate
# message never runs a tool twice.
@dramatiq.actor(broker=broker, queue_name="agent", max_retries=0, time_limit=60 * 60 * 1000)
def run_agent(run_id: str, org_id: str, user_id: str) -> None:
    asyncio.run(
        advance(
            uuid.UUID(run_id),
            uuid.UUID(org_id),
            uuid.UUID(user_id),
            worker_sessions(),
            get_tool_provider,
        )
    )
