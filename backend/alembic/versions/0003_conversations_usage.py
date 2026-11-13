"""conversations, messages, usage_events + RLS; UNIQUE (document_id, ord) on chunks

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
TABLES = ["conversations", "messages", "usage_events"]

SCHEMA = """
ALTER TABLE chunks ADD CONSTRAINT chunks_document_ord_key UNIQUE (document_id, ord);
DROP INDEX chunks_document_idx;

CREATE TABLE conversations (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kb_ids uuid[] NOT NULL DEFAULT '{}',
  title text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id)
);
CREATE INDEX conversations_page_idx ON conversations (org_id, user_id, created_at DESC, id DESC);

CREATE TABLE messages (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  conversation_id uuid NOT NULL,
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL,
  citations jsonb NOT NULL DEFAULT '[]',
  usage jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, conversation_id) REFERENCES conversations (org_id, id) ON DELETE CASCADE
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at);

CREATE TABLE usage_events (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  kind text NOT NULL CHECK (kind IN ('chat', 'embed', 'rewrite')),
  provider text NOT NULL,
  model text NOT NULL,
  in_tok integer NOT NULL CHECK (in_tok >= 0),
  out_tok integer NOT NULL CHECK (out_tok >= 0),
  cost_usd numeric(12, 6),
  request_id text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX usage_events_org_idx ON usage_events (org_id, created_at DESC);
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
    # Conversations are private to their author, not just their org.
    _exec(_rls("conversations", f"org_id = {ORG} AND user_id = {USER}"))
    for table in ("messages", "usage_events"):
        _exec(_rls(table, f"org_id = {ORG}"))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE {', '.join(reversed(TABLES))} CASCADE")
    op.execute("ALTER TABLE chunks DROP CONSTRAINT chunks_document_ord_key")
    op.execute("CREATE INDEX chunks_document_idx ON chunks (document_id, ord)")
