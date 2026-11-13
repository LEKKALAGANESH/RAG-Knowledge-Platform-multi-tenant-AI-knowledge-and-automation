# ADR 0001: Modular monolith API + Dramatiq workers

Status: Accepted (wave 1)

## Context

The platform needs an HTTP API, long-running background work (parsing, embedding, crawling, agent
runs) and a periodic scheduler. One team builds it, there is no measured scaling need for separate
services, and every feature shares the same tenant model and database.

## Decision

One Python codebase (`backend/app/<module>/`) deployed as three process types: the FastAPI API, the
Dramatiq worker (`app.worker`, queues `ingest`, `index`, `sync`, `agent`) and the scheduler
(`app.connectors.scheduler`). Redis is the broker. Modules talk through function calls and the
database; provider adapters are a `Protocol` plus concrete classes.

## Consequences

- One deploy, one migration chain, one RLS model; a feature touches one module.
- Each process type scales on its own (API replicas, worker processes and threads, queue selection).
- Module boundaries are enforced by convention only. Import cycles bit once (actor modules imported
  from package `__init__`s); the rule now is empty `__init__`s and every actor imported in
  `app/worker.py`.

## Alternatives considered

- **Microservices:** rejected: network boundaries and duplicated auth/tenancy with no evidence they
  were needed.
- **Celery:** heavier configuration for the same retries and dead-letter features.
- **Temporal:** better for long workflows; revisit if agent runs need it.
