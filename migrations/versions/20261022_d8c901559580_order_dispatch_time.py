"""order dispatch time

Revision ID: d8c901559580
Revises: 8cd1678869eb
Create Date: 2026-10-22 00:04:39.934859

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8c901559580"
down_revision: str | Sequence[str] | None = "8cd1678869eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("dispatch_at", sa.DateTime(timezone=True), nullable=True))
    # Orders placed before this column existed were planned for immediate dispatch.
    op.execute("UPDATE orders SET dispatch_at = created_at")
    op.alter_column("orders", "dispatch_at", nullable=False)


def downgrade() -> None:
    op.drop_column("orders", "dispatch_at")
