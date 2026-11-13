"""embedding_indexes + chunk_embeddings (per-index partial HNSW) + RLS

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
TABLES = ["embedding_indexes", "chunk_embeddings"]

SCHEMA = """
ALTER TABLE chunks ADD CONSTRAINT chunks_org_id_id_key UNIQUE (org_id, id);

CREATE TABLE embedding_indexes (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  kb_id uuid NOT NULL,
  provider text NOT NULL,
  model text NOT NULL,
  dim integer NOT NULL CHECK (dim BETWEEN 1 AND 2000),
  version integer NOT NULL,
  status text NOT NULL DEFAULT 'building' CHECK (status IN ('building', 'active', 'retired')),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, kb_id) REFERENCES knowledge_bases (org_id, id) ON DELETE CASCADE,
  UNIQUE (kb_id, version),
  UNIQUE (org_id, id)
);

ALTER TABLE knowledge_bases ADD CONSTRAINT knowledge_bases_embedding_index_fk
  FOREIGN KEY (org_id, embedding_index_id) REFERENCES embedding_indexes (org_id, id);

-- Untyped `vector` column: each index has its own dim; queries cast to vector(dim) so the
-- per-index partial HNSW (created at runtime by embedding_hnsw) is usable.
CREATE TABLE chunk_embeddings (
  chunk_id uuid NOT NULL,
  index_id uuid NOT NULL,
  org_id uuid NOT NULL,
  embedding vector NOT NULL,
  PRIMARY KEY (index_id, chunk_id),
  FOREIGN KEY (org_id, chunk_id) REFERENCES chunks (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, index_id) REFERENCES embedding_indexes (org_id, id) ON DELETE CASCADE
);
CREATE INDEX chunk_embeddings_chunk_idx ON chunk_embeddings (chunk_id);
"""

# rag_app does not own chunk_embeddings, so it cannot CREATE INDEX itself. This owner-run function
# only builds/drops the HNSW for an index row of the caller's own org; dim comes from that row.
HNSW_FN = f"""
CREATE FUNCTION embedding_hnsw(p_index uuid, p_create boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE
  d integer;
  n text := 'chunk_embeddings_hnsw_' || replace(p_index::text, '-', '');
BEGIN
  SELECT dim INTO d FROM embedding_indexes WHERE id = p_index AND org_id = {ORG};
  IF d IS NULL THEN
    RAISE EXCEPTION 'embedding index % not found', p_index;
  END IF;
  IF p_create THEN
    EXECUTE format(
      'CREATE INDEX IF NOT EXISTS %I ON chunk_embeddings USING hnsw '
      '((embedding::vector(%s)) vector_cosine_ops) WHERE index_id = %L', n, d, p_index);
  ELSE
    EXECUTE format('DROP INDEX IF EXISTS %I', n);
  END IF;
END
$fn$
"""


def _exec(sql: str) -> None:
    for stmt in sql.split(";\n"):
        if stmt.strip():
            op.execute(stmt)


def upgrade() -> None:
    _exec(SCHEMA)
    for table in TABLES:
        _exec(f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table} USING (org_id = {ORG}) WITH CHECK (org_id = {ORG});
""")
    op.execute(HNSW_FN)
    op.execute("REVOKE ALL ON FUNCTION embedding_hnsw(uuid, boolean) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION embedding_hnsw(uuid, boolean) TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION embedding_hnsw(uuid, boolean)")
    op.execute("ALTER TABLE knowledge_bases DROP CONSTRAINT knowledge_bases_embedding_index_fk")
    op.execute("UPDATE knowledge_bases SET embedding_index_id = NULL")
    op.execute(f"DROP TABLE {', '.join(reversed(TABLES))} CASCADE")
    op.execute("ALTER TABLE chunks DROP CONSTRAINT chunks_org_id_id_key")
