"""users RLS (S9) + org_invites with in-app accept (S10)

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
PENDING = "accepted_at IS NULL AND declined_at IS NULL AND revoked_at IS NULL"

# A session sees its own user row and the members of its current org (memberships RLS limits the
# subquery to app.org_id rows + its own). It may only insert itself (register). pw_hash is not
# readable by rag_app at all: login reads it through auth_user_by_email(). No UPDATE/DELETE.
SCHEMA = """
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY user_visibility ON users FOR SELECT
  USING (id = {user} OR id IN (SELECT user_id FROM memberships));
CREATE POLICY user_self_insert ON users FOR INSERT WITH CHECK (id = {user});

CREATE TABLE org_invites (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  email text NOT NULL,
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
  invited_by uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  accepted_at timestamptz,
  declined_at timestamptz,
  revoked_at timestamptz
);
CREATE INDEX org_invites_org_idx ON org_invites (org_id) WHERE {pending};
CREATE INDEX org_invites_email_idx ON org_invites (email) WHERE {pending};
ALTER TABLE org_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_invites FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON org_invites USING (org_id = {org}) WITH CHECK (org_id = {org})
"""

# Login runs before any user context exists. One row by exact email; the caller verifies the hash.
LOGIN_FN = """
CREATE FUNCTION auth_user_by_email(lookup text)
RETURNS TABLE (id uuid, pw_hash text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  SELECT u.id, u.pw_hash FROM public.users u WHERE u.email = lookup
$fn$
"""

# The invitee is not in the inviting org, so org RLS hides the invite from them. These match
# invites to the CALLER's own email (app.user_id -> users.email) and nothing else.
MY_INVITES_FN = """
CREATE FUNCTION my_invites()
RETURNS TABLE (id uuid, org_id uuid, org_name text, role text,
               created_at timestamptz, expires_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  SELECT i.id, i.org_id, o.name, i.role, i.created_at, i.expires_at
  FROM public.org_invites i
  JOIN public.orgs o ON o.id = i.org_id
  JOIN public.users u ON u.email = i.email
  WHERE u.id = NULLIF(current_setting('app.user_id', true), '')::uuid
    AND i.accepted_at IS NULL AND i.declined_at IS NULL AND i.revoked_at IS NULL
    AND i.expires_at > now()
  ORDER BY i.created_at
$fn$
"""

# Accept (membership with the invited role; an existing membership is left as is) or decline.
# Only a pending, unexpired invite for the caller's email changes; else no row comes back.
RESPOND_FN = """
CREATE FUNCTION respond_invite(invite uuid, accept boolean)
RETURNS TABLE (org_id uuid, role text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off AS $fn$
  WITH me AS (
    SELECT u.id, u.email FROM public.users u
    WHERE u.id = NULLIF(current_setting('app.user_id', true), '')::uuid
  ), done AS (
    UPDATE public.org_invites v
    SET accepted_at = CASE WHEN accept THEN now() END,
        declined_at = CASE WHEN accept THEN NULL ELSE now() END
    FROM me
    WHERE v.id = invite AND v.email = me.email
      AND v.accepted_at IS NULL AND v.declined_at IS NULL AND v.revoked_at IS NULL
      AND v.expires_at > now()
    RETURNING v.org_id, v.role, me.id AS user_id
  ), joined AS (
    INSERT INTO public.memberships (org_id, user_id, role)
    SELECT done.org_id, done.user_id, done.role FROM done WHERE accept
    ON CONFLICT DO NOTHING
  )
  SELECT done.org_id, done.role FROM done
$fn$
"""

FUNCTIONS = {
    "auth_user_by_email(text)": LOGIN_FN,
    "my_invites()": MY_INVITES_FN,
    "respond_invite(uuid, boolean)": RESPOND_FN,
}


def upgrade() -> None:
    sql = SCHEMA.replace("{user}", USER).replace("{org}", ORG).replace("{pending}", PENDING)
    for stmt in sql.split(";\n"):
        if stmt.strip():
            op.execute(stmt)
    # Column privileges: rag_app never reads pw_hash and never rewrites a user row.
    op.execute(f"REVOKE SELECT, UPDATE, DELETE, TRUNCATE ON users FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT (id, email, created_at), INSERT ON users TO {APP_ROLE}")
    # Invites change state (accept/decline/revoke), never disappear: the trail stays.
    op.execute(f"REVOKE DELETE, TRUNCATE ON org_invites FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON org_invites TO {APP_ROLE}")
    for sig, body in FUNCTIONS.items():
        op.execute(body)
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO {APP_ROLE}")


def downgrade() -> None:
    for sig in FUNCTIONS:
        op.execute(f"DROP FUNCTION {sig}")
    op.execute("DROP TABLE org_invites")
    for policy in ("user_visibility", "user_self_insert", "user_self_delete"):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON users")
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON users TO {APP_ROLE}")
