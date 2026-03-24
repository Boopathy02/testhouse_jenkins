import io
import os
import re
import zipfile
from pathlib import Path
from typing import Optional
import shutil

from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
from database.models import Project, User, ProjectFile
from database.session import get_db
from database.project_storage import DatabaseBackedProjectStorage
from git_service import push_generated_project

from utils.chroma_client import reset_chroma_client

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


class TokenPayload(BaseModel):
    sub: EmailStr
    uid: int
    org: str
    org_id: Optional[int] = None
    exp: Optional[int] = None


def _org_slug(name: str) -> str:
    normalized = (name or "").strip().lower()
    return re.sub(r"[^a-z0-9_-]+", "-", normalized) or "default"


def _project_dir_segment(project: Project) -> str:
    """Return a filesystem-safe folder segment for the given project."""
    # Prefer the canonical slug if available; fall back to normalized key.
    base_slug = (project.slug or Project.normalized_key(project.project_name)).strip()
    base_slug = re.sub(r"[^a-z0-9_-]+", "-", base_slug.lower()) or "project"

    if project.id:
        return f"{project.id}-{base_slug}"
    return base_slug


def _project_root(project: Project) -> Path:
    backend_root = Path(__file__).resolve().parents[1]
    org_segment = _org_slug(project.organization)

    org_root = backend_root / "organizations" / org_segment
    desired = org_root / _project_dir_segment(project)

    # Backwards compatibility: projects created before this change stored data
    # under a plain folder named after the project. If that legacy folder still
    # exists and the new structure has not yet been created, migrate it so we
    # do not blend multiple projects together.
    legacy = org_root / project.project_name.strip()
    if legacy.exists() and not desired.exists():
        desired.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy.rename(desired)
        except Exception:
            # If the rename fails (e.g. permissions), keep using the legacy path.
            return legacy

    return desired


def _project_source_root(project: Project) -> Path:
    """Return the base directory that holds generated source artifacts."""
    return _project_root(project) / "generated_runs" / "src"


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        token_data = TokenPayload(**payload)
    except (JWTError, ValidationError) as exc:
        raise credentials_exception from exc

    user = (
        db.query(User)
        .filter(User.id == token_data.uid, User.email == str(token_data.sub).lower())
        .first()
    )
    if not user:
        raise credentials_exception

    if (user.organization or "").strip().lower() != (token_data.org or "").strip().lower():
        raise credentials_exception
    if token_data.org_id is not None and user.organization_id != token_data.org_id:
        raise credentials_exception

    return user


class ProjectDetails(BaseModel):
    project_name: str
    framework: str
    language: str


class ProjectActivateRequest(BaseModel):
    project_name: str


class ProjectFileUpdateRequest(BaseModel):
    path: str
    content: str
    encoding: Optional[str] = "utf-8"


class GitPushRequest(BaseModel):
    repo_url: str
    base_branch: str
    target_branch: str
    git_username: str
    git_token_env: str
    commit_message: str
    author_name: str
    author_email: str


def _unique_project_name(db: Session, org_id: int, base_name: str) -> str:
    cleaned = (base_name or "").strip()
    if not cleaned:
        return "project"
    base_key = Project.normalized_key(cleaned)
    existing_keys = {
        row[0]
        for row in (
            db.query(Project.project_key)
            .filter(Project.organization_id == org_id)
            .all()
        )
    }
    if base_key not in existing_keys:
        return cleaned
    suffix = 2
    while True:
        candidate = f"{cleaned} {suffix}"
        candidate_key = Project.normalized_key(candidate)
        if candidate_key not in existing_keys:
            return candidate
        suffix += 1


