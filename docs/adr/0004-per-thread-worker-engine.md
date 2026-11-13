# ADR 0004: One database engine per worker thread

Status: Accepted (A2.2, A2.3)

## Context

Dramatiq runs actors on several threads; each actor runs its async code with `asyncio.run`, so each
thread has its own event loop. The worker shared one cached SQLAlchemy async engine. When two jobs
made the engine's first connection at the same time, one hung forever: the engine's first-connect
guard is an `asyncio.Lock`, and a release from thread A's loop never wakes a waiter on thread B's
loop. A thread dump and a gc walk of the stuck coroutine confirmed it.

## Decision

`app/core/db.py::worker_sessions()` returns a session factory backed by one engine per worker thread
(`threading.local`) with `NullPool` (asyncpg connections cannot cross loops). Every actor uses it;
per-module engines were removed. The API keeps its normal pooled engine.

## Consequences

- The default worker command works (`dramatiq app.worker`, many threads); regression tests run a real
  worker with 2 threads.
- No connection pooling inside workers: one connection per task. Fine at current load.
- New actors must use `worker_sessions()`.

## Alternatives considered

- **`--threads 1`:** hides the bug and wastes the worker.
- **One long-lived event loop per thread with a pooled engine:** more moving parts; not needed yet.
- **Sync SQLAlchemy in workers:** a second data-access style to maintain.
