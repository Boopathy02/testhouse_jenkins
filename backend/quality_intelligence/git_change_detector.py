from __future__ import annotations

from dataclasses import dataclass
import ast
import re
from typing import Dict, List, Optional


try:
    from git import Repo
except Exception as exc:  # pragma: no cover - import guard
    Repo = None  # type: ignore[assignment]
    _GITPYTHON_IMPORT_ERROR = exc


try:
    import esprima  # type: ignore
except Exception:
    esprima = None


_PY_EXTS = {".py"}
_JS_EXTS = {".js", ".jsx", ".ts", ".tsx"}


@dataclass
class _ChangeHunk:
    start: int
    end: int


def _parse_diff_hunks(patch_text: str) -> list[_ChangeHunk]:
    hunks: list[_ChangeHunk] = []
    for line in (patch_text or "").splitlines():
        if not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = int(match.group(1))
        length = int(match.group(2) or "1")
        end = start + max(length - 1, 0)
        hunks.append(_ChangeHunk(start=start, end=end))
    return hunks


def _overlaps(hunks: list[_ChangeHunk], start: int, end: int) -> bool:
    for hunk in hunks:
        if start <= hunk.end and end >= hunk.start:
            return True
    return False


def _extract_code_segment(source: str, start: int, end: int) -> str:
    lines = source.splitlines()
    start_idx = max(start - 1, 0)
    end_idx = min(end, len(lines))
    return "\n".join(lines[start_idx:end_idx]).strip()


def _python_changes(source: str, hunks: list[_ChangeHunk], file_path: str) -> list[dict]:
    changed: list[dict] = []
    try:
        tree = ast.parse(source)
    except Exception:
        return changed
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not start or not end:
            continue
        if not _overlaps(hunks, start, end):
            continue
        name = node.name
        payload: dict = {
            "file_path": file_path,
            "code_content": _extract_code_segment(source, start, end),
        }
        if isinstance(node, ast.ClassDef):
            payload["class_name"] = name
        else:
            payload["function_name"] = name
        changed.append(payload)
    return changed


def _js_changes(source: str, hunks: list[_ChangeHunk], file_path: str) -> list[dict]:
    changed: list[dict] = []
    if not esprima:
        return changed
    try:
        tree = esprima.parseModule(source, loc=True, range=True, tolerant=True)
    except Exception:
        return changed

    def _maybe_add(name: Optional[str], start: int, end: int) -> None:
        if not name:
            return
        if not _overlaps(hunks, start, end):
            return
        payload: dict = {
            "file_path": file_path,
            "code_content": _extract_code_segment(source, start, end),
        }
        if name[:1].isupper():
            payload["component_name"] = name
        else:
            payload["function_name"] = name
        changed.append(payload)

    for node in tree.body or []:
        node_type = getattr(node, "type", "")
        loc = getattr(node, "loc", None)
        if not loc:
            continue
        start = getattr(loc.start, "line", None)
        end = getattr(loc.end, "line", None)
        if not start or not end:
            continue

        if node_type == "FunctionDeclaration" and node.id:
            _maybe_add(node.id.name, start, end)
        elif node_type == "ClassDeclaration" and node.id:
            _maybe_add(node.id.name, start, end)
        elif node_type == "VariableDeclaration":
            for decl in node.declarations or []:
                init = getattr(decl, "init", None)
                if not init:
                    continue
                init_type = getattr(init, "type", "")
                if init_type not in ("ArrowFunctionExpression", "FunctionExpression"):
                    continue
                name = getattr(getattr(decl, "id", None), "name", None)
                _maybe_add(name, start, end)
        elif node_type == "ExportDefaultDeclaration":
            declaration = getattr(node, "declaration", None)
            if not declaration:
                continue
            decl_type = getattr(declaration, "type", "")
            if decl_type in ("FunctionDeclaration", "ClassDeclaration") and declaration.id:
                _maybe_add(declaration.id.name, start, end)
    return changed


def detect_changes(repo_path: str, base_commit: str, head_commit: str) -> List[Dict]:
    """Detect changed functions/classes/components between two git commits."""
    if Repo is None:
        raise ImportError(
            "gitpython is required for detect_changes(). Install GitPython to use this module."
        ) from _GITPYTHON_IMPORT_ERROR

    repo = Repo(repo_path)
    base = repo.commit(base_commit)
    head = repo.commit(head_commit)
    diffs = base.diff(head, create_patch=True)
    results: list[dict] = []

    for diff in diffs:
        path = diff.b_path or diff.a_path
        if not path:
            continue
        ext = "." + path.split(".")[-1].lower() if "." in path else ""
        if ext not in _PY_EXTS and ext not in _JS_EXTS:
            continue
        if diff.deleted_file:
            continue
        patch_text = diff.diff.decode("utf-8", errors="ignore") if isinstance(diff.diff, (bytes, bytearray)) else str(diff.diff)
        hunks = _parse_diff_hunks(patch_text)
        if not hunks:
            continue
        try:
            source = repo.git.show(f"{head_commit}:{path}")
        except Exception:
            continue
        if ext in _PY_EXTS:
            results.extend(_python_changes(source, hunks, path))
        else:
            results.extend(_js_changes(source, hunks, path))

    # Deduplicate by file + name + content hash
    seen = set()
    deduped = []
    for item in results:
        name = item.get("function_name") or item.get("class_name") or item.get("component_name") or ""
        key = (item.get("file_path"), name, item.get("code_content"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped
