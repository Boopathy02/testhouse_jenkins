"""create versions table

Revision ID: 20260212_0013
Revises: 20260130_0012
Create Date: 2026-02-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260212_0013"
down_revision = "20260130_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "versions" in inspector.get_table_names():
        return
    op.create_table(
        "versions",
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("batch_id", sa.Text(), nullable=True),
        sa.Column("load_date", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("versions")