def _ensure_project_structure(project: Project) -> dict:
    project_root = _project_root(project)
    data_dir = project_root / "data"
    runs_dir = project_root / "generated_runs"
    runs_src = runs_dir / "src"

    data_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_src / "metadata").mkdir(parents=True, exist_ok=True)
    (runs_src / "ocr-dom-metadata").mkdir(parents=True, exist_ok=True)
    (runs_src / "pages").mkdir(parents=True, exist_ok=True)
    (runs_src / "tests").mkdir(parents=True, exist_ok=True)
    
    prompts_dir = runs_src / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)

    existing_prompts = [p for p in prompts_dir.iterdir() if p.is_file() and p.suffix == ".txt"]
    if not existing_prompts:
        # Copy global prompts when starting a new project.
        global_prompts_dir = Path(__file__).resolve().parents[1] / "prompts"
        if global_prompts_dir.exists():
            for item in global_prompts_dir.iterdir():
                if item.is_file():
                    dest = prompts_dir / item.name
                    if dest.exists():
                        continue
                    try:
                        shutil.copy(item, dest)
                    except Exception as e:
                        print(f"ERROR: Failed to copy prompt '{item.name}' to '{prompts_dir}': {e}")
                else:
                    print(f"DEBUG: Skipping non-file item: {item.name}")
        else:
            print(f"ERROR: Global prompts directory does not exist: {global_prompts_dir}")

    return {
        "project_root": str(project_root.resolve()),
        "data_dir": str(data_dir.resolve()),
        "generated_runs": str(runs_dir.resolve()),
        "src_dir": str(runs_src.resolve()),
        "chroma_path": str((data_dir / "chroma_db").resolve()),
    }


def _persist_project_prompts(project: Project, project_paths: dict, db: Session) -> None:
    src_dir = Path(project_paths["src_dir"])
    prompts_dir = src_dir / "prompts"
    if not prompts_dir.exists() or not prompts_dir.is_dir():
        return
    storage = DatabaseBackedProjectStorage(project, src_dir, db)
    for item in prompts_dir.iterdir():
        if not item.is_file() or item.suffix != ".txt":
            continue
        try:
            content = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = item.read_text(encoding="utf-8", errors="replace")
        storage.write_file(f"prompts/{item.name}", content, "utf-8")


def _ensure_prompt_records(project: Project, project_paths: dict, db: Session) -> None:
    try:
        existing = (
            db.query(ProjectFile.path)
            .filter(
                ProjectFile.project_id == project.id,
                ProjectFile.path.like("prompts/%"),
            )
            .first()
        )
    except Exception:
        existing = None
    if existing is None:
        _persist_project_prompts(project, project_paths, db)


def _activate_env(project_paths: dict, project: Optional[Project] = None) -> None:
    os.environ["SMARTAI_PROJECT_DIR"] = project_paths["project_root"]
    os.environ["SMARTAI_SRC_DIR"] = project_paths["src_dir"]
    os.environ["SMARTAI_CHROMA_PATH"] = project_paths["chroma_path"]
    if project and project.id:
        os.environ["SMARTAI_PROJECT_ID"] = str(project.id)
    reset_chroma_client()


def _clear_env_if_active(project_root: Path) -> None:
    """Unset SMARTAI_* env vars if they point at the deleted project."""
    resolved = str(project_root.resolve())
    if os.environ.get("SMARTAI_PROJECT_DIR") == resolved:
        for key in ("SMARTAI_PROJECT_DIR", "SMARTAI_SRC_DIR", "SMARTAI_CHROMA_PATH", "SMARTAI_PROJECT_ID"):
            os.environ.pop(key, None)


def _resolve_project_path(base: Path, relative: str) -> Path:
    """Resolve a user-provided path safely within the project boundary."""
    relative_path = (Path(relative or ".")).as_posix().lstrip("/")
    target = (base / relative_path).resolve(strict=False)

    if target == base:
        return target

    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    return target


@router.post("/projects/save-details")
def save_project_details(
    details: ProjectDetails,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_org = current_user.organization.strip()
    user_org_id = current_user.organization_id
    project_name = details.project_name.strip()
    try:
        project = Project(
            organization=user_org,
            organization_id=user_org_id,
            project_name=project_name,
            framework=details.framework.strip(),
            language=details.language.strip(),
            created_by=current_user.id
        )
        db.add(project)
        db.flush()
    except IntegrityError:
        db.rollback()
        # Auto-resolve name collisions within the same organization.
        project_name = _unique_project_name(db, user_org_id, project_name)
        try:
            project = Project(
                organization=user_org,
                organization_id=user_org_id,
                project_name=project_name,
                framework=details.framework.strip(),
                language=details.language.strip(),
                created_by=current_user.id
            )
            db.add(project)
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"Project '{details.project_name.strip()}' already exists",
            )
    except ValueError as ve:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(ve))
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc))

    project_paths = {}
    try:
        project_paths = _ensure_project_structure(project)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to prepare project directories: {exc}") from exc
    try:
        _persist_project_prompts(project, project_paths, db)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to persist prompts: {exc}") from exc

    try:
        _activate_env(project_paths, project)
    except Exception:
        # Environment activation is best-effort; failures shouldn't prevent API success.
        pass

    payload = {
        "status": "success",
        "project": project.to_dict(),
        **project_paths,
    }

    return payload


