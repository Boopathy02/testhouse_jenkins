from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import Project, ProjectFile


@dataclass
class ProjectFileData:
    path: str
    content: str
    encoding: str
    source: str


class DatabaseBackedProjectStorage:
    """Persist project file contents in the database while keeping disk in sync."""

    def __init__(self, project: Project, base_dir: Path, db: Session) -> None:
        self.project = project
        self.base_dir = base_dir
        self.db = db

    def _normalize_path(self, relative_path: str) -> str:
        relative = (relative_path or "").strip()
        if not relative:
            return ""
        return Path(relative).as_posix().lstrip("/")

    def _get_record(self, normalized_path: str) -> Optional[ProjectFile]:
        if not normalized_path:
            return None
        try:
            self.db.flush()
        except Exception:
            pass
        return (
            self.db.query(ProjectFile)
            .filter(ProjectFile.project_id == self.project.id, ProjectFile.path == normalized_path)
            .first()
        )

    def _upsert_record(self, normalized_path: str, content: str, encoding: str) -> ProjectFile:
        stmt = self._build_upsert_statement(normalized_path, content, encoding)
        if stmt is not None:
            self.db.execute(stmt)
            self.db.flush()
            # Query again to return a managed ProjectFile instance.
            return self.db.query(ProjectFile).filter(
                ProjectFile.project_id == self.project.id,
                ProjectFile.path == normalized_path
            ).first()
        
        # Fallback for unsupported dialects (should not happen with current _build_upsert_statement)
        record = self._get_record(normalized_path)
        if record:
            record.content = content
            record.encoding = encoding
        else:
            record = ProjectFile(
                project_id=self.project.id,
                path=normalized_path,
                encoding=encoding,
                content=content,
            )
            self.db.add(record)
        self.db.flush()
        return record

    def _build_upsert_statement(
        self,
        normalized_path: str,
        content: str,
        encoding: str,
    ):
        bind = self.db.get_bind()
        if bind is None:
            return None
        values = {
            "project_id": self.project.id,
            "path": normalized_path,
            "encoding": encoding,
            "content": content,
        }
        dialect_name = (bind.dialect.name or "").lower()
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(ProjectFile).values(**values)
            return stmt.on_conflict_do_update(
                constraint="uq_project_files_project_path",
                set_={
                    "content": stmt.excluded.content,
                    "encoding": stmt.excluded.encoding,
                },
            )
        if dialect_name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(ProjectFile).values(**values)
            return stmt.on_conflict_do_update(
                index_elements=["project_id", "path"],
                set_={
                    "content": stmt.excluded.content,
                    "encoding": stmt.excluded.encoding,
                },
            )
        return None

    def _read_disk(self, abs_path: Path) -> Tuple[str, str]:
        try:
            return abs_path.read_text(encoding="utf-8"), "utf-8"
        except UnicodeDecodeError:
            return abs_path.read_text(encoding="utf-8", errors="replace"), "utf-8 (errors replaced)"

    def read_file(self, relative_path: str, absolute_path: Path) -> ProjectFileData:
        normalized = self._normalize_path(relative_path)
        self.db.expire_all() # Ensure fresh data from DB
        record = self._get_record(normalized)
        if record:
            return ProjectFileData(
                path=normalized,
                content=record.content or "",
                encoding=record.encoding or "utf-8",
                source="database",
            )

        content, encoding = self._read_disk(absolute_path)
        self._upsert_record(normalized, content, encoding)
        return ProjectFileData(path=normalized, content=content, encoding=encoding, source="filesystem")

    def write_file(self, relative_path: str, content: str, encoding: str) -> None:
        normalized = self._normalize_path(relative_path)
        self._upsert_record(normalized, content, encoding or "utf-8")
