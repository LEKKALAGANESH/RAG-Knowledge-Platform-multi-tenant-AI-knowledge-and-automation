# ADR 0008: Refresh sessions with a reuse grace window

Status: Accepted (A6b)

## Context

Refresh tokens were stateless, so logout and member removal could not revoke them. Rotating refresh
tokens with reuse detection fixes that, but the frontend BFF can refresh in parallel (several
requests hit a 401 at once); strict reuse detection would then sign users out at random.

## Decision

- Table `sessions` (migration 0008): one row per refresh token, linked in a family (`parent_id`,
  `replaced_by`). The refresh JWT `jti` is the row id. RLS is per user, since one login can switch
  orgs.
- Refresh locks the row (`FOR UPDATE`). If it was already replaced and the reuse comes within
  `REFRESH_REUSE_GRACE_SECONDS` (30), the same successor pair is returned again: tokens are
  deterministic in (row id, created_at), so no token hash needs storing. After the window, reuse is
  treated as theft: the whole family is revoked, `401 session_revoked`, audit
  `session.reuse_detected`.
- Access tokens stay stateless (15 min). Logout, logout-all and member removal take effect at the next
  refresh. Member removal uses `revoke_member_sessions()` (per-user RLS hides the rows from the admin).

## Consequences

- Parallel refreshes are safe; a stolen refresh token is caught once both parties use it.
- Revocation lags by up to the access-token TTL.
- Rows accumulate (one per refresh); a prune job needs DELETE as the owner role (backlog).
- Pre-0008 refresh tokens were invalidated (one extra sign-in after deploy).

## Alternatives considered

- **Stateless refresh tokens:** no revocation.
- **Strict rotation without grace:** random sign-outs under parallel refresh.
- **A BFF-side refresh lock:** covers one client only, not several server instances.
- **Storing token hashes:** unnecessary; HS256 signing already prevents minting tokens from DB access.
