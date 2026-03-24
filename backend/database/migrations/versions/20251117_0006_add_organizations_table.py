"""introduce organizations table and relationships

Revision ID: 20251117_0006
Revises: 20251117_0005
Create Date: 2025-11-17 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
import re


# revision identifiers, used by Alembic.
revision = "20251117_0006"
down_revision = "20251117_0005"
branch_labels = None
depends_on = None


def _normalized_slug(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
    return cleaned or "org"


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _column_exists(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _unique_exists(inspector, table_name: str, constraint_name: str) -> bool:
    return constraint_name in {uc["name"] for uc in inspector.get_unique_constraints(table_name)}


def _fk_exists(inspector, table_name: str, constraint_name: str) -> bool:
    return constraint_name in {fk["name"] for fk in inspector.get_foreign_keys(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _table_exists(inspector, "organizations"):
        op.create_table(
            "organizations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name", name="uq_organizations_name"),
            sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        )
        inspector = sa.inspect(bind)
    else:
        if not _unique_exists(inspector, "organizations", "uq_organizations_name"):
            op.create_unique_constraint("uq_organizations_name", "organizations", ["name"])
            inspector = sa.inspect(bind)
        if not _unique_exists(inspector, "organizations", "uq_organizations_slug"):
            op.create_unique_constraint("uq_organizations_slug", "organizations", ["slug"])
            inspector = sa.inspect(bind)

    if not _column_exists(inspector, "users", "organization_id"):
        op.add_column("users", sa.Column("organization_id", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)
    if "users" in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("users")}
        if "ix_users_organization_id" not in existing_indexes:
            op.create_index("ix_users_organization_id", "users", ["organization_id"], unique=False)
            inspector = sa.inspect(bind)

    if not _column_exists(inspector, "projects", "organization_id"):
        op.add_column("projects", sa.Column("organization_id", sa.Integer(), nullable=True))
        inspector = sa.inspect(bind)
    if "projects" in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("projects")}
        if "ix_projects_organization_id" not in existing_indexes:
            op.create_index("ix_projects_organization_id", "projects", ["organization_id"], unique=False)
            inspector = sa.inspect(bind)

    if _unique_exists(inspector, "projects", "uq_projects_org_key"):
        op.drop_constraint("uq_projects_org_key", "projects", type_="unique")

    inspector = sa.inspect(bind)

    if not _fk_exists(inspector, "users", "fk_users_organization_id"):
        op.create_foreign_key(
            "fk_users_organization_id",
            "users",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )

    inspector = sa.inspect(bind)
    if not _fk_exists(inspector, "projects", "fk_projects_organization_id"):
        op.create_foreign_key(
            "fk_projects_organization_id",
            "projects",
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="CASCADE",
        )

    inspector = sa.inspect(bind)

    def ensure_org(name: str) -> int:
        cleaned = (name or "").strip() or "default"
        slug = _normalized_slug(cleaned)
        result = bind.execute(
            sa.text(
                "INSERT INTO organizations (name, slug, display_name) "
                "VALUES (:name, :slug, :display_name) "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            ),
            {"name": cleaned, "slug": slug, "display_name": cleaned},
        )
        return result.scalar_one()

    if _column_exists(inspector, "users", "organization_id"):
        user_rows = bind.execute(sa.text("SELECT id, organization FROM users"))
        for row in user_rows:
            org_id = ensure_org(row.organization)
            bind.execute(
                sa.text("UPDATE users SET organization_id = :org_id WHERE id = :id"),
                {"org_id": org_id, "id": row.id},
            )

    if _column_exists(inspector, "projects", "organization_id"):
        project_rows = bind.execute(sa.text("SELECT id, organization FROM projects"))
        for row in project_rows:
            org_id = ensure_org(row.organization)
            bind.execute(
                sa.text("UPDATE projects SET organization_id = :org_id WHERE id = :id"),
                {"org_id": org_id, "id": row.id},
            )

    if _column_exists(sa.inspect(bind), "users", "organization_id"):
        op.alter_column("users", "organization_id", nullable=False)
    if _column_exists(sa.inspect(bind), "projects", "organization_id"):
        op.alter_column("projects", "organization_id", nullable=False)

    if not _unique_exists(sa.inspect(bind), "projects", "uq_projects_org_key"):
        op.create_unique_constraint("uq_projects_org_key", "projects", ["organization_id", "project_key"])


def downgrade() -> None:
    try:
        op.drop_constraint("uq_projects_org_key", "projects", type_="unique")
    except Exception:
        pass
    try:
        op.create_unique_constraint("uq_projects_org_key", "projects", ["organization", "project_key"])
    except Exception:
        pass

    try:
        op.drop_constraint("fk_projects_organization_id", "projects", type_="foreignkey")
    except Exception:
        pass
    try:
        op.drop_constraint("fk_users_organization_id", "users", type_="foreignkey")
    except Exception:
        pass

    try:
        op.drop_index("ix_projects_organization_id", table_name="projects")
    except Exception:
        pass
    try:
        op.drop_index("ix_users_organization_id", table_name="users")
    except Exception:
        pass

    try:
        op.drop_column("projects", "organization_id")
    except Exception:
        pass
    try:
        op.drop_column("users", "organization_id")
    except Exception:
        pass

    try:
        op.drop_table("organizations")
    except Exception:
        pass
