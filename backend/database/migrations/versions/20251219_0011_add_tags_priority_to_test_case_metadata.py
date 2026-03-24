"""add tags and priority to test_case_metadata

Revision ID: 20251219_0011
Revises: 20251218_0010
Create Date: 2025-12-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251219_0011"
down_revision = "20251218_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col["name"] for col in inspector.get_columns("test_case_metadata")}
    if "tags" not in columns:
        op.add_column(
            "test_case_metadata",
            sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "priority" not in columns:
        op.add_column(
            "test_case_metadata",
            sa.Column("priority", sa.String(length=50), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("test_case_metadata", "priority")
    op.drop_column("test_case_metadata", "tags")
