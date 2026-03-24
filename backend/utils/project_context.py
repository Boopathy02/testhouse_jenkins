from typing import Any, Iterable, List, Mapping, Optional
from contextvars import ContextVar
import os


_PROJECT_ID_CONTEXT: ContextVar[Optional[int]] = ContextVar("smartai_project_id", default=None)


def set_current_project_id(value: Optional[int]):
    return _PROJECT_ID_CONTEXT.set(value)


def reset_current_project_id(token) -> None:
    _PROJECT_ID_CONTEXT.reset(token)


def current_project_id() -> Optional[int]:
    """Return the active project id using the SMARTAI project env var."""
    ctx_value = _PROJECT_ID_CONTEXT.get()
    if ctx_value is not None:
        return ctx_value
    value = os.environ.get("SMARTAI_PROJECT_ID")
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_project_id_from_meta(meta: Mapping[str, Any]) -> Optional[int]:
    pid = meta.get("project_id")
    if pid is None:
        return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


def metadata_matches_current_project(meta: Mapping[str, Any]) -> bool:
    pid = current_project_id()
    if pid is None:
        return True
    meta_pid = _extract_project_id_from_meta(meta)
    return meta_pid == pid


def filter_metadata_by_project(metas: Iterable[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Return only the metadata entries that belong to the active project."""
    pid = current_project_id()
    if pid is None:
        return [meta for meta in metas if isinstance(meta, Mapping)]
    filtered = []
    for meta in metas:
        if not isinstance(meta, Mapping):
            continue
        if _extract_project_id_from_meta(meta) == pid:
            filtered.append(meta)
    return filtered