@router.get("/projects")
def list_projects(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    projects = (
        db.query(Project)
        .filter(Project.organization_id == org_id)
        .order_by(Project.created_at.desc())
        .all()
    )
    return {"projects": [p.to_dict() for p in projects]}


@router.post("/projects/activate")
def activate_project(
    req: ProjectActivateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user_org = current_user.organization.strip()
    org_id = current_user.organization_id
    name = (req.project_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="project_name is required")

    project_key = Project.normalized_key(name)
    project = (
        db.query(Project)
        .filter(
            Project.project_key == project_key,
            Project.organization_id == org_id,
        )
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    try:
        project_paths = _ensure_project_structure(project)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to prepare project directories: {exc}",
        ) from exc
    try:
        _persist_project_prompts(project, project_paths, db)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist prompts: {exc}",
        ) from exc
    try:
        _activate_env(project_paths, project)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to activate project: {exc}") from exc

    return {
        "status": "activated",
        "project": project.to_dict(),
        **project_paths,
    }


@router.get("/projects/{project_id}")
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.organization_id == org_id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project with id '{project_id}' not found")

    try:
        project_paths = _ensure_project_structure(project)
    except Exception:
        project_paths = {}

    return {
        "project": project.to_dict(),
        "paths": project_paths,
    }


@router.get("/projects/{project_id}/prompts")
def list_project_prompts(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = _get_project_for_user(project_id, db, org_id)
    project_paths = _ensure_project_structure(project)
    _ensure_prompt_records(project, project_paths, db)

    try:
        rows = (
            db.query(ProjectFile.path)
            .filter(
                ProjectFile.project_id == project.id,
                ProjectFile.path.like("prompts/%"),
            )
            .all()
        )
    except Exception as exc:
        print(f"ERROR in list_project_prompts: Failed to read prompt records: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to read prompt records: {exc}")

    prompt_files = sorted(
        {
            Path(path).name
            for (path,) in rows
            if path and path.endswith(".txt")
        }
    )
    return {"prompts": prompt_files}


def _get_project_for_user(project_id: int, db: Session, org_id: int) -> Project:
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.organization_id == org_id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project with id '{project_id}' not found")
    return project


def _extract_test_functions(file_path: Path) -> list[dict]:
    names = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return names
    pattern = re.compile(r"^\s*(?:async\s+def|def)\s+((?:test_|TS_)[A-Za-z0-9_]+)\s*\(", re.MULTILINE)
    for match in pattern.finditer(content):
        names.append({"name": match.group(1)})
    if not names:
        names.append({"name": file_path.stem})
    return names


@router.get("/projects/{project_id}/testcases")
def list_project_testcases(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = _get_project_for_user(project_id, db, org_id)
    project_paths = _ensure_project_structure(project)
    src_dir = Path(project_paths["src_dir"])

    def _collect_tests(tests_root: Path) -> tuple[list[dict], dict]:
        if not tests_root.exists():
            return [], {}
        categories = {
            "ui": tests_root / "ui_scripts",
            "security": tests_root / "security_tests",
            "accessibility": tests_root / "accessibility_tests",
            "root": tests_root,
        }
        category_payload = {}
        flat_tests = []
        for category, base_dir in categories.items():
            if not base_dir.exists():
                continue
            tests = []
            for path in sorted(base_dir.rglob("*.py")):
                test_defs = _extract_test_functions(path)
                rel_path = ""
                try:
                    rel_path = str(path.relative_to(tests_root))
                except Exception:
                    rel_path = str(path)
                for entry in test_defs:
                    tests.append({"name": entry["name"], "path": rel_path})
            if tests:
                category_payload[category] = {
                    "count": len(tests),
                    "tests": tests,
                }
                flat_tests.extend(tests)
        return flat_tests, category_payload

    tests_root = src_dir / "tests"
    flat_tests, category_payload = _collect_tests(tests_root)

    if not flat_tests:
        org_root = Path(__file__).resolve().parents[1] / "organizations" / _org_slug(project.organization)
        legacy_root = org_root / project.project_name.strip() / "generated_runs" / "src"
        legacy_tests = legacy_root / "tests"
        flat_tests, category_payload = _collect_tests(legacy_tests)

    return {
        "project_id": project_id,
        "total": len(flat_tests),
        "categories": category_payload,
        "tests": flat_tests,
    }


@router.get("/projects/{project_id}/files")
def list_project_files(
    project_id: int,
    path: str = Query("", description="Relative path within the project's generated source tree."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = _get_project_for_user(project_id, db, org_id)

    try:
        project_paths = _ensure_project_structure(project)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to prepare project directories: {exc}") from exc

    base_dir = Path(project_paths["src_dir"])
    normalized_path = (path or "").strip().strip("/")
    if normalized_path == "prompts":
        _ensure_prompt_records(project, project_paths, db)
        try:
            rows = (
                db.query(ProjectFile.path)
                .filter(
                    ProjectFile.project_id == project.id,
                    ProjectFile.path.like("prompts/%"),
                )
                .all()
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to read prompt records: {exc}") from exc

        entries = []
        for (row_path,) in rows:
            if not row_path:
                continue
            name = Path(row_path).name
            if not name.endswith(".txt"):
                continue
            entries.append(
                {
                    "name": name,
                    "path": row_path,
                    "type": "file",
                }
            )
        entries.sort(key=lambda item: item["name"].lower())
        return {
            "project_id": project_id,
            "base_path": base_dir.as_posix(),
            "path": "prompts",
            "entries": entries,
        }

    target = _resolve_project_path(base_dir, path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            rel_path = child.relative_to(base_dir).as_posix()
            entry_type = "file" if child.is_file() else "directory"
            entries.append(
                {
                    "name": child.name,
                    "path": rel_path,
                    "type": entry_type,
                }
            )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Access denied for requested path") from exc

    return {
        "project_id": project_id,
        "base_path": base_dir.as_posix(),
        "path": target.relative_to(base_dir).as_posix() if target != base_dir else "",
        "entries": entries,
    }


@router.get("/projects/{project_id}/files/content")
def get_project_file_content(
    project_id: int,
    path: str = Query(..., min_length=1, description="Relative file path within the project."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = _get_project_for_user(project_id, db, org_id)

    try:
        project_paths = _ensure_project_structure(project)
    except Exception as exc:
        print(f"ERROR in get_project_file_content: Failed to prepare project directories: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to prepare project directories: {exc}") from exc
    _ensure_prompt_records(project, project_paths, db)

    base_dir = Path(project_paths["src_dir"])
    storage = DatabaseBackedProjectStorage(project, base_dir, db)
    target = _resolve_project_path(base_dir, path)

    relative_path = target.relative_to(base_dir).as_posix()
    record = (
        db.query(ProjectFile)
        .filter(ProjectFile.project_id == project.id, ProjectFile.path == relative_path)
        .first()
    )

    def _read_disk_text(file_path: Path) -> tuple[str, str]:
        try:
            return file_path.read_text(encoding="utf-8"), "utf-8"
        except UnicodeDecodeError:
            return file_path.read_text(encoding="utf-8", errors="replace"), "utf-8"

    if target.exists() and target.is_file():
        if record is None or (record.content or "") == "":
            content, encoding = _read_disk_text(target)
            storage.write_file(relative_path, content, encoding)
            extension = target.suffix.lower().lstrip(".")
            return {
                "project_id": project_id,
                "path": relative_path,
                "encoding": encoding,
                "language": extension or "text",
                "content": content,
                "source": "filesystem",
            }
    elif record is None:
        raise HTTPException(status_code=404, detail="File not found")

    if record is None:
        file_data = storage.read_file(relative_path, target)
        extension = target.suffix.lower().lstrip(".")
        return {
            "project_id": project_id,
            "path": relative_path,
            "encoding": file_data.encoding,
            "language": extension or "text",
            "content": file_data.content,
            "source": file_data.source,
        }

    extension = Path(relative_path).suffix.lower().lstrip(".")
    content = record.content or ""
    encoding = record.encoding or "utf-8"

    return {
        "project_id": project_id,
        "path": relative_path,
        "encoding": encoding,
        "language": extension or "text",
        "content": content,
        "source": "database",
    }


@router.put("/projects/{project_id}/files/content")
def update_project_file_content(
    project_id: int,
    payload: ProjectFileUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = _get_project_for_user(project_id, db, org_id)

    try:
        project_paths = _ensure_project_structure(project)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to prepare project directories: {exc}") from exc

    base_dir = Path(project_paths["src_dir"])
    storage = DatabaseBackedProjectStorage(project, base_dir, db)
    relative_path = (payload.path or "").strip()
    if not relative_path:
        raise HTTPException(status_code=400, detail="Path is required")
    target = _resolve_project_path(base_dir, relative_path)

    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="Cannot overwrite a directory")

    encoding = (payload.encoding or "utf-8").lower().strip() or "utf-8"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to prepare directories: {exc}") from exc

    try:
        storage.write_file(target.relative_to(base_dir).as_posix(), payload.content or "", encoding)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to persist file in database: {exc}") from exc

    try:
        target.write_text(payload.content or "", encoding=encoding)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=f"Unsupported encoding '{encoding}'") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {exc}") from exc

    stat = target.stat()
    extension = target.suffix.lower().lstrip(".")

    return {
        "status": "saved",
        "project_id": project_id,
        "path": target.relative_to(base_dir).as_posix(),
        "encoding": encoding,
        "language": extension or "text",
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.organization_id == org_id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project with id '{project_id}' not found")

    project_root = _project_root(project)

    try:
        db.delete(project)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete project: {exc}")

    # Best-effort cleanup of environment variables if the deleted project was active
    try:
        _clear_env_if_active(project_root)
    except Exception:
        pass

    return {"status": "deleted", "project_id": project_id}


@router.get("/projects/{project_id}/download")
def download_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.organization_id == org_id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project with id '{project_id}' not found")

    project_root = _project_root(project).resolve()
    if not project_root.exists() or not project_root.is_dir():
        raise HTTPException(status_code=404, detail=f"Project directory for '{project.project_name}' not found")

    buffer = io.BytesIO()
    base_prefix = Path(project.slug or project.project_name.strip() or f"project_{project_id}")

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file_path in project_root.rglob("*"):
            if file_path.is_file():
                arcname = base_prefix / file_path.relative_to(project_root)
                zipf.write(str(file_path), arcname=str(arcname))

    buffer.seek(0)
    filename = f"{base_prefix}.zip"

    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@router.post("/projects/{project_id}/git/push")
def push_project_to_git(
    project_id: int,
    payload: GitPushRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    org_id = current_user.organization_id
    project = _get_project_for_user(project_id, db, org_id)
    project_root = _project_root(project).resolve()
    generated_runs_path = project_root / "generated_runs"

    config = {
        "repo_url": payload.repo_url,
        "base_branch": payload.base_branch,
        "target_branch": payload.target_branch,
        "git_username": payload.git_username,
        "git_token_env": payload.git_token_env,
        "commit_message": payload.commit_message,
        "author_name": payload.author_name,
        "author_email": payload.author_email,
        "generated_runs_path": str(generated_runs_path),
    }

    result = push_generated_project(config)
    if result.get("status") != "success":
        raise HTTPException(status_code=500, detail=result.get("error_message") or "Git push failed.")
    return result


def get_user_project(
    db: Session,
    project_id: int,
    current_user: User,
) -> Project:
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.organization_id == current_user.organization_id,
            Project.created_by == current_user.id,   
        )
        .first()
    )

    if not project:
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this project"
        )

    return project

def get_project_by_projectId(
    db: Session,
    project_id: int,
) -> Project:
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
        )
        .first()
    )

    if not project:
        raise HTTPException(
            status_code=403,
            detail="You do not have access to this project"
        )

    return project

