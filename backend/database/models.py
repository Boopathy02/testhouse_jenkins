from datetime import datetime
import re
from typing import Optional

from sqlalchemy import Column, DateTime, Float, Integer, String, UniqueConstraint, func, ForeignKey, Text, Index, JSON
from sqlalchemy.orm import validates, Session
from sqlalchemy.exc import OperationalError, ProgrammingError

try:
    from psycopg.errors import UndefinedTable as PsycopgUndefinedTable
except Exception:  # pragma: no cover - optional import guard
    PsycopgUndefinedTable = None

from .session import Base


def _normalize_project_name(name: str) -> str:
    """Normalize project names for uniqueness checks."""
    return re.sub(r"\s+", " ", name or "").strip().lower()


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        UniqueConstraint("name", name="uq_organizations_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    slug = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @staticmethod
    def normalized_slug(name: str) -> str:
        cleaned = re.sub(r"\s+", " ", name or "").strip().lower()
        cleaned = re.sub(r"[^a-z0-9_-]+", "-", cleaned)
        return cleaned or "org"

    @classmethod
    def get_or_create(cls, db: Session, name: str) -> "Organization":
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("Organization name is required")
        slug = cls.normalized_slug(cleaned)
        try:
            existing = (
                db.query(cls)
                .filter(cls.slug == slug)
                .first()
            )
            if existing:
                return existing
            org = cls(name=cleaned, slug=slug, display_name=cleaned)
            db.add(org)
            db.flush()
            return org
        except (ProgrammingError, OperationalError) as exc:
            message = str(exc).lower()
            if PsycopgUndefinedTable and isinstance(getattr(exc, "orig", None), PsycopgUndefinedTable):
                raise RuntimeError("Database schema out of sync. Run Alembic migrations.") from exc
            if "relation" in message and "does not exist" in message:
                raise RuntimeError("Database schema out of sync. Run Alembic migrations.") from exc
            raise

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "display_name": self.display_name,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("organization_id", "project_key", name="uq_projects_org_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization = Column(String(255), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_name = Column(String(255), nullable=False)
    project_key = Column(String(255), nullable=False)
    framework = Column(String(100), nullable=False)
    language = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @validates("project_name")
    def _validate_project_name(self, key, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("project_name is required")
        normalized = _normalize_project_name(value)
        self.project_key = normalized
        return value.strip()

    @validates("organization")
    def _validate_organization(self, key, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("organization is required")
        return cleaned

    @validates("framework")
    def _validate_framework(self, key, value: str) -> str:
        return (value or "").strip()

    @validates("language")
    def _validate_language(self, key, value: str) -> str:
        return (value or "").strip()

    @classmethod
    def normalized_key(cls, name: Optional[str]) -> str:
        return _normalize_project_name(name or "")

    @property
    def slug(self) -> str:
        return self.project_key.replace(" ", "_")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "organization": self.organization,
            "organization_id": self.organization_id,
            "project_name": self.project_name,
            "framework": self.framework,
            "language": self.language,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class ApiSpec(Base):
    __tablename__ = "api_specs"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_api_specs_project_key"),
        Index("ix_api_specs_project_id", "project_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    key = Column(String(255), nullable=False)
    service_name = Column(String(255), nullable=False)
    operation_name = Column(String(255), nullable=False)
    http_method = Column(String(20), nullable=False)
    path = Column(String(1024), nullable=False)
    base_url = Column(String(1024), nullable=False)
    description = Column(Text, nullable=True)
    default_headers = Column(JSON, nullable=False, default=dict)
    default_query = Column(JSON, nullable=False, default=dict)
    request_schema = Column(JSON, nullable=False, default=dict)
    response_schema = Column(JSON, nullable=False, default=dict)
    examples = Column(JSON, nullable=False, default=dict)
    raw_definition = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "key": self.key,
            "service_name": self.service_name,
            "operation_name": self.operation_name,
            "http_method": self.http_method,
            "path": self.path,
            "base_url": self.base_url,
            "description": self.description,
            "default_headers": self.default_headers,
            "default_query": self.default_query,
            "request_schema": self.request_schema,
            "response_schema": self.response_schema,
            "examples": self.examples,
            "raw_definition": self.raw_definition,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class ProjectFile(Base):
    __tablename__ = "project_files"
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_project_files_project_path"),
        Index("ix_project_files_project_id", "project_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    path = Column(String(1024), nullable=False)
    encoding = Column(String(50), nullable=False, default="utf-8")
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @validates("path")
    def _validate_path(self, key, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("path is required")
        return cleaned

    @validates("encoding")
    def _validate_encoding(self, key, value: str) -> str:
        cleaned = (value or "utf-8").strip()
        return cleaned or "utf-8"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "path": self.path,
            "encoding": self.encoding,
            "content": self.content,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class ImageMetadata(Base):
    __tablename__ = "image_metadata"
    __table_args__ = (
        UniqueConstraint("project_id", "image_name", name="uq_image_metadata_project_image"),
        Index("ix_image_metadata_project_id", "project_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    page_name = Column(String(255), nullable=False)
    image_name = Column(String(255), nullable=False)
    metadata_json = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "page_name": self.page_name,
            "image_name": self.image_name,
            "metadata": self.metadata_json,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class ImageUploadRun(Base):
    __tablename__ = "image_upload_runs"
    __table_args__ = (
        Index("ix_image_upload_runs_project_id", "project_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    results = Column(JSON, nullable=False, default=list)
    image_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "results": self.results,
            "image_count": self.image_count,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class ProjectAllureChart(Base):
    __tablename__ = "project_allure_charts"
    __table_args__ = (
        UniqueConstraint("project_id", "chart_key", "asset_type", name="uq_project_allure_charts_project_key_type"),
        Index("ix_project_allure_charts_project_id", "project_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    chart_key = Column(String(255), nullable=False)
    label = Column(String(255), nullable=True)
    asset_type = Column(String(50), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    media_type = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "chart_key": self.chart_key,
            "label": self.label,
            "asset_type": self.asset_type,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
        "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class ProjectAllureResult(Base):
    __tablename__ = "project_allure_results"
    __table_args__ = (
        Index("ix_project_allure_results_project_id", "project_id"),
        Index("ix_project_allure_results_run_id", "run_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    run_id = Column(String(64), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    relative_path = Column(String(1024), nullable=False)
    test_name = Column(String(255), nullable=True)
    status = Column(String(50), nullable=True)
    duration = Column(Float, nullable=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "file_name": self.file_name,
            "relative_path": self.relative_path,
            "test_name": self.test_name,
            "status": self.status,
            "duration": self.duration,
            "payload": self.payload,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization = Column(String(255), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @validates("organization")
    def _validate_organization(self, key, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("organization is required")
        return cleaned

    @validates("email")
    def _validate_email(self, key, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if not cleaned:
            raise ValueError("email is required")
        return cleaned

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "organization": self.organization,
            "organization_id": self.organization_id,
            "email": self.email,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }

class TestCaseMetadata(Base):
    __tablename__ = "test_case_metadata"
    __table_args__ = (
        UniqueConstraint("project_id", "test_name", name="uq_test_case_metadata_project_test"),
        Index("ix_test_case_metadata_project_id", "project_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    test_name = Column(String(1024), nullable=False)
    markers = Column(JSON, nullable=False, default=list)
    tags = Column(JSON, nullable=False, default=list)
    priority = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "test_name": self.test_name,
            "markers": self.markers,
            "tags": self.tags,
            "priority": self.priority,
            "created_at": self.created_at.isoformat() if isinstance(self.created_at, datetime) else self.created_at,
            "updated_at": self.updated_at.isoformat() if isinstance(self.updated_at, datetime) else self.updated_at,
        }
