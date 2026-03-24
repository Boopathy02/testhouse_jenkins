"""create api_specs table

Revision ID: 20260130_0012
Revises: 20251219_0011
Create Date: 2026-01-30 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260130_0012"
down_revision = "20251219_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "api_specs" not in inspector.get_table_names():
        op.create_table(
            "api_specs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("project_id", sa.Integer(), nullable=False),
            sa.Column("key", sa.String(length=255), nullable=False),
            sa.Column("service_name", sa.String(length=255), nullable=False),
            sa.Column("operation_name", sa.String(length=255), nullable=False),
            sa.Column("http_method", sa.String(length=20), nullable=False),
            sa.Column("path", sa.String(length=1024), nullable=False),
            sa.Column("base_url", sa.String(length=1024), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("default_headers", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("default_query", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("request_schema", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("response_schema", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("examples", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("raw_definition", sa.JSON(), nullable=False, server_default="{}"),
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
            sa.UniqueConstraint("project_id", "key", name="uq_api_specs_project_key"),
        )
        inspector = sa.inspect(bind)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes("api_specs")}
    if "ix_api_specs_project_id" not in existing_indexes:
        op.create_index(
            "ix_api_specs_project_id",
            "api_specs",
            ["project_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index("ix_api_specs_project_id", table_name="api_specs")
    op.drop_table("api_specs")
