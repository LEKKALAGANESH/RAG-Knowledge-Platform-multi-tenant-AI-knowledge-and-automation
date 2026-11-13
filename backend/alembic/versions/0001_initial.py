"""initial schema: orgs, users, memberships, api_keys, knowledge_bases, documents, chunks + RLS

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
# NULLIF: once a connection has run SET LOCAL, the unset value is '' (not NULL), and ''::uuid errors.
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
TABLES = ["orgs", "users", "memberships", "api_keys", "knowledge_bases", "documents", "chunks"]

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE orgs (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  settings jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id uuid PRIMARY KEY,
  email text NOT NULL UNIQUE,
  pw_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX memberships_user_idx ON memberships (user_id);

CREATE TABLE api_keys (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name text NOT NULL,
  prefix text NOT NULL UNIQUE,
  hash text NOT NULL,
  scopes text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);
CREATE INDEX api_keys_org_idx ON api_keys (org_id);

CREATE TABLE knowledge_bases (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  name text NOT NULL,
  settings jsonb NOT NULL DEFAULT '{}',
  embedding_index_id uuid,  -- FK added with embedding_indexes (0002)
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id)
);
CREATE INDEX knowledge_bases_page_idx ON knowledge_bases (org_id, created_at DESC, id DESC);

-- Composite (org_id, ...) FKs: RLS does not apply to FK checks, so a plain kb_id FK would let a
-- row point at another org's parent. Including org_id makes cross-org references impossible.
CREATE TABLE documents (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  kb_id uuid NOT NULL,
  source_id uuid,  -- FK added with sources (0004)
  name text NOT NULL,
  mime text NOT NULL,
  sha256 text NOT NULL,
  blob_key text NOT NULL,
  version integer NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'deleted')),
  error text,
  warnings jsonb NOT NULL DEFAULT '[]',
  meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, kb_id) REFERENCES knowledge_bases (org_id, id) ON DELETE CASCADE,
  UNIQUE (kb_id, sha256, version),
  UNIQUE (org_id, kb_id, id)
);
CREATE INDEX documents_page_idx ON documents (kb_id, created_at DESC, id DESC);

CREATE TABLE chunks (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  kb_id uuid NOT NULL,
  document_id uuid NOT NULL,
  parent_id uuid REFERENCES chunks(id) ON DELETE CASCADE,
  ord integer NOT NULL,
  text text NOT NULL,
  loc jsonb NOT NULL DEFAULT '{}',
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, kb_id, document_id)
    REFERENCES documents (org_id, kb_id, id) ON DELETE CASCADE
);
CREATE INDEX chunks_document_idx ON chunks (document_id, ord);
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
"""


def _exec(sql: str) -> None:
    # asyncpg runs one statement per call; statements here end with ";\n".
    for stmt in sql.split(";\n"):
        if stmt.strip():
            op.execute(stmt)


def _rls(table: str, using: str, check: str) -> str:
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table} USING ({using}) WITH CHECK ({check});
"""


def upgrade() -> None:
    op.execute(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') "
        f"THEN CREATE ROLE {APP_ROLE} NOLOGIN; END IF; END $$"
    )
    _exec(SCHEMA)
    # A user sees orgs/memberships they belong to (org switcher, login); writes stay org-scoped.
    _exec(_rls("orgs", f"id = {ORG} OR id IN (SELECT org_id FROM memberships)", f"id = {ORG}"))
    _exec(_rls("memberships", f"org_id = {ORG} OR user_id = {USER}", f"org_id = {ORG}"))
    for table in ("api_keys", "knowledge_bases", "documents", "chunks"):
        _exec(_rls(table, f"org_id = {ORG}", f"org_id = {ORG}"))
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(f"DROP TABLE {', '.join(reversed(TABLES))} CASCADE")
