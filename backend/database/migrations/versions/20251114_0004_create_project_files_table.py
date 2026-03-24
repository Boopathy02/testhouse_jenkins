"""create project_files table

Revision ID: 20251114_0004
Revises: 20250311_0003
Create Date: 2025-11-14 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251114_0004"
down_revision = "20250311_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "project_files" not in inspector.get_table_names():
        op.create_table(
            "project_files",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("path", sa.String(length=1024), nullable=False),
            sa.Column("encoding", sa.String(length=50), nullable=False, server_default="utf-8"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "path", name="uq_project_files_project_path"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("project_files")}
    if "ix_project_files_project_id" not in existing_indexes:
        op.create_index("ix_project_files_project_id", "project_files", ["project_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_project_files_project_id", table_name="project_files")
    op.drop_table("project_files")
