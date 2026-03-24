"""create project_allure_charts table

Revision ID: 20251201_0008
Revises: 20251117_0007
Create Date: 2025-12-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251201_0008"
down_revision = "20251117_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "project_allure_charts" not in inspector.get_table_names():
        op.create_table(
            "project_allure_charts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("chart_key", sa.String(length=255), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=True),
            sa.Column("asset_type", sa.String(length=50), nullable=False),
            sa.Column("relative_path", sa.String(length=1024), nullable=False),
            sa.Column("media_type", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        inspector = sa.inspect(bind)

    unique_constraints = {uc["name"] for uc in inspector.get_unique_constraints("project_allure_charts")}
    if "uq_project_allure_charts_project_key_type" not in unique_constraints:
        op.create_unique_constraint(
            "uq_project_allure_charts_project_key_type",
            "project_allure_charts",
            ["project_id", "chart_key", "asset_type"],
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("project_allure_charts")}
    if "ix_project_allure_charts_project_id" not in existing_indexes:
        op.create_index(
            "ix_project_allure_charts_project_id",
            "project_allure_charts",
            ["project_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_project_allure_charts_project_id", table_name="project_allure_charts")
    op.drop_constraint(
        "uq_project_allure_charts_project_key_type",
        "project_allure_charts",
        type_="unique",
    )
    op.drop_table("project_allure_charts")
