"""api_keys expiry/last_used + lookup fn, append-only audit_log + RLS

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"

SCHEMA = """
ALTER TABLE api_keys ADD COLUMN expires_at timestamptz, ADD COLUMN last_used_at timestamptz;

CREATE TABLE audit_log (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  actor_user_id uuid,
  actor_api_key_id uuid,
  action text NOT NULL,
  target_type text,
  target_id uuid,
  request_id text,
  ip_hash text,
  meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_page_idx ON audit_log (org_id, created_at DESC, id DESC);
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit_log USING (org_id = {org}) WITH CHECK (org_id = {org});
"""

# An API key arrives before its org is known, and rag_app sees one org at a time (FORCE RLS).
# This owner-run function is the only cross-org read of api_keys: one row by its unique prefix.
# The caller compares the hash in constant time; rows never leave without a matching prefix.
LOOKUP = """
CREATE FUNCTION api_key_lookup(key_prefix text)
RETURNS TABLE (id uuid, org_id uuid, user_id uuid, hash text, scopes text[],
               expires_at timestamptz, revoked_at timestamptz, last_used_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  SELECT id, org_id, user_id, hash, scopes, expires_at, revoked_at, last_used_at
  FROM public.api_keys WHERE prefix = key_prefix
$fn$
"""


def upgrade() -> None:
    for stmt in SCHEMA.replace("{org}", ORG).split(";\n"):
        if stmt.strip():
            op.execute(stmt)
    # Append-only for the app: default privileges granted it everything on new tables.
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {APP_ROLE}")
    op.execute(LOOKUP)
    op.execute("REVOKE ALL ON FUNCTION api_key_lookup(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION api_key_lookup(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION api_key_lookup(text)")
    op.execute("DROP TABLE audit_log")
    op.execute("ALTER TABLE api_keys DROP COLUMN expires_at, DROP COLUMN last_used_at")
