"""create image_metadata table

Revision ID: 20251117_0005
Revises: 20251114_0004
Create Date: 2025-11-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251117_0005"
down_revision = "20251114_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "image_metadata" not in inspector.get_table_names():
        op.create_table(
            "image_metadata",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("page_name", sa.String(length=255), nullable=False),
            sa.Column("image_name", sa.String(length=255), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "image_name", name="uq_image_metadata_project_image"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("image_metadata")}
    if "ix_image_metadata_project_id" not in existing_indexes:
        op.create_index("ix_image_metadata_project_id", "image_metadata", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_image_metadata_project_id", table_name="image_metadata")
    op.drop_table("image_metadata")
