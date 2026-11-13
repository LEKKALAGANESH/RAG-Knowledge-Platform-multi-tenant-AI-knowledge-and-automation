"""H3 cleanup: usage kind `agent`, audit actor email snapshot, due_sync_orgs floor, session pruning

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE, DEFINER = "rag_app", "rag_definer"
KINDS_OLD = "'chat', 'embed', 'rewrite'"

# Same body as 0005, except a caller can no longer shrink stale_after below an hour (rag_app could
# otherwise call it with interval '0' and enumerate every org with a running sync).
DUE_FN = """
CREATE OR REPLACE FUNCTION due_sync_orgs(stale_after interval) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  SELECT org_id FROM public.sources
  WHERE schedule_minutes IS NOT NULL AND status = 'idle'
    AND coalesce(last_sync_at, created_at) + schedule_minutes * interval '1 minute' <= now()
  UNION
  SELECT org_id FROM public.sync_runs
  WHERE status = 'running' AND started_at < now() - {stale}
$fn$
"""

# Sessions are per-user RLS and rag_app has no DELETE on them (0008), so the scheduler prunes
# through this. It deletes only rows dead for `keep` (floored at 1 day); FKs between sessions rows
# are ON DELETE SET NULL, so a pruned parent/successor never blocks or breaks a kept row.
PRUNE_FN = """
CREATE FUNCTION prune_sessions(keep interval) RETURNS integer
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  WITH d AS (
    DELETE FROM public.sessions
    WHERE expires_at < now() - GREATEST(keep, interval '1 day')
       OR revoked_at < now() - GREATEST(keep, interval '1 day')
    RETURNING 1
  )
  SELECT count(*)::integer FROM d
$fn$
"""


def _kinds(values: str, suffix: str = "") -> None:
    op.execute("ALTER TABLE usage_events DROP CONSTRAINT usage_events_kind_check")
    op.execute(
        "ALTER TABLE usage_events ADD CONSTRAINT usage_events_kind_check "
        f"CHECK (kind IN ({values})){suffix}"
    )


def upgrade() -> None:
    _kinds(f"{KINDS_OLD}, 'agent'")
    op.execute("ALTER TABLE audit_log ADD COLUMN actor_email text")
    op.execute(DUE_FN.format(stale="GREATEST(stale_after, interval '1 hour')"))
    op.execute(f"GRANT DELETE ON sessions TO {DEFINER}")
    op.execute(PRUNE_FN)
    op.execute(f"ALTER FUNCTION prune_sessions(interval) OWNER TO {DEFINER}")
    op.execute("REVOKE ALL ON FUNCTION prune_sessions(interval) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION prune_sessions(interval) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION prune_sessions(interval)")
    op.execute(f"REVOKE DELETE ON sessions FROM {DEFINER}")
    op.execute(DUE_FN.format(stale="stale_after"))
    op.execute("ALTER TABLE audit_log DROP COLUMN actor_email")
    # NOT VALID: kept `agent` rows stay (FORCE RLS hides them from a data fix run as rag_owner).
    _kinds(KINDS_OLD, " NOT VALID")
