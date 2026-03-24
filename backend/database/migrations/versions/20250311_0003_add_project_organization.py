"""add organization to projects

Revision ID: 20250311_0003
Revises: 20250311_0002
Create Date: 2025-03-11 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20250311_0003"
down_revision = "20250131_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "projects" not in inspector.get_table_names():
        return

    columns = {col["name"] for col in inspector.get_columns("projects")}
    if "created_by" not in columns:
        op.add_column("projects", sa.Column("created_by", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)
        # Backfill with the earliest user id when available.
        if "users" in inspector.get_table_names():
            bind.execute(
                sa.text(
                    """
                    UPDATE projects
                    SET created_by = (SELECT MIN(id) FROM users)
                    WHERE created_by IS NULL
                    """
                )
            )
        columns = {col["name"] for col in inspector.get_columns("projects")}
        if "created_by" in columns:
            try:
                op.create_foreign_key(
                    "fk_projects_created_by",
                    "projects",
                    "users",
                    ["created_by"],
                    ["id"],
                    ondelete="CASCADE",
                )
            except Exception:
                pass
            try:
                op.create_index("ix_projects_created_by", "projects", ["created_by"], unique=False)
            except Exception:
                pass

    if "organization" not in columns:
        op.add_column("projects", sa.Column("organization", sa.String(length=255), nullable=True))
        op.execute("UPDATE projects SET organization = 'default'")
        op.alter_column("projects", "organization", nullable=False)
        inspector = sa.inspect(bind)

    unique_constraints = {uc["name"] for uc in inspector.get_unique_constraints("projects")}
    if "uq_projects_project_key" in unique_constraints:
        op.drop_constraint("uq_projects_project_key", "projects", type_="unique")
        inspector = sa.inspect(bind)

    unique_constraints = {uc["name"] for uc in inspector.get_unique_constraints("projects")}
    if "uq_projects_org_key" not in unique_constraints:
        op.create_unique_constraint("uq_projects_org_key", "projects", ["organization", "project_key"])
        inspector = sa.inspect(bind)

    indexes = {idx["name"] for idx in inspector.get_indexes("projects")}
    if "ix_projects_organization" not in indexes:
        op.create_index("ix_projects_organization", "projects", ["organization"], unique=False)


def downgrade() -> None:
    try:
        op.drop_index("ix_projects_organization", table_name="projects")
    except Exception:
        pass
    try:
        op.drop_constraint("uq_projects_org_key", "projects", type_="unique")
    except Exception:
        pass
    try:
        op.create_unique_constraint("uq_projects_project_key", "projects", ["project_key"])
    except Exception:
        pass
    try:
        op.drop_index("ix_projects_created_by", table_name="projects")
    except Exception:
        pass
    try:
        op.drop_constraint("fk_projects_created_by", "projects", type_="foreignkey")
    except Exception:
        pass
    try:
        op.drop_column("projects", "created_by")
    except Exception:
        pass
    op.drop_column("projects", "organization")
