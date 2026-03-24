"""create project_allure_results table

Revision ID: 20251201_0009
Revises: 20251201_0008
Create Date: 2025-12-01 00:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251201_0009"
down_revision = "20251201_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "project_allure_results" not in inspector.get_table_names():
        op.create_table(
            "project_allure_results",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("file_name", sa.String(length=255), nullable=False),
            sa.Column("relative_path", sa.String(length=1024), nullable=False),
            sa.Column("test_name", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=50), nullable=True),
            sa.Column("duration", sa.Float(), nullable=True),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("project_allure_results")}
    if "ix_project_allure_results_project_id" not in existing_indexes:
        op.create_index(
            "ix_project_allure_results_project_id",
            "project_allure_results",
            ["project_id"],
            unique=False,
        )
    if "ix_project_allure_results_run_id" not in existing_indexes:
        op.create_index(
            "ix_project_allure_results_run_id",
            "project_allure_results",
            ["run_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_project_allure_results_run_id", table_name="project_allure_results")
    op.drop_index("ix_project_allure_results_project_id", table_name="project_allure_results")
    op.drop_table("project_allure_results")
