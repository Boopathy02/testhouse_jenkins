"""create test_case_metadata table

Revision ID: 20251218_0010
Revises: 20251201_0009
Create Date: 2025-12-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251218_0010"
down_revision = "20251201_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "test_case_metadata" not in inspector.get_table_names():
        op.create_table(
            "test_case_metadata",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("test_name", sa.String(length=1024), nullable=False),
            sa.Column("markers", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "project_id",
                "test_name",
                name="uq_test_case_metadata_project_test",
            ),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("test_case_metadata")}
    if "ix_test_case_metadata_project_id" not in existing_indexes:
        op.create_index(
            "ix_test_case_metadata_project_id",
            "test_case_metadata",
            ["project_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_test_case_metadata_project_id", table_name="test_case_metadata")
    op.drop_table("test_case_metadata")
