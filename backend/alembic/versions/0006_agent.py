"""agent_runs, agent_steps, approvals, reports + RLS

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
ORG = "NULLIF(current_setting('app.org_id', true), '')::uuid"
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"
TABLES = ["agent_runs", "agent_steps", "approvals", "reports"]

SCHEMA = """
CREATE TABLE agent_runs (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  goal text NOT NULL,
  kb_ids uuid[] NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'planning' CHECK (status IN
    ('planning', 'running', 'awaiting_approval', 'succeeded', 'failed', 'cancelled')),
  max_steps integer NOT NULL CHECK (max_steps BETWEEN 1 AND 25),
  result jsonb,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (org_id, id)
);
CREATE INDEX agent_runs_page_idx ON agent_runs (org_id, user_id, created_at DESC, id DESC);

CREATE TABLE agent_steps (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  run_id uuid NOT NULL,
  n integer NOT NULL CHECK (n >= 1),
  kind text NOT NULL CHECK (kind IN ('plan', 'tool_call', 'tool_result', 'message')),
  tool text,
  input jsonb,
  output jsonb,
  risk text CHECK (risk IN ('read', 'reversible_write', 'irreversible_write')),
  started_at timestamptz,
  finished_at timestamptz,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, id),
  UNIQUE (run_id, n),
  FOREIGN KEY (org_id, run_id) REFERENCES agent_runs (org_id, id) ON DELETE CASCADE
);
-- One call row and one result row per model tool_use id: a second worker on the same run
-- fails here instead of executing a tool twice.
CREATE UNIQUE INDEX agent_steps_tool_use_key ON agent_steps (run_id, kind, (input->>'tool_use_id'))
  WHERE kind IN ('tool_call', 'tool_result');

CREATE TABLE approvals (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  run_id uuid NOT NULL,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  step_id uuid NOT NULL,
  tool text NOT NULL,
  input jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  decided_by uuid REFERENCES users(id) ON DELETE SET NULL,
  decided_at timestamptz,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, run_id) REFERENCES agent_runs (org_id, id) ON DELETE CASCADE,
  FOREIGN KEY (org_id, step_id) REFERENCES agent_steps (org_id, id) ON DELETE CASCADE
);
CREATE INDEX approvals_run_idx ON approvals (run_id);

CREATE TABLE reports (
  id uuid PRIMARY KEY,
  org_id uuid NOT NULL,
  run_id uuid NOT NULL,
  name text NOT NULL,
  mime text NOT NULL,
  blob_key text NOT NULL,
  size integer NOT NULL CHECK (size >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (org_id, run_id) REFERENCES agent_runs (org_id, id) ON DELETE CASCADE
);
CREATE INDEX reports_run_idx ON reports (run_id);
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
    # Runs are private to their author, like conversations. Steps and reports follow their run
    # (the subquery runs under agent_runs' own policy). Approvals are org-wide so an org admin
    # can decide one; the API still checks owner-or-admin.
    _exec(_rls("agent_runs", f"org_id = {ORG} AND user_id = {USER}"))
    own_run = f"org_id = {ORG} AND EXISTS (SELECT 1 FROM agent_runs r WHERE r.id = run_id)"
    for table in ("agent_steps", "reports"):
        _exec(_rls(table, own_run))
    _exec(_rls("approvals", f"org_id = {ORG}"))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP TABLE {', '.join(reversed(TABLES))} CASCADE")
