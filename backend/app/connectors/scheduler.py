"""Periodic sync scheduler: `python -m app.connectors.scheduler [--interval SECONDS]`.

Each tick asks the owner-run `due_sync_orgs()` (alembic 0005/0012) which orgs need work, prunes
dead login sessions (`prune_sessions`, 0012), then runs `start_due_syncs` for each org in its own
RLS-scoped session. Safe to run several copies.
"""

import argparse
import asyncio
import logging
import signal
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth import sessions as auth_sessions
from app.connectors import sync
from app.core.db import SessionLocal, use_tenant

log = logging.getLogger("app.connectors.scheduler")


async def tick(
    sessions: async_sessionmaker[AsyncSession] = SessionLocal,
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """One pass over all orgs; returns sources started per org. A failing org skips only itself."""
    async with sessions() as s:
        rows = await s.scalars(text("SELECT due_sync_orgs(:stale)"), {"stale": sync.STALE_AFTER})
        orgs = list(rows)
        try:  # ponytail: a full scan per tick; add an expires_at index if sessions grows large
            await auth_sessions.prune(s)
        except Exception as exc:  # housekeeping must never stall syncs
            await s.rollback()
            log.warning("session prune failed", extra={"error": type(exc).__name__})
    started: dict[uuid.UUID, list[uuid.UUID]] = {}
    for org_id in orgs:
        try:
            async with sessions() as s:
                await use_tenant(s, org_id, None)
                started[org_id] = await sync.start_due_syncs(s, org_id)
        except Exception as exc:  # one broken org must not stall the rest
            log.warning("scheduler tick failed for org",
                        extra={"org_id": str(org_id), "error": type(exc).__name__})  # fmt: skip
    return started


async def run(interval: float) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):  # signal.signal: add_signal_handler is POSIX-only
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    while not stop.is_set():
        try:
            started = await tick()
            n = sum(map(len, started.values()))
            log.info("scheduler tick", extra={"orgs": len(started), "started": n})
        except Exception as exc:  # DB down: keep the loop alive, retry next tick
            log.warning("scheduler tick failed", extra={"error": type(exc).__name__})
        try:
            await asyncio.wait_for(stop.wait(), interval)
        except TimeoutError:
            pass


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.connectors.scheduler")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between ticks")
    # JSON logging with secret scrubbing is already set up by app.worker (imported via sync).
    asyncio.run(run(p.parse_args().interval))


if __name__ == "__main__":
    main()
