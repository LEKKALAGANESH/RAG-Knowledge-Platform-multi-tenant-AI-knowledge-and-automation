"""due_sync_orgs(): the scheduler's only cross-org read (org ids, nothing else)

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"

# rag_app sees one org at a time (FORCE RLS), so it cannot find which orgs need a scheduler tick.
# This owner-run function answers only that: org ids with a due source (same rule as
# sync.start_due_syncs) or a `running` run older than stale_after (sync.STALE_AFTER is the single
# source of truth; the caller passes it). All reads/writes then happen in per-org RLS sessions.
# row_security = off makes a non-bypassing owner fail loudly instead of silently returning nothing.
FN = """
CREATE FUNCTION due_sync_orgs(stale_after interval) RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  SELECT org_id FROM public.sources
  WHERE schedule_minutes IS NOT NULL AND status = 'idle'
    AND coalesce(last_sync_at, created_at) + schedule_minutes * interval '1 minute' <= now()
  UNION
  SELECT org_id FROM public.sync_runs
  WHERE status = 'running' AND started_at < now() - stale_after
$fn$
"""


def upgrade() -> None:
    op.execute(FN)
    op.execute("REVOKE ALL ON FUNCTION due_sync_orgs(interval) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION due_sync_orgs(interval) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION due_sync_orgs(interval)")
