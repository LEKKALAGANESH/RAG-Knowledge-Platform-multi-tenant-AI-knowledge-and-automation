"""refresh-token sessions (rotation, reuse detection, revocation) + member-session revoke fn

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"

# Per-user RLS (not per org): a login is one family that can switch orgs, and refresh runs
# before any org is trusted. org_id records which org the token was issued for.
SCHEMA = """
CREATE TABLE sessions (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  family_id uuid NOT NULL,
  parent_id uuid REFERENCES sessions(id) ON DELETE SET NULL,
  replaced_by uuid REFERENCES sessions(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  ip_hash text,
  user_agent_hash text
);
CREATE INDEX sessions_family_idx ON sessions (family_id);
CREATE INDEX sessions_user_live_idx ON sessions (user_id, org_id) WHERE revoked_at IS NULL;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY user_isolation ON sessions USING (user_id = {user}) WITH CHECK (user_id = {user});
"""

# Removing a member must end THEIR sessions, which per-user RLS hides from the admin doing it.
# Owner-run, and limited to the org the caller's transaction is scoped to (app.org_id).
REVOKE_FN = """
CREATE FUNCTION revoke_member_sessions(member uuid) RETURNS integer
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  WITH r AS (
    UPDATE public.sessions SET revoked_at = now()
    WHERE user_id = member AND revoked_at IS NULL
      AND org_id = NULLIF(current_setting('app.org_id', true), '')::uuid
    RETURNING 1
  )
  SELECT count(*)::integer FROM r
$fn$
"""


def upgrade() -> None:
    for stmt in SCHEMA.replace("{user}", USER).split(";\n"):
        if stmt.strip():
            op.execute(stmt)
    op.execute(f"REVOKE DELETE, TRUNCATE ON sessions FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON sessions TO {APP_ROLE}")
    op.execute(REVOKE_FN)
    op.execute("REVOKE ALL ON FUNCTION revoke_member_sessions(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION revoke_member_sessions(uuid) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION revoke_member_sessions(uuid)")
    op.execute("DROP TABLE sessions")
