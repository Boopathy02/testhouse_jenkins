"""create projects table

Revision ID: 20250131_0001
Revises: 
Create Date: 2025-01-31 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20250131_0002"
down_revision = "20250121_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "projects" not in inspector.get_table_names():
        op.create_table(
            "projects",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("project_name", sa.String(length=255), nullable=False),
            sa.Column("project_key", sa.String(length=255), nullable=False),
            sa.Column("framework", sa.String(length=100), nullable=False),
            sa.Column("language", sa.String(length=100), nullable=False),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                server_onupdate=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.UniqueConstraint("project_key", name="uq_projects_project_key"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("projects")}
    if "ix_projects_id" not in existing_indexes:
        op.create_index("ix_projects_id", "projects", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_projects_id", table_name="projects")
    op.drop_table("projects")
