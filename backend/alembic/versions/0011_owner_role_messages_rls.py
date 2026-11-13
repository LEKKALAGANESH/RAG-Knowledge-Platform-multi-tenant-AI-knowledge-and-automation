"""non-superuser owner (S14, ADR 0009) + messages follow their conversation (N6)

Revision ID: 0011
Revises: 0010

Needs the rag_owner / rag_definer roles from docker/initdb/02-owner-role.sh. On a DB built before
that, run 0011 once as the superuser (it moves ownership off it); after that, migrations run as
rag_owner.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE, OWNER, DEFINER = "rag_app", "rag_owner", "rag_definer"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"

ROLES_EXIST = f"""
DO $$ BEGIN
  IF (SELECT count(*) FROM pg_roles WHERE rolname IN ('{OWNER}', '{DEFINER}')) < 2 THEN
    RAISE EXCEPTION 'roles {OWNER}/{DEFINER} missing: run backend/docker/initdb/02-owner-role.sh';
  END IF;
END $$
"""

# Extension members (pgvector's types/functions) stay with the extension.
NOT_EXT = (
    "NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = '{cat}'::regclass "
    "AND d.objid = {oid} AND d.deptype = 'e')"
)

# Tables (+ their indexes and linked sequences), views, free-standing sequences.
TABLES_TO_OWNER = f"""
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT c.oid::regclass AS obj,
      CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                     WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END AS kind
    FROM pg_class c
    WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
      AND {NOT_EXT.format(cat="pg_class", oid="c.oid")}
      AND NOT (c.relkind = 'S' AND EXISTS (SELECT 1 FROM pg_depend d
        WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype IN ('a', 'i')))
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO {OWNER}', r.kind, r.obj);
  END LOOP;
END $$
"""

# A definer function with row_security = off must bypass FORCE'd RLS, which a table owner cannot:
# it goes to rag_definer. Everything else (embedding_hnsw needs table ownership for CREATE INDEX)
# goes to rag_owner. Future migrations: create as rag_owner, then this same rule.
FUNCTIONS_TO_OWNERS = f"""
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT p.oid::regprocedure AS fn,
      p.prosecdef AND 'row_security=off' = ANY(coalesce(p.proconfig, '{{}}')) AS bypass
    FROM pg_proc p
    WHERE p.pronamespace = 'public'::regnamespace AND {NOT_EXT.format(cat="pg_proc", oid="p.oid")}
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO %I', r.fn,
                   CASE WHEN r.bypass THEN '{DEFINER}' ELSE '{OWNER}' END);
  END LOOP;
END $$
"""

# Exactly what the bypassing bodies touch (0005, 0007, 0008, 0010). CREATE on the schema is only
# so a function can be owned by rag_definer (PG checks it on ALTER ... OWNER); it cannot log in.
DEFINER_GRANTS = [
    f"GRANT USAGE, CREATE ON SCHEMA public TO {DEFINER}",
    f"GRANT SELECT ON api_keys, sources, sync_runs, sessions, users, orgs, org_invites TO {DEFINER}",
    f"GRANT UPDATE ON sessions, org_invites TO {DEFINER}",
    f"GRANT INSERT ON memberships TO {DEFINER}",
]

# 0001's default grant was made by the old owner; new tables now come from rag_owner.
DEFAULT_PRIVS = (
    f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA public "
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
)

MESSAGES_OWN = (
    f"org_id = {ORG} AND EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_id)"
)


def _messages_policy(using: str) -> None:
    op.execute("DROP POLICY tenant_isolation ON messages")
    op.execute(f"CREATE POLICY tenant_isolation ON messages USING ({using}) WITH CHECK ({using})")


def upgrade() -> None:
    op.execute(ROLES_EXIST)
    op.execute(TABLES_TO_OWNER)
    for stmt in DEFINER_GRANTS:
        op.execute(stmt)
    op.execute(FUNCTIONS_TO_OWNERS)
    op.execute(DEFAULT_PRIVS)
    # Messages follow their conversation (per-user), like agent_steps follow agent_runs (0006).
    _messages_policy(MESSAGES_OWN)


def downgrade() -> None:
    # Hands everything back to the caller: run as the superuser to get the pre-0011 state.
    _messages_policy(f"org_id = {ORG}")
    op.execute(DEFAULT_PRIVS.replace(" GRANT ", " REVOKE ").replace(" TO ", " FROM "))
    for stmt in reversed(DEFINER_GRANTS):
        op.execute(stmt.replace("GRANT ", "REVOKE ", 1).replace(" TO ", " FROM "))
    op.execute(TABLES_TO_OWNER.replace(f"OWNER TO {OWNER}", "OWNER TO CURRENT_USER"))
    op.execute(
        FUNCTIONS_TO_OWNERS.replace(
            f"CASE WHEN r.bypass THEN '{DEFINER}' ELSE '{OWNER}' END", "current_user"
        )
    )
