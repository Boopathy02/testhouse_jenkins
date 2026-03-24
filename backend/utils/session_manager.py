from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


def auth_storage_path(project_root: Path) -> Path:
    target = project_root / "auth" / "storage.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def auth_landing_path(project_root: Path) -> Path:
    target = project_root / "auth" / "landing_url.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def load_storage_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _normalize_domain(domain: str) -> str:
    cleaned = (domain or "").strip().lstrip(".")
    return cleaned.lower()


def _hostname_from_url(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def storage_has_cookie_for_url(state: dict, url: str) -> bool:
    hostname = _hostname_from_url(url)
    if not hostname:
        return False
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not cookies:
        return False
    for cookie in cookies:
        domain = _normalize_domain(str((cookie or {}).get("domain") or ""))
        if not domain:
            continue
        if hostname == domain or hostname.endswith(f".{domain}"):
            return True
    return False


def is_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    return bool(re.search(r"(login|signin|sign-in|auth|sso)", lowered))


async def wait_for_login_and_save(
    page: Any,
    project_root: Path,
    timeout_sec: int = 900,
    interval_sec: float = 1.0,
) -> bool:
    """Poll the page until it looks authenticated, then persist storage_state."""
    storage_path = auth_storage_path(project_root)
    landing_path = auth_landing_path(project_root)
    start = asyncio.get_event_loop().time()
    while True:
        elapsed = asyncio.get_event_loop().time() - start
        if elapsed >= timeout_sec:
            return False

        try:
            if hasattr(page, "is_closed") and page.is_closed():
                return False
        except Exception:
            pass

        url = ""
        try:
            url = page.url or ""
        except Exception:
            url = ""

        if url and not is_login_url(url):
            try:
                state = await page.context.storage_state()
                if storage_has_cookie_for_url(state, url):
                    storage_path.write_text(json.dumps(state), encoding="utf-8")
                    try:
                        landing_path.write_text(url, encoding="utf-8")
                    except Exception:
                        pass
                    try:
                        persist_auth_files_to_db(project_root, storage_path, landing_path)
                    except Exception:
                        pass
                    return True
            except Exception:
                pass

        await asyncio.sleep(max(0.2, interval_sec))


def should_start_auth_watch(storage_path: Path, current_url: str) -> bool:
    if not storage_path.exists():
        return True
    if current_url and is_login_url(current_url):
        return True
    return False


def _project_id_from_root(project_root: Path) -> Optional[int]:
    try:
        match = re.match(r"(?P<id>\d+)-", project_root.name)
        if match:
            return int(match.group("id"))
    except Exception:
        return None
    return None


def persist_auth_files_to_db(
    project_root: Path,
    storage_path: Path,
    landing_path: Optional[Path] = None,
) -> None:
    project_id = _project_id_from_root(project_root)
    if not project_id:
        return
    try:
        from database.session import session_scope
        from database.models import Project
        from database.project_storage import DatabaseBackedProjectStorage
    except Exception:
        return

    try:
        with session_scope() as db:
            project = db.query(Project).filter(Project.id == project_id).first()
            if not project:
                return
            storage = DatabaseBackedProjectStorage(project, project_root, db)
            try:
                if storage_path.exists():
                    storage.write_file(
                        "auth/storage.json",
                        storage_path.read_text(encoding="utf-8"),
                        "utf-8",
                    )
            except Exception:
                pass
            try:
                if landing_path and landing_path.exists():
                    storage.write_file(
                        "auth/landing_url.txt",
                        landing_path.read_text(encoding="utf-8"),
                        "utf-8",
                    )
            except Exception:
                pass
    except Exception:
        return
