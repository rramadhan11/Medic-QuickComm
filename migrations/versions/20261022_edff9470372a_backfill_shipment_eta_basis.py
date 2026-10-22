"""backfill shipment eta basis

Revision ID: edff9470372a
Revises: d8c901559580
Create Date: 2026-10-22 18:49:56.715368

"""

from collections.abc import Sequence

from alembic import op

revision: str = "edff9470372a"
down_revision: str | Sequence[str] | None = "d8c901559580"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Decisions stored before ETAs learned from delivery history have no eta_basis on their
    # shipments. Those ETAs came from distance alone, so record exactly that.
    op.execute(
        """
        UPDATE orders
        SET decision = jsonb_set(
            decision,
            '{shipments}',
            (
                SELECT jsonb_agg(
                    CASE WHEN s ? 'eta_basis' THEN s
                    ELSE s || jsonb_build_object('eta_basis', 'distance only') END
                    ORDER BY i
                )
                FROM jsonb_array_elements(decision -> 'shipments') WITH ORDINALITY AS t(s, i)
            )
        )
        WHERE EXISTS (
            SELECT 1 FROM jsonb_array_elements(decision -> 'shipments') AS s
            WHERE NOT s ? 'eta_basis'
        )
        """
    )


def downgrade() -> None:
    # The extra key is harmless to the older schema; nothing to undo.
    pass
