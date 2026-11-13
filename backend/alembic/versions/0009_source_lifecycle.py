"""sources.last_error + sources.needs_reconnect (OAuth source lifecycle)

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "sources",
        sa.Column("needs_reconnect", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("sources", "needs_reconnect")
    op.drop_column("sources", "last_error")
