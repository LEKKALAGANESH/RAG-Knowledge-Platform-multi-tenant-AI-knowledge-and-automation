"""sources + sync_runs + RLS; documents.external_key/content_hash for connector-owned documents

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
TABLES = ["sources", "sync_runs"]

SCHEMA = """
CREATE TABLE sources (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  kb_id uuid NOT NULL,
  type text NOT NULL,
  name text NOT NULL,
  config jsonb NOT NULL DEFAULT '{}',
  secret_enc bytea,
  status text NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'syncing', 'error', 'disabled')),
  cursor jsonb NOT NULL DEFAULT '{}',
  last_sync_at timestamptz,
  schedule_minutes integer CHECK (schedule_minutes IS NULL OR schedule_minutes >= 15),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, kb_id) REFERENCES knowledge_bases (org_id, id) ON DELETE CASCADE,
  UNIQUE (org_id, id),
  UNIQUE (org_id, kb_id, id)
);
CREATE INDEX sources_page_idx ON sources (kb_id, created_at DESC, id DESC);

CREATE TABLE sync_runs (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  source_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'running'
    CHECK (status IN ('running', 'succeeded', 'failed', 'partial')),
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  stats jsonb NOT NULL DEFAULT '{}',
  error text,
  FOREIGN KEY (org_id, source_id) REFERENCES sources (org_id, id) ON DELETE CASCADE
);
CREATE INDEX sync_runs_source_idx ON sync_runs (source_id, started_at DESC);
-- One running sync per source, enforced by the DB rather than a check-then-insert race.
CREATE UNIQUE INDEX sync_runs_one_running ON sync_runs (source_id) WHERE status = 'running';

ALTER TABLE documents ADD COLUMN external_key text;
ALTER TABLE documents ADD COLUMN content_hash text;
-- (org_id, kb_id, source_id): a source's documents can only live in that source's KB and org.
ALTER TABLE documents ADD CONSTRAINT documents_source_fk FOREIGN KEY (org_id, kb_id, source_id)
  REFERENCES sources (org_id, kb_id, id) ON DELETE CASCADE;
ALTER TABLE documents ADD CONSTRAINT documents_source_key UNIQUE (source_id, external_key);
-- Upload dedupe stays per KB; two crawled pages may legitimately share content.
ALTER TABLE documents DROP CONSTRAINT documents_kb_id_sha256_version_key;
CREATE UNIQUE INDEX documents_upload_sha_key ON documents (kb_id, sha256, version)
  WHERE source_id IS NULL;
"""


def _exec(sql: str) -> None:
    for stmt in sql.split(";\n"):
        if stmt.strip():
            op.execute(stmt)


def _rls(table: str, using: str) -> str:
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table} USING ({using}) WITH CHECK ({using});
"""


def upgrade() -> None:
    _exec(SCHEMA)
    for table in TABLES:
        _exec(_rls(table, f"org_id = {ORG}"))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")


def downgrade() -> None:
    _exec("""
DELETE FROM documents WHERE source_id IS NOT NULL;
DROP INDEX documents_upload_sha_key;
ALTER TABLE documents ADD CONSTRAINT documents_kb_id_sha256_version_key UNIQUE (kb_id, sha256, version);
ALTER TABLE documents DROP CONSTRAINT documents_source_key;
ALTER TABLE documents DROP CONSTRAINT documents_source_fk;
ALTER TABLE documents DROP COLUMN content_hash;
ALTER TABLE documents DROP COLUMN external_key;
DROP TABLE sync_runs, sources CASCADE;
""")
