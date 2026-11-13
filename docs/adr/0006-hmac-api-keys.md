# ADR 0006: API keys stored as peppered HMAC, not argon2

Status: Accepted (A6a)

## Context

API keys authenticate every programmatic request. They must never be stored in plain text, must be
found without knowing the org, and checking them must not add noticeable latency.

## Decision

Key format `rag_<12 hex prefix>_<token_urlsafe(32)>` (256-bit secret). Store the prefix (unique,
O(1) lookup through `api_key_lookup`) and `HMAC-SHA256(pepper, secret)`, compared with
`hmac.compare_digest`. Pepper = `API_KEY_PEPPER`, or derived from `JWT_SECRET` when unset. The same
keyed hash is used for audit IPs.

## Consequences

- Verification costs microseconds, not ~50 ms per request.
- A database dump alone does not reveal keys or allow offline guessing without the pepper.
- If `API_KEY_PEPPER` is unset, rotating `JWT_SECRET` invalidates every key; set it in production.
  An empty `API_KEY_PEPPER=` line yields an empty HMAC key (see [.env.example.md](../.env.example.md)).
- The plaintext key is shown once, at creation.

## Alternatives considered

- **argon2 or bcrypt:** built for low-entropy passwords; against 256 random bits a slow hash buys
  nothing and costs latency on every request.
- **Plain SHA-256:** works for random secrets, but a pepper adds protection if only the DB leaks.
- **Encrypting keys (Fernet):** reversible storage is not needed.
