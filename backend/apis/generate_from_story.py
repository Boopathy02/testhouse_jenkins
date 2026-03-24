from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pathlib import Path
from typing import List, Optional

import ast
import json
import os
import re
import textwrap
import hashlib

import pandas as pd

# Kept for future use (silence linter if configured)
from services.graph_service import read_dependency_graph, get_adjacency_list, find_path  # noqa: F401
from services.test_generation_utils import openai_client
from utils.prompt_utils import build_prompt, build_security_prompt, build_accessibility_prompt
from utils.chroma_client import get_collection
from utils.file_utils import generate_unique_name
from utils.match_utils import normalize_page_name
from utils.smart_ai_utils import analyze_test_case_content, get_smartai_src_dir

from database.models import Project, TestCaseMetadata, ImageMetadata, ImageUploadRun
from database.project_storage import DatabaseBackedProjectStorage
from database.session import session_scope, Session
from .projects_api import _ensure_project_structure, get_current_user, get_user_project
from database.models import User
from database.session import get_db
router = APIRouter()


# ----------------------------------------------------------------------
# Marker helpers
# ----------------------------------------------------------------------
_TEST_DEF_RE = re.compile(r"^\s*def\s+test_[A-Za-z0-9_]*\s*\(", re.MULTILINE)
_A11Y_DEF_RE = re.compile(r"^\s*def\s+test_a11y_[A-Za-z0-9_]*\s*\(", re.MULTILINE)


def _has_test_definition(code: str, require_a11y: bool = False) -> bool:
    if not code:
        return False
    if require_a11y:
        return bool(_A11Y_DEF_RE.search(code))
    return bool(_TEST_DEF_RE.search(code))


def _call_llm_with_retry(prompt: str, model_name: str, require_a11y: bool = False) -> str:
    result = openai_client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=int(os.getenv("AI_MAX_TOKENS", "4096")),
        temperature=float(os.getenv("AI_TEMPERATURE", "0")),
    )

    clean_output = re.sub(
        r"```(?:python)?|^\s*Here is.*?:",
        "",
        (result.choices[0].message.content or "").strip(),
        flags=re.MULTILINE,
    ).strip()

    if _has_test_definition(clean_output, require_a11y=require_a11y):
        return clean_output

    retry_prompt = (
        prompt
        + "\n\nSTRICT OUTPUT REQUIREMENT:\n"
        + "Return ONLY valid Python code with at least one function named def test_....\n"
        + "Do not include any explanations or markdown.\n"
    )
    retry_result = openai_client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": retry_prompt}],
        max_tokens=int(os.getenv("AI_MAX_TOKENS", "4096")),
        temperature=0,
    )

    retry_output = re.sub(
        r"```(?:python)?|^\s*Here is.*?:",
        "",
        (retry_result.choices[0].message.content or "").strip(),
        flags=re.MULTILINE,
    ).strip()
    return retry_output or clean_output


def _stable_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_method_map(method_map: dict) -> dict:
    normalized: dict[str, list[str]] = {}
    for key, methods in (method_map or {}).items():
        if methods is None:
            normalized[str(key)] = []
        elif isinstance(methods, list):
            normalized[str(key)] = [str(m) for m in methods]
        else:
            normalized[str(key)] = [str(m) for m in methods]
    return normalized


def _generation_cache_dir(run_folder: Path) -> Path:
    cache_dir = run_folder / "logs" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _generation_cache_key(
    *,
    user_story: str,
    test_type: str,
    site_url: str,
    method_map: dict,
    page_names: list[str],
    prompt: str,
    model_name: str,
) -> str:
    normalized_methods = _normalize_method_map(method_map)
    payload = {
        "story": user_story or "",
        "test_type": test_type or "",
        "site_url": site_url or "",
        "model": model_name or "",
        "page_names": page_names or [],
        "method_map": normalized_methods,
        "prompt_hash": _stable_hash(prompt or ""),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return _stable_hash(raw)


def _load_cached_generation(cache_dir: Path, cache_key: str) -> Optional[str]:
    cache_file = cache_dir / f"{cache_key}.json"
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    code = payload.get("code")
    return code if isinstance(code, str) and code.strip() else None


def _store_cached_generation(
    cache_dir: Path,
    cache_key: str,
    *,
    code: str,
    meta: dict,
) -> None:
    cache_file = cache_dir / f"{cache_key}.json"
    payload = {
        "meta": meta,
        "code": code,
    }
    try:
        cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass

def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s\"'<>]+", text or "")
    if not match:
        return ""
    return match.group(0).rstrip(").,")


def _get_markers_for_test(db_session: Session, project_id: int, test_name: str) -> List[str]:
    """Fetch markers for a given test case directly from the database."""
    if not project_id or not test_name:
        return []
    try:
        record = (
            db_session.query(TestCaseMetadata)
            .filter(
                TestCaseMetadata.project_id == project_id,
                TestCaseMetadata.test_name == test_name,
            )
            .first()
        )
        if record and record.markers and isinstance(record.markers, list):
            return record.markers
    except Exception as e:
        print(f"Database query for markers failed: {e}")
    return []


def _format_markers_as_pytest_decorators(markers: List[str]) -> List[str]:
    """
    Convert marker strings into valid pytest decorator lines.

    Markers may contain whitespace or special characters; pytest marker attributes
    must be valid Python identifiers, so we sanitize them deterministically.
    """
    decorators: List[str] = []
    for marker in markers or []:
        raw = str(marker or "").strip()
        if not raw:
            continue
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_")
        if not safe:
            continue
        if safe[0].isdigit():
            safe = f"m_{safe}"
        decorators.append(f"@pytest.mark.{safe}")
    # Deduplicate while preserving order.
    return list(dict.fromkeys(decorators))


# ----------------------------------------------------------------------
# Merge helpers for incremental metadata updates
# ----------------------------------------------------------------------
def _is_blank_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        stripped = value.strip().lower()
        return stripped == "" or stripped in {"[]", "{}"}
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _record_identity(record: dict) -> str | None:
    if not isinstance(record, dict):
        return None
    page = (record.get("page_name") or "").strip().lower()
    intent = (record.get("intent") or "").strip().lower()
    ocr_type = (record.get("ocr_type") or "").strip().lower()
    if intent or ocr_type:
        return f"intent:{page}|{intent}|{ocr_type}"
    for key in ("ocr_id", "id", "unique_name", "element_id"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip().lower()}"
    label = (record.get("label_text") or "").strip().lower()
    if any((label, intent, ocr_type)):
        return f"fallback:{label}|{intent}|{ocr_type}"
    bbox = record.get("bbox")
    if isinstance(bbox, str) and bbox.strip():
        return f"bbox:{bbox.strip().lower()}"
    return None


def _should_replace(old_value, new_value) -> bool:
    if isinstance(new_value, bool):
        return new_value and not bool(old_value)
    if _is_blank_value(new_value):
        return False
    return old_value is None or _is_blank_value(old_value)


def _merge_record(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})
    prefer_new = {"label_text", "get_by_text", "placeholder", "unique_name"}
    for key, value in (incoming or {}).items():
        if key in prefer_new:
            if not _is_blank_value(value):
                merged[key] = value
            continue
        if key not in merged or _should_replace(merged.get(key), value):
            merged[key] = value
    return merged


def _merge_metadata_records(existing_records: list[dict], new_records: list[dict]) -> list[dict]:
    """
    Merge records keyed by identity. Existing entries for pages present in the new set
    that are not in the new identities are dropped (authoritative replacement per page).
    Other pages are preserved.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    counter = 0

    # Identify pages covered by this new snapshot
    new_pages = {
        normalize_page_name((r or {}).get("page_name") or "")
        for r in (new_records or [])
        if isinstance(r, dict)
    }
    new_identities = {_record_identity(r) for r in (new_records or [])}

    def _store(key: str, record: dict):
        if key not in order:
            order.append(key)
        merged[key] = dict(record or {})

    for record in existing_records or []:
        key = _record_identity(record)
        page = normalize_page_name((record or {}).get("page_name") or "")
        if page in new_pages:
            # Only keep existing entry if it is also present in the new set
            if key and key in new_identities:
                _store(key, record)
            # else drop it
        else:
            if not key:
                key = f"existing-{counter}"
                counter += 1
            _store(key, record)

    for record in new_records or []:
        key = _record_identity(record)
        if not key:
            key = f"new-{counter}"
            counter += 1
        if key in merged:
            merged[key] = _merge_record(merged[key], record)
        else:
            _store(key, record)

    return [merged[k] for k in order if k in merged]


# ----------------------------------------------------------------------
# Project/run helpers
# ----------------------------------------------------------------------
def _next_run_dir(base_dir: Path, prefix: str = "run") -> Path:
    existing = []
    for child in base_dir.iterdir():
        if not child.is_dir():
            continue
        match = re.match(rf"{re.escape(prefix)}_(\d+)$", child.name)
        if match:
            existing.append(int(match.group(1)))
    next_idx = (max(existing) + 1) if existing else 1
    run_dir = base_dir / f"{prefix}_{next_idx}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "__init__.py").touch(exist_ok=True)
    return run_dir


def _latest_upload_snapshot(project_id: int) -> list[dict]:
    if not project_id:
        return []
    with session_scope() as db:
        latest_run = (
            db.query(ImageUploadRun)
            .filter(ImageUploadRun.project_id == project_id)
            .order_by(ImageUploadRun.created_at.desc())
            .first()
        )
        if not latest_run or not isinstance(latest_run.results, list):
            return []
        metadata_ids = [
            item.get("metadata_id")
            for item in latest_run.results
            if isinstance(item, dict) and item.get("metadata_id")
        ]
        if not metadata_ids:
            return []
        records = (
            db.query(ImageMetadata)
            .filter(ImageMetadata.project_id == project_id, ImageMetadata.id.in_(metadata_ids))
            .all()
        )
        payload: list[dict] = []
        for record in records:
            if isinstance(record.metadata_json, list):
                payload.extend([m for m in record.metadata_json if isinstance(m, dict)])
        return payload


def _resolve_active_project_id() -> int:
    project_id_str = os.environ.get("SMARTAI_PROJECT_ID")
    if project_id_str and project_id_str.isdigit():
        return int(project_id_str)
    project_dir = os.environ.get("SMARTAI_PROJECT_DIR")
    if project_dir:
        try:
            segment = Path(project_dir).name
            match = re.match(r"(?P<id>\d+)-", segment)
            if match:
                return int(match.group("id"))
        except Exception:
            return 0
    return 0


# ----------------------------------------------------------------------
# Page selection + code normalization helpers
# ----------------------------------------------------------------------
def _select_story_pages(user_story: str, method_map_full: dict, max_pages: int = 5) -> list[str]:
    story_text = (user_story or "").lower()
    if not story_text:
        return list(method_map_full.keys())
    scored: list[tuple[int, str]] = []
    required_pages: dict[str, int] = {}
    for page, methods in method_map_full.items():
        score = 0
        page_tokens = [t for t in re.split(r"[_\W]+", page.lower()) if len(t) >= 3]
        if any(token in story_text for token in page_tokens):
            required_pages[page] = max(required_pages.get(page, 0), 1)
        for method in methods or []:
            name = method.split("(")[0].replace("def ", "").strip()
            base = name
            for prefix in ("enter_", "fill_", "select_", "click_", "verify_", "assert_"):
                if base.startswith(prefix):
                    base = base[len(prefix) :]
                    break
            tokens = [t for t in re.split(r"[_\W]+", base) if len(t) >= 3]
            for token in tokens:
                if token in story_text:
                    score += 1
            if any(token in story_text for token in tokens):
                required_pages[page] = max(required_pages.get(page, 0), 1)
        if score:
            scored.append((score, page))
    if not scored:
        return list(method_map_full.keys())
    scored.sort(key=lambda item: item[0], reverse=True)
    max_score = scored[0][0]
    threshold = max_score * 0.3
    selected = [page for score, page in scored if score >= threshold]
    for _, page in scored:
        if page in required_pages and page not in selected:
            selected.append(page)
    if not selected:
        return [scored[0][1]]
    if len(selected) > max_pages:
        required = [page for page in selected if page in required_pages]
        extras = [page for page in selected if page not in required]
        selected = required + extras[: max(0, max_pages - len(required))]
    return selected


def _extract_method_name(signature: str) -> str:
    return signature.split("(")[0].replace("def ", "").strip()


def _method_tokens(method_name: str) -> list[str]:
    base = method_name
    for prefix in ("enter_", "fill_", "select_", "click_", "verify_", "assert_"):
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    return [t for t in re.split(r"[_\W]+", base.lower()) if len(t) >= 3]


def _infer_value_for_method(method_name: str, story_text: str) -> Optional[str]:
    tokens = _method_tokens(method_name)
    if not tokens:
        return None
    for line in (story_text or "").splitlines():
        lower = line.lower()
        if any(token in lower for token in tokens):
            match = re.search(r"\"([^\"]+)\"", line)
            if match:
                return match.group(1)
    return None


def _best_method_for_step(step_text: str, allowed_methods: list[str], desired_prefix: str) -> Optional[str]:
    tokens = [t for t in re.split(r"[_\W]+", step_text.lower()) if len(t) >= 3]
    best_score = 0
    best_method = None
    for method in allowed_methods:
        if not method.startswith(desired_prefix):
            continue
        method_tokens = _method_tokens(method)
        score = sum(1 for t in method_tokens if t in tokens)
        if score > best_score:
            best_score = score
            best_method = method
    return best_method


def _extract_click_steps_from_story(story_text: str, allowed_methods: list[str]) -> list[str]:
    if not story_text:
        return []
    steps: list[str] = []
    click_re = re.compile(r'click(?:ed)?(?: the)?\s+"([^"]+)"', re.IGNORECASE)
    for line in story_text.splitlines():
        for match in click_re.finditer(line):
            label = match.group(1).strip()
            if not label:
                continue
            method = _best_method_for_step(label, allowed_methods, "click_")
            if not method:
                method = _best_method_for_step(line, allowed_methods, "click_")
            if method and method not in steps:
                steps.append(method)
    return steps


def _pick_submit_method(allowed_methods: list[str], story_text: str) -> Optional[str]:
    click_methods = [m for m in allowed_methods if m.startswith("click_")]
    if not click_methods:
        return None
    has_add = any("add_customer" in m for m in click_methods)
    has_create = any("create_customer" in m for m in click_methods)
    if has_add and has_create:
        return next((m for m in click_methods if "add_customer" in m), None)
    # Prefer obvious submit/save/add names if present
    for token in ("submit", "save", "add", "confirm", "create"):
        for m in click_methods:
            if token in m:
                return m
    # Fallback to last click step from story
    click_steps = _extract_click_steps_from_story(story_text, allowed_methods)
    return click_steps[-1] if click_steps else click_methods[-1]


def _inject_security_navigation_and_submit(code: str, method_map: dict, story_text: str) -> str:
    allowed_methods: list[str] = []
    for methods in (method_map or {}).values():
        for method in methods or []:
            allowed_methods.append(_extract_method_name(method))

    nav_steps = _extract_click_steps_from_story(story_text, allowed_methods)
    submit_method = _pick_submit_method(allowed_methods, story_text)

    if not nav_steps and not submit_method:
        return code

    lines = code.splitlines(True)
    out: list[str] = []
    in_func = False
    func_lines: list[str] = []
    func_indent = ""

    def _flush_func():
        nonlocal func_lines
        if not func_lines:
            return
        out.extend(_process_func_block(func_lines))
        func_lines = []

    def _process_func_block(block: list[str]) -> list[str]:
        text = "".join(block)
        missing_nav = [
            m
            for m in nav_steps
            if not re.search(rf"^\s*{re.escape(m)}\(\s*page", text, re.MULTILINE)
        ]
        injected = []
        inserted_nav = False
        for line in block:
            injected.append(line)
            if (not inserted_nav) and re.search(r"\bpage\.goto\(", line):
                indent = re.match(r"^(\s*)", line).group(1)
                for m in missing_nav:
                    injected.append(f"{indent}{m}(page)\n")
                inserted_nav = True

        if submit_method:
            injected = _inject_submit_in_loop(injected, submit_method)
        return injected

    def _inject_submit_in_loop(block: list[str], submit: str) -> list[str]:
        result: list[str] = []
        i = 0
        while i < len(block):
            line = block[i]
            result.append(line)
            loop_match = re.match(r"^(\s*)for\s+\w+\s+in\s+payloads\s*:\s*$", line)
            if not loop_match:
                i += 1
                continue
            loop_indent = loop_match.group(1)
            body_indent = None
            body_lines: list[str] = []
            j = i + 1
            while j < len(block):
                next_line = block[j]
                if body_indent is None:
                    if next_line.strip() == "":
                        body_lines.append(next_line)
                        j += 1
                        continue
                    body_indent = re.match(r"^(\s*)", next_line).group(1)
                if re.match(rf"^{re.escape(loop_indent)}\S", next_line):
                    break
                body_lines.append(next_line)
                j += 1

            body_text = "".join(body_lines)
            if not re.search(rf"^\s*{re.escape(submit)}\(\s*page", body_text, re.MULTILINE):
                if body_indent is None:
                    body_indent = loop_indent + "    "
                body_lines.append(f"{body_indent}{submit}(page)\n")
            result.extend(body_lines)
            i = j
            continue
        return result

    for line in lines:
        if re.match(r"^def\s+[A-Za-z0-9_]+\s*\(", line):
            _flush_func()
            in_func = True
            func_indent = re.match(r"^(\s*)", line).group(1)
            func_lines.append(line)
            continue
        if in_func:
            if line.strip() == "" and func_indent == "":
                func_lines.append(line)
            else:
                func_lines.append(line)
            continue
        out.append(line)
    _flush_func()
    return "".join(out)


def _sanitize_security_numeric_fields(code: str, method_map: dict) -> str:
    """
    Avoid injecting non-numeric payloads into number-only fields.
    """
    allowed_methods: list[str] = []
    for methods in (method_map or {}).values():
        for method in methods or []:
            allowed_methods.append(_extract_method_name(method))

    numeric_markers = ("income", "deposit", "amount", "balance", "number")
    numeric_methods = [m for m in allowed_methods if m.startswith("enter_") and any(k in m for k in numeric_markers)]
    if not numeric_methods:
        return code

    lines = code.splitlines(True)
    out: list[str] = []
    for line in lines:
        loop_match = re.match(r"^(\s*)for\s+\w+\s+in\s+payloads\s*:\s*$", line)
        out.append(line)
        if loop_match:
            indent = loop_match.group(1) + "    "
            out.append(f'{indent}numeric_payload = "123"\n')
        else:
            for m in numeric_methods:
                line = re.sub(
                    rf"^(\s*){re.escape(m)}\(\s*page\s*,\s*payload\s*\)\s*$",
                    rf"\1{m}(page, numeric_payload)",
                    line,
                )
            out[-1] = line
    return "".join(out)


def _split_inline_calls(code: str) -> str:
    """
    Ensure each helper call is on its own line.
    """
    out: list[str] = []
    call_prefix = r"(?:enter_|fill_|select_|click_|verify_|assert_|type_)"
    for line in code.splitlines(True):
        indent = re.match(r"^(\s*)", line).group(1)
        updated = line
        updated = re.sub(rf"\)\s+(?={call_prefix})", f")\n{indent}", updated)
        out.append(updated)
    return "".join(out)


def _normalize_generated_code(code: str, method_map: dict, story_text: str) -> str:
    """
    Post-fix common LLM misses:
    - If it wrote '# Skipped step due to missing method:' try to map to closest allowed method.
    - If it called a non-existent method name, remap to best available with same prefix.
    """
    allowed_methods = []
    for methods in (method_map or {}).values():
        for method in methods or []:
            allowed_methods.append(_extract_method_name(method))
    allowed_set = set(allowed_methods)

    lines = code.splitlines(True)
    out_lines: list[str] = []
    for line in lines:
        skip_match = re.match(r"^(\s*)#\s*Skipped step due to missing method:\s*(.+)$", line)
        if skip_match:
            indent, step = skip_match.groups()
            step_lower = step.lower()
            if "enter " in step_lower:
                desired_prefix = "enter_"
            elif "select " in step_lower:
                desired_prefix = "select_"
            elif "click " in step_lower:
                desired_prefix = "click_"
            elif "verify " in step_lower:
                desired_prefix = "verify_"
            else:
                desired_prefix = ""
            if desired_prefix:
                method = _best_method_for_step(step, allowed_methods, desired_prefix)
                if method:
                    value = None
                    if desired_prefix in ("enter_", "select_"):
                        value = _infer_value_for_method(method, story_text)
                    if value is not None:
                        out_lines.append(f'{indent}{method}(page, "{value}")\n')
                    else:
                        out_lines.append(f"{indent}{method}(page)\n")
                    continue

        call_match = re.match(r"^(\s*)([a-zA-Z_][a-zA-Z0-9_]*)\(page(?:,\s*(.*))?\)\s*$", line)
        if call_match:
            indent, name, args = call_match.groups()
            if name not in allowed_set:
                desired_prefix = ""
                for prefix in ("enter_", "fill_", "select_", "click_", "verify_", "assert_"):
                    if name.startswith(prefix):
                        desired_prefix = prefix
                        break
                if desired_prefix:
                    method = _best_method_for_step(name, allowed_methods, desired_prefix)
                    if method:
                        if desired_prefix in ("enter_", "select_") and (args is None or args.strip() in ("\"\"", "''")):
                            value = _infer_value_for_method(method, story_text)
                            if value is not None:
                                out_lines.append(f'{indent}{method}(page, "{value}")\n')
                                continue
                        replacement = f"{indent}{method}(page"
                        if args:
                            replacement += f", {args}"
                        replacement += ")\n"
                        out_lines.append(replacement)
                        continue

        out_lines.append(line)
    return "".join(out_lines)


def _safe_slug(value: str, fallback: str = "story", max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", (value or "").lower()).strip("_")
    if slug:
        parts = [p for p in slug.split("_") if p and p not in {"successfully"}]
        slug = "_".join(parts)
    if not slug:
        slug = fallback
    return slug[:max_len]


def _infer_script_slug(story: str, category: str) -> str:
    """
    Use the LLM to propose a short, descriptive filename base for the story.
    Falls back to a sanitized story snippet when inference fails.
    """
    story_clean = (story or "").strip()
    if not story_clean:
        return "story"
    prompt = (
        "Create a short 3-6 word snake_case filename base that summarizes this user story. "
        "Only output the filename base (no extension, no quotes, no extra text).\n\n"
        f"Story:\n{story_clean}\n"
    )
    model_name = os.getenv("AI_INFER_MODEL", os.getenv("AI_MODEL_NAME", "gpt-4o"))
    try:
        result = openai_client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=40,
            temperature=0,
        )
        raw = (result.choices[0].message.content or "").strip()
        raw = raw.splitlines()[0].strip().strip("\"'`")
        raw = re.sub(r"\.py$", "", raw, flags=re.IGNORECASE).strip()
        slug = _safe_slug(raw)
        if slug:
            return slug
    except Exception:
        pass
    return _safe_slug(" ".join(story_clean.split()[:6]))


def _strip_module_qualified_helpers(code: str, method_map: dict) -> str:
    """
    Replace module-qualified helper calls (e.g., bank_customer.click_x(page))
    with the plain helper name (click_x(page)).
    """
    helper_names = set()
    for methods in (method_map or {}).values():
        for method_def in methods:
            name = method_def.split("(", 1)[0].replace("def ", "").strip()
            if name:
                helper_names.add(name)
    if not helper_names:
        return code
    cleaned = code
    for helper_name in helper_names:
        module_pattern = rf"(?:[a-zA-Z_][a-zA-Z0-9_]*\.)+{re.escape(helper_name)}\("
        cleaned = re.sub(module_pattern, f"{helper_name}(", cleaned)
    return cleaned


# ----------------------------------------------------------------------
# Assertions injection
# ----------------------------------------------------------------------
METHOD_CALL_RE = re.compile(r"^(\s*)((?:enter_|fill_)[a-zA-Z0-9_]+)\(\s*page\s*,\s*(.+?)\s*\)\s*$")
ASSERT_CALL_RE = re.compile(r"^(\s*)assert_([a-zA-Z0-9_]+)\(\s*page\s*,\s*(.+?)\s*\)\s*$")


def inject_assertions_after_actions(code: str) -> str:
    """
    For every line like: enter_xxx(page, <value>)
    insert the next line: assert_enter_xxx(page, <value>)
    """
    out_lines: List[str] = []
    for line in code.splitlines(True):
        out_lines.append(line)
        m = METHOD_CALL_RE.match(line)
        if not m:
            continue
        indent, method_name, value_expr = m.groups()
        assert_name = f"assert_{method_name}"
        out_lines.append(f"{indent}{assert_name}(page, {value_expr})\n")
    return "".join(out_lines)


def remove_redundant_assertions(code: str) -> str:
    """
    Drop duplicate assert_* lines unless a new value was entered since the last assert.
    """
    out_lines: List[str] = []
    last_enter_value: dict[str, str] = {}
    needs_assert: dict[str, bool] = {}

    for line in code.splitlines(True):
        enter_match = METHOD_CALL_RE.match(line)
        if enter_match:
            _, method_name, value_expr = enter_match.groups()
            last_enter_value[method_name] = value_expr.strip()
            needs_assert[method_name] = True
            out_lines.append(line)
            continue

        assert_match = ASSERT_CALL_RE.match(line)
        if assert_match:
            _, assert_target, value_expr = assert_match.groups()
            base_method = assert_target
            value_key = value_expr.strip()
            if base_method in last_enter_value and last_enter_value[base_method] == value_key:
                if not needs_assert.get(base_method, False):
                    continue
                needs_assert[base_method] = False
            out_lines.append(line)
            continue

        out_lines.append(line)

    return "".join(out_lines)


# ----------------------------------------------------------------------
# Split generated tests by category
# ----------------------------------------------------------------------
def _split_generated_tests_by_category(code: str) -> dict[str, list[str]]:
    """
    Partition generated test code into UI, security, and accessibility buckets.
    Only named test functions (def test_*(...):) are considered.
    """
    functions: List[tuple[str, str]] = []
    current_name: Optional[str] = None
    current_lines: List[str] = []

    for line in code.splitlines(True):
        m = re.match(r"^def\s+(test_[a-zA-Z0-9_]+)\(([^)]*)\)\s*:", line)
        if m:
            if current_name and current_lines:
                functions.append((current_name, "".join(current_lines)))
            current_name = m.group(1)
            current_lines = [line]
            continue
        if current_name:
            current_lines.append(line)

    if current_name and current_lines:
        functions.append((current_name, "".join(current_lines)))

    grouped = {"ui": [], "security": [], "accessibility": []}
    for name, body in functions:
        if name.startswith("test_security"):
            grouped["security"].append(body)
        elif name.startswith("test_accessibility") or name.startswith("test_a11y"):
            grouped["accessibility"].append(body)
        else:
            grouped["ui"].append(body)
    return grouped


# ----------------------------------------------------------------------
# File / DB helpers
# ----------------------------------------------------------------------
def create_default_test_data(
    run_folder: Path,
    method_map_full: Optional[dict] = None,
    test_data_json: Optional[str] = None,
) -> None:
    """
    Create or write test data for the run. If `test_data_json` is provided it will be used (must be JSON string).
    Otherwise a minimal scaffold is created by scanning method_map_full for common keys.
    """
    data = {}
    if test_data_json:
        try:
            data = json.loads(test_data_json)
        except Exception:
            data = {}
    else:
        if method_map_full:
            for page_key, _methods in method_map_full.items():
                data[page_key] = {}
        if not data:
            data = {"__meta__": {}}

    data_dir = Path(run_folder) / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "__init__.py").touch()
    with open(data_dir / "test_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _persist_directory_to_db(src_dir: Path, target_dir: Path) -> None:
    if not target_dir.exists() or not target_dir.is_dir():
        return
    project_id_value = os.environ.get("SMARTAI_PROJECT_ID")
    if not project_id_value:
        return
    try:
        project_id = int(project_id_value)
    except ValueError:
        return

    with session_scope() as db:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return
        storage = DatabaseBackedProjectStorage(project, src_dir, db)
        for path in sorted(target_dir.rglob("*.py")):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(src_dir).as_posix()
            except ValueError:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except Exception:
                continue
            storage.write_file(relative, content, "utf-8")


def extract_method_names_from_file(file_path: Path) -> List[str]:
    method_names: List[str] = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            m = re.match(r"def\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\([^\)]*\):", line)
            if m:
                method_names.append(line.strip())
    return method_names


def get_all_page_methods(pages_dir: Path) -> dict:
    page_method_map = {}
    for py_file in Path(pages_dir).glob("*_page_methods.py"):
        page_name = py_file.stem.replace("_page_methods", "")
        page_method_map[page_name] = extract_method_names_from_file(py_file)
    return page_method_map


def next_index(target_dir: Path, pattern: str = "test_{}.py") -> int:
    files = list(target_dir.glob(pattern.format("*")))
    indices = [int(m.group(1)) for f in files if (m := re.match(r".*_(\d+)\.", f.name))]
    return max(indices, default=0) + 1


def _get_next_story_index(tests_dir: Path) -> int:
    """
    Continue TS numbering across runs by finding the max TS_### in existing tests.
    """
    max_ts = 0
    pattern = re.compile(r"TS_(\d{3})")
    for path in tests_dir.rglob("*.py"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for match in pattern.finditer(content):
            try:
                value = int(match.group(1))
                if value > max_ts:
                    max_ts = value
            except Exception:
                continue
    return max_ts + 1


# ----------------------------------------------------------------------
# Chroma normalization: IMPORTANT FIX
# Ensures we always write FLAT metadata objects into before_enrichment.json
# (not {"id":..., "document":..., "metadata": {...}}).
# ----------------------------------------------------------------------
def _normalize_chroma_meta(meta: object, fallback_id: Optional[str] = None) -> dict:
    """
    Chroma metadata can arrive in multiple shapes:
    1) dict with the real metadata directly
    2) dict like {"id":..., "document":..., "metadata": {...}}
    3) list/tuple where one item is a dict

    We ALWAYS return the FLAT metadata dict (the inner "metadata" if present),
    and strip noisy keys so before_enrichment.json is stable and clean.
    """
    # Unwrap list/tuple shapes
    if isinstance(meta, (list, tuple)):
        for item in meta:
            if isinstance(item, dict):
                meta = item
                break

    if not isinstance(meta, dict):
        return {}

    # Unwrap export shape: {"id":..., "document":..., "metadata": {...}}
    if "metadata" in meta and isinstance(meta.get("metadata"), dict):
        meta = meta["metadata"]

    if not isinstance(meta, dict):
        return {}

    # Remove noisy/unstable keys (optional but recommended)
    drop_keys = {"project_id", "id", "document", "embedding", "distance", "score"}
    cleaned = {k: v for k, v in meta.items() if k not in drop_keys}

    # Ensure element_id is always present
    if fallback_id and not cleaned.get("element_id"):
        cleaned["element_id"] = fallback_id

    return cleaned


# ----------------------------------------------------------------------
# LLM generation functions
# ----------------------------------------------------------------------
def generate_security_test_code_from_methods(
    user_story: str,
    method_map: dict,
    page_names: List[str],
    site_url: str,
    run_folder: Path,
    project_src_dir: Path,
) -> str:
    prompt = build_security_prompt(
        story_block=user_story,
        method_map=method_map,
        page_names=page_names,
        site_url=site_url,
        project_src_dir=project_src_dir,
    )
    model_name = os.getenv("AI_MODEL_NAME", "gpt-4o")
    cache_dir = _generation_cache_dir(run_folder)
    cache_key = _generation_cache_key(
        user_story=user_story,
        test_type="security",
        site_url=site_url,
        method_map=method_map,
        page_names=page_names,
        prompt=prompt,
        model_name=model_name,
    )
    cached_output = _load_cached_generation(cache_dir, cache_key)

    prompt_dir = run_folder / "logs" / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        prompt_file = prompt_dir / f"security_prompt_{i}.md"
        if not prompt_file.exists():
            break
        i += 1
    prompt_file.write_text(prompt, encoding="utf-8")

    output_dir = run_folder / "logs" / "test_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        output_file = output_dir / f"security_test_output_{i}.py"
        if not output_file.exists():
            break
        i += 1

    if cached_output:
        clean_output = cached_output
    else:
        clean_output = _call_llm_with_retry(prompt, model_name)

        try:
            if site_url and str(site_url).strip():
                goto_literal = json.dumps(site_url)
                clean_output = re.sub(r"page\.goto\([^\)]*\)", f"page.goto({goto_literal})", clean_output)
        except Exception:
            pass
        clean_output = _inject_security_navigation_and_submit(clean_output, method_map, user_story)
        clean_output = _sanitize_security_numeric_fields(clean_output, method_map)
        clean_output = _split_inline_calls(clean_output)
        _store_cached_generation(
            cache_dir,
            cache_key,
            code=clean_output,
            meta={
                "story": user_story,
                "test_type": "security",
                "site_url": site_url,
                "model": model_name,
                "method_map_hash": _stable_hash(
                    json.dumps(_normalize_method_map(method_map), sort_keys=True, ensure_ascii=True)
                ),
                "prompt_hash": _stable_hash(prompt or ""),
            },
        )

    output_file.write_text(clean_output, encoding="utf-8")
    return clean_output


def generate_accessibility_test_code_from_methods(
    user_story: str,
    method_map: dict,
    page_names: List[str],
    site_url: str,
    run_folder: Path,
    project_src_dir: Path,
) -> str:
    prompt = build_accessibility_prompt(
        story_block=user_story,
        method_map=method_map,
        page_names=page_names,
        site_url=site_url,
        project_src_dir=project_src_dir,
    )
    model_name = os.getenv("AI_MODEL_NAME", "gpt-4o")
    cache_dir = _generation_cache_dir(run_folder)
    cache_key = _generation_cache_key(
        user_story=user_story,
        test_type="accessibility",
        site_url=site_url,
        method_map=method_map,
        page_names=page_names,
        prompt=prompt,
        model_name=model_name,
    )
    cached_output = _load_cached_generation(cache_dir, cache_key)

    prompt_dir = run_folder / "logs" / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        prompt_file = prompt_dir / f"accessibility_prompt_{i}.md"
        if not prompt_file.exists():
            break
        i += 1
    prompt_file.write_text(prompt, encoding="utf-8")

    if cached_output:
        clean_output = cached_output
    else:
        clean_output = _call_llm_with_retry(prompt, model_name, require_a11y=True)

        try:
            if site_url and str(site_url).strip():
                goto_literal = json.dumps(site_url)
                clean_output = re.sub(r"page\.goto\([^\)]*\)", f"page.goto({goto_literal})", clean_output)
        except Exception:
            pass
        _store_cached_generation(
            cache_dir,
            cache_key,
            code=clean_output,
            meta={
                "story": user_story,
                "test_type": "accessibility",
                "site_url": site_url,
                "model": model_name,
                "method_map_hash": _stable_hash(
                    json.dumps(_normalize_method_map(method_map), sort_keys=True, ensure_ascii=True)
                ),
                "prompt_hash": _stable_hash(prompt or ""),
            },
        )

    output_dir = run_folder / "logs" / "test_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        output_file = output_dir / f"accessibility_test_output_{i}.py"
        if not output_file.exists():
            break
        i += 1
    output_file.write_text(clean_output, encoding="utf-8")
    return clean_output


def generate_test_code_from_methods(
    user_story: str,
    method_map: dict,
    page_names: List[str],
    site_url: str,
    run_folder: Path,
    project_src_dir: Path,
) -> str:
    dynamic_steps: List[str] = []
    for methods in method_map.values():
        for method in methods:
            name = method.split("(")[0].replace("def ", "").strip()
            if name.startswith(("enter_", "fill_")):
                param = name.replace("enter_", "").replace("fill_", "")
                dynamic_steps.append(f'    - Call `{name}("<{param}>")`')
            elif name.startswith(("click_", "select_")):
                if name.startswith("select_"):
                    dynamic_steps.append(f'    - Call `{name}("<value>")`')
                else:
                    dynamic_steps.append(f'    - Call `{name}()`')
            elif name.startswith("verify_"):
                readable = name.replace("verify_", "").replace("_", " ").capitalize()
                dynamic_steps.append(f"    - Assert `{name}()` checks if **{readable}** is visible")

    user_story_clean = user_story.replace('"""', '\\"""')
    story_block = f'"""{user_story_clean}"""'

    output_dir = run_folder / "logs" / "dynamic_steps"
    output_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        output_file = output_dir / f"dynamic_steps_{i}.md"
        if not output_file.exists():
            break
        i += 1
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# Dynamic Steps\n\n")
        for step in dynamic_steps:
            f.write(step + "\n")

    prompt = build_prompt(
        story_block=story_block,
        method_map=method_map,
        page_names=page_names,
        site_url=site_url,
        dynamic_steps=dynamic_steps,
        project_src_dir=project_src_dir,
    )
    model_name = os.getenv("AI_MODEL_NAME", "gpt-4o")
    cache_dir = _generation_cache_dir(run_folder)
    cache_key = _generation_cache_key(
        user_story=user_story,
        test_type="ui",
        site_url=site_url,
        method_map=method_map,
        page_names=page_names,
        prompt=prompt,
        model_name=model_name,
    )
    cached_output = _load_cached_generation(cache_dir, cache_key)

    prompt_dir = run_folder / "logs" / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        prompt_file = prompt_dir / f"prompt_{i}.md"
        if not prompt_file.exists():
            break
        i += 1
    prompt_file.write_text(prompt, encoding="utf-8")

    if cached_output:
        clean_output = cached_output
    else:
        clean_output = _call_llm_with_retry(prompt, model_name)

        clean_output = inject_assertions_after_actions(clean_output)
        clean_output = remove_redundant_assertions(clean_output)

        try:
            if site_url and str(site_url).strip():
                goto_literal = json.dumps(site_url)
                clean_output = re.sub(r"page\.goto\([^\)]*\)", f"page.goto({goto_literal})", clean_output)
        except Exception:
            pass
        _store_cached_generation(
            cache_dir,
            cache_key,
            code=clean_output,
            meta={
                "story": user_story,
                "test_type": "ui",
                "site_url": site_url,
                "model": model_name,
                "method_map_hash": _stable_hash(
                    json.dumps(_normalize_method_map(method_map), sort_keys=True, ensure_ascii=True)
                ),
                "prompt_hash": _stable_hash(prompt or ""),
            },
        )

    output_dir = run_folder / "logs" / "test_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        output_file = output_dir / f"test_output_{i}.py"
        if not output_file.exists():
            break
        i += 1
    output_file.write_text(clean_output, encoding="utf-8")

    return clean_output


def get_inferred_pages(user_story: str, method_map_full: dict, client) -> List[str]:
    page_list_str = "\n".join([f"{i+1}. {k.replace('_', ' ')}" for i, k in enumerate(method_map_full.keys())])
    story_block = f'"""{user_story}"""'
    prompt = f"""
You are an expert QA automation engineer.

Given the following available application pages:
{page_list_str}

Here is a user story:
{story_block}

Output ONLY a Python list (in order) of the page keys (use the keys exactly as shown) that must be visited for this story. Do not explain.
"""
    model_name = os.getenv("AI_INFER_MODEL", "gpt-4o")
    result = client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=int(os.getenv("AI_MAX_TOKENS", "4096")),
        temperature=float(os.getenv("AI_TEMPERATURE", "0")),
    )

    output = (result.choices[0].message.content or "").strip()
    try:
        inferred_pages = ast.literal_eval(output)
        return [p for p in inferred_pages if p in method_map_full]
    except Exception:
        return list(method_map_full.keys())


# ----------------------------------------------------------------------
# MAIN ENDPOINT (snapshot concept removed)
# ----------------------------------------------------------------------
@router.post("/{project_id}/rag/generate-from-story")
async def generate_from_user_story(
    project_id: int,
    user_story: Optional[str] = Form(None),
    site_url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    ai_model: Optional[str] = Form(None),
    infer_pages: Optional[bool] = Form(False),
    test_data_json: Optional[str] = Form(None),
    test_type: Optional[str] = Form("ui"),
    jira_key: Optional[str] = Form(None),
    acceptance_criteria: Optional[str] = Form(None),
    selection_mode: Optional[str] = Form(None),  # kept for compatibility (unused now)
    replace_existing: Optional[bool] = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # src_env = os.environ.get("SMARTAI_SRC_DIR")
    # if not src_env:
    #     raise HTTPException(status_code=400, detail="No active project. Start a project first (SMARTAI_SRC_DIR not set).")
    project = get_user_project(db, project_id, current_user)
    project_paths = _ensure_project_structure(project)
    projectChromaPath= project_paths["chroma_path"]
    run_folder =Path(project_paths["src_dir"])
    pages_dir = run_folder / "pages"
    tests_dir = run_folder / "tests"
    ui_tests_dir = tests_dir / "ui_scripts"
    security_tests_dir = tests_dir / "security_tests"
    accessibility_tests_dir = tests_dir / "accessibility_tests"
    logs_dir = run_folder / "logs"
    meta_dir = run_folder / "metadata"

    all_dirs = [
        pages_dir,
        tests_dir,
        ui_tests_dir,
        security_tests_dir,
        accessibility_tests_dir,
        logs_dir,
        meta_dir,
    ]
    for d in all_dirs:
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").touch()

    # ---------------- Parse incoming user stories ----------------
    stories: List[str] = []
    if file:
        import io

        content = await file.read()
        if file.filename.endswith((".xls", ".xlsx")):
            try:
                xls = pd.ExcelFile(io.BytesIO(content))
                if "User Stories" not in xls.sheet_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Sheet 'User Stories' not found. Sheets present: {xls.sheet_names}",
                    )
                df = pd.read_excel(io.BytesIO(content), sheet_name="User Stories")
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Failed to read 'User Stories' sheet from Excel: {str(e)}")
        elif file.filename.endswith(".csv"):
            df = pd.read_csv(io.StringIO(content.decode()))
        else:
            raise HTTPException(status_code=400, detail="Unsupported file type")

        column_map = {col.strip().lower(): col for col in df.columns}
        if "user story" not in column_map:
            raise HTTPException(
                status_code=400,
                detail=f"Column 'User Story' not found in sheet. Columns present: {list(column_map.keys())}",
            )
        column_name = column_map["user story"]
        stories = df[column_name].dropna().astype(str).tolist()
    elif user_story:
        stories = [user_story]
    else:
        raise HTTPException(status_code=400, detail="Either 'user_story' or 'file' must be provided")

    # Determine site_url: param -> env -> story fallback
    if not site_url:
        site_url = os.getenv("SITE_URL", "")
    story_url = _extract_first_url(stories[0]) if stories else ""
    if story_url:
        site_url = story_url

    ac_list: List[str] = []
    if acceptance_criteria:
        try:
            parsed = json.loads(acceptance_criteria)
            if isinstance(parsed, list):
                ac_list = [str(item) for item in parsed if str(item).strip()]
        except Exception:
            ac_list = []

    inputs_payload: list[dict] = []
    for idx, story in enumerate(stories):
        inputs_payload.append(
            {
                "jira_key": jira_key if idx == 0 else None,
                "user_story": story,
                "acceptance_criteria": ac_list if idx == 0 else [],
                "site_url": site_url,
            }
        )
    inputs_path = meta_dir / "inputs.json"
    with open(inputs_path, "w", encoding="utf-8") as f:
        json.dump(inputs_payload if len(inputs_payload) > 1 else inputs_payload[0], f, indent=2)

    # Optional: set AI model for this request
    if ai_model:
        os.environ["AI_MODEL_NAME"] = ai_model

    # ---------------- Snapshot current chroma metadata -> before_enrichment.json (FLAT objects) ----------------
    collection = get_collection(projectChromaPath ,"element_metadata")
    all_chroma_data = collection.get()
    ids = all_chroma_data.get("ids", []) or []
    metas = all_chroma_data.get("metadatas", []) or []

    all_chroma_metadatas: list[dict] = []
    for _id, m in zip(ids, metas):
        m = _normalize_chroma_meta(m, fallback_id=_id)
        if not m:
            continue

        # Normalize OCR records into your required flat shape ALWAYS
        if (m.get("type") or "").lower() == "ocr":
            page = (m.get("page_name") or "")
            label = (m.get("label_text") or m.get("get_by_text") or "")
            otype = (m.get("ocr_type") or "")
            intent_val = (m.get("intent") or "")
            uname = (m.get("unique_name") or generate_unique_name(page, label, otype, intent_val))

            elem_id = (
                m.get("element_id")
                or _id
                or m.get("ocr_id")
                or uname
            )

            all_chroma_metadatas.append(
                {
                    "page_name": page,
                    "label_text": label,
                    "get_by_text": m.get("get_by_text") or label,
                    "placeholder": m.get("placeholder") or label,
                    "ocr_type": otype,
                    "intent": intent_val,
                    "dom_matched": bool(m.get("dom_matched")) if m.get("dom_matched") is not None else False,
                    "external": bool(m.get("external")) if m.get("external") is not None else False,
                    "type": "ocr",
                    "unique_name": uname,
                    "element_id": elem_id,
                }
            )
        else:
            # Non-OCR records: already flat now
            all_chroma_metadatas.append(m)

    before_file = meta_dir / "before_enrichment.json"
    existing_before = []
    if before_file.exists():
        try:
            existing_before = json.loads(before_file.read_text(encoding="utf-8")) or []
            if not isinstance(existing_before, list):
                existing_before = []
        except Exception:
            existing_before = []

    # project_id = _resolve_active_project_id()
    latest_snapshot = _latest_upload_snapshot(project_id)

    latest_records = latest_snapshot or all_chroma_metadatas
    merged_before = _merge_metadata_records(existing_before, latest_records)
    before_file.write_text(json.dumps(merged_before, indent=2), encoding="utf-8")

    method_map_full = get_all_page_methods(pages_dir)
    project_src_dir = get_smartai_src_dir()

    results: List[dict] = []
    all_path_pages: List[str] = []
    test_file: Optional[Path] = None
    generated_test_names: list[str] = []

    # Snapshot-based update logic removed. Keep simple behavior:
    update_existing_tests = False

    category_dirs = {
        "ui": ui_tests_dir,
        "security": security_tests_dir,
        "accessibility": accessibility_tests_dir,
    }

    # ---------------- Generate per story ----------------
    story_start_index = _get_next_story_index(tests_dir)
    for offset, story in enumerate(stories, start=0):
        story_index = story_start_index + offset
        # page path selection
        if infer_pages or os.getenv("AI_INFER_PAGES", "false").lower() in ("1", "true", "yes"):
            path_pages = get_inferred_pages(story, method_map_full, openai_client)
        else:
            path_pages = _select_story_pages(story, method_map_full)

        if not path_pages:
            continue

        all_path_pages.extend(path_pages)
        sub_method_map = {p: method_map_full[p] for p in path_pages if p in method_map_full}

        # generate code by type
        if test_type == "security":
            code = generate_security_test_code_from_methods(
                story,
                sub_method_map,
                path_pages,
                site_url,
                run_folder,
                project_src_dir,
            )
        elif test_type == "accessibility":
            code = generate_accessibility_test_code_from_methods(
                story,
                sub_method_map,
                path_pages,
                site_url,
                run_folder,
                project_src_dir,
            )
        else:
            code = generate_test_code_from_methods(
                story,
                sub_method_map,
                path_pages,
                site_url,
                run_folder,
                project_src_dir,
            )

        # post-fixes
        code = _strip_module_qualified_helpers(code, method_map_full)
        code = _normalize_generated_code(code, sub_method_map, story)

        # imports
        page_method_files = sorted(pages_dir.glob("*_page_methods.py"))
        page_security_files = sorted(pages_dir.glob("*_security_methods.py"))
        page_accessibility_files = sorted(pages_dir.glob("*_accessibility_methods.py"))
        import_lines = [
            "import pytest",
            "from playwright.sync_api import sync_playwright, expect",
            "import json",
            "from pathlib import Path",
            "from lib.smart_ai import patch_page_with_smartai",
        ]
        for f in page_method_files + page_security_files + page_accessibility_files:
            import_lines.append(f"from pages.{f.stem} import *")

        grouped_tests = _split_generated_tests_by_category(code)

        # only process requested category
        categories_to_process = {test_type} if test_type in grouped_tests else set()
        for category, func_blocks in grouped_tests.items():
            if not func_blocks or category not in categories_to_process:
                continue

            target_dir = category_dirs.get(category)
            if not target_dir:
                continue

            story_slug = _infer_script_slug(story, category)
            stable_name = f"test_{story_slug}.py"

            # Path strategy
            if replace_existing:
                test_path = target_dir / stable_name
            else:
                test_path = target_dir / stable_name
                if test_path.exists():
                    suffix = 1
                    while (target_dir / f"test_{story_slug}_{suffix}.py").exists():
                        suffix += 1
                    test_path = target_dir / f"test_{story_slug}_{suffix}.py"

            processed_func_blocks: list[tuple[Optional[str], str]] = []

            project_id_str = os.environ.get("SMARTAI_PROJECT_ID")
            project_id_val = int(project_id_str) if project_id_str and project_id_str.isdigit() else 0

            with session_scope() as db:
                for func_block in func_blocks:
                    test_name_match = re.search(
                        r"^\s*def\s+(test_[a-zA-Z0-9_]+)\s*\(\s*page\s*\)\s*:",
                        func_block,
                        flags=re.MULTILINE,
                    )
                    if not test_name_match:
                        processed_func_blocks.append((None, func_block))
                        continue

                    test_name = test_name_match.group(1)
                    generated_test_names.append(test_name)

                    analysis = analyze_test_case_content(func_block)
                    analyzed_tags = analysis.get("tags") or []
                    analyzed_priority = analysis.get("priority") or "Low"

                    record = None
                    existing_markers: List[str] = []
                    existing_tags: List[str] = []
                    if project_id_val and test_name:
                        try:
                            record = (
                                db.query(TestCaseMetadata)
                                .filter(
                                    TestCaseMetadata.project_id == project_id_val,
                                    TestCaseMetadata.test_name == test_name,
                                )
                                .first()
                            )
                            if record:
                                if isinstance(record.markers, list):
                                    existing_markers = record.markers
                                if isinstance(record.tags, list):
                                    existing_tags = record.tags
                        except Exception as e:
                            print(f"Database query for metadata failed: {e}")

                    combined_tags = list(dict.fromkeys(existing_tags + analyzed_tags))
                    combined_markers = list(dict.fromkeys(existing_markers + combined_tags))

                    if project_id_val and test_name:
                        if record:
                            record.markers = combined_markers
                            record.tags = combined_tags
                            record.priority = analyzed_priority
                        else:
                            record = TestCaseMetadata(
                                project_id=project_id_val,
                                test_name=test_name,
                                markers=combined_markers,
                                tags=combined_tags,
                                priority=analyzed_priority,
                            )
                            db.add(record)

                    marker_decorators = _format_markers_as_pytest_decorators(combined_markers)
                    tag_label = ", ".join(combined_tags) if combined_tags else "none"
                    base_indent = ""
                    def_match = re.search(
                        r"^(\s*)def\s+test_[a-zA-Z0-9_]+\s*\(",
                        func_block,
                        flags=re.MULTILINE,
                    )
                    if def_match:
                        base_indent = def_match.group(1) + "    "

                    updated_func_block = func_block.rstrip() + f"\n{base_indent}# AI-analyzed tags: {tag_label} | Priority: {analyzed_priority}\n"

                    if marker_decorators:
                        processed_func_blocks.append((test_name, f"{chr(10).join(marker_decorators)}\n{updated_func_block}"))
                    else:
                        processed_func_blocks.append((test_name, updated_func_block))

            function_code = "\n\n".join(block for _, block in processed_func_blocks).strip()
            if not function_code:
                continue

            category_imports = list(import_lines)
            if category == "accessibility":
                category_imports.append("from services.accessibility_test_utils import run_accessibility_scan")

            content = "\n\n".join(category_imports + [function_code]).rstrip() + "\n"
            test_path.write_text(content, encoding="utf-8")

            results.append(
                {
                    "Prompt": f" Prompt\n\n1. {story}\nExpected: Success",
                    "auto_testcase": function_code,
                    "test_file_path": str(test_path),
                    "original_story": story,
                }
            )

            _generate_execution_script_for_category(
                category=category,
                target_dir=target_dir,
                test_file_path=test_path,
                original_story=story,
                site_url=site_url,
                import_lines=import_lines,
                method_map=method_map_full,
                ts_index=story_index,
            )

            test_file = test_path

    # ---------------- Logs + defaults + persistence ----------------
    log_idx = next_index(logs_dir, "logs_{}.log")
    log_file = logs_dir / f"logs_{log_idx}.log"
    if all_path_pages:
        log_file.write_text("\n".join(all_path_pages), encoding="utf-8")
    else:
        log_file.write_text("No stories were processed.", encoding="utf-8")

    create_default_test_data(run_folder, method_map_full=method_map_full, test_data_json=test_data_json)
    _persist_directory_to_db(run_folder, tests_dir)

    unique_generated = list(dict.fromkeys(generated_test_names))

    return {
        "results": results,
        "test_file": str(test_file) if test_file else "",
        "log_file": str(log_file),
        "updated_tests": unique_generated,
    }


# ----------------------------------------------------------------------
# Runner generator
# ----------------------------------------------------------------------
def _generate_execution_script_for_category(
    category: str,
    target_dir: Path,
    test_file_path: Path,
    original_story: str,
    site_url: Optional[str],
    import_lines: List[str],
    method_map: dict,
    ts_index: int,
) -> Optional[Path]:
    lines = test_file_path.read_text(encoding="utf-8").splitlines(True)
    func_blocks: List[tuple[str, str, List[str]]] = []
    current_name: Optional[str] = None
    current_body: List[str] = []
    current_markers: List[str] = []
    pending_markers: List[str] = []
    in_function = False

    def _commit_current():
        nonlocal current_name, current_body, current_markers, in_function
        if current_name and current_body:
            func_blocks.append((current_name, "".join(current_body), current_markers))
        current_name = None
        current_body = []
        current_markers = []
        in_function = False

    for line in lines:
        m = re.match(r"^\s*def (test_[a-zA-Z0-9_]+)\(page\):", line)
        marker_match = re.match(r"^\s*@pytest\.mark\.([a-zA-Z0-9_]+)\s*$", line)
        if in_function:
            if marker_match or m:
                _commit_current()
                if marker_match:
                    pending_markers.append(marker_match.group(1))
                    continue
                if m:
                    current_name = m.group(1)
                    current_body = []
                    current_markers = pending_markers
                    pending_markers = []
                    in_function = True
                    continue
            current_body.append(line)
            continue
        if marker_match:
            pending_markers.append(marker_match.group(1))
            continue
        if m:
            current_name = m.group(1)
            current_body = []
            current_markers = pending_markers
            pending_markers = []
            in_function = True
            continue

    if current_name and current_body:
        func_blocks.append((current_name, "".join(current_body), current_markers))
    if not func_blocks:
        return None

    story_text = original_story or ""
    storage_override_js = None
    try:
        m = re.search(r'storage state\s*"([^"]+)"', story_text, re.I)
        if m:
            storage_override_js = json.dumps(m.group(1))
    except Exception:
        storage_override_js = None

    wrapper_blocks: List[str] = []
    has_markers = any(markers for _, _, markers in func_blocks)
    story_slug = _infer_script_slug(original_story, category)
    ts_id = f"TS_{ts_index:03d}"
    run_tag_map: dict[str, list[str]] = {}
    runner_names: list[str] = []
    for idx, (func_name, func_body, func_markers) in enumerate(func_blocks, start=1):
        base_name = func_name.replace("test_", "")
        tc_id = f"TC_{idx:03d}"
        runner_name = f"{ts_id}_{tc_id}_{story_slug}_{base_name}"
        runner_names.append(runner_name)
        run_tag_map[runner_name] = [
            str(marker).strip().lower()
            for marker in (func_markers or [])
            if str(marker).strip()
        ]

        dedented = textwrap.dedent(func_body)
        step_lines = ["        " + l if l.strip() else "" for l in dedented.strip("\n").splitlines()]
        if category == "accessibility":
            step_lines.append("        run_accessibility_scan(page)")
        steps = "\n".join(step_lines)

        ai_summary = ""
        ai_match = re.search(r"#\s*AI-analyzed tags:\s*(.*?)\s*\|\s*Priority:\s*([A-Za-z]+)", func_body)
        if ai_match:
            tags_value = ai_match.group(1).strip()
            priority_value = ai_match.group(2).strip()
            ai_summary = f"# AI-analyzed tags: {tags_value} | Priority: {priority_value}"
        elif func_markers:
            ai_summary = f"# AI-analyzed tags: {', '.join(func_markers)} | Priority: Low"

        marker_prefix = ""
        if func_markers:
            marker_decorators = _format_markers_as_pytest_decorators(func_markers)
            if marker_decorators:
                marker_prefix = "\n".join(marker_decorators) + "\n"
        if ai_summary:
            marker_prefix += f"{ai_summary}\n"

        # Rewrite helper calls back to imported page-level functions
        helper_names = set()
        for methods in (method_map or {}).values():
            for method_def in methods:
                name = method_def.split("(", 1)[0].replace("def ", "").strip()
                if name:
                    helper_names.add(name)

        for helper_name in helper_names:
            pattern = rf"(?<!\w)page\.{re.escape(helper_name)}\((.*?)\)"

            def repl(match, helper_name=helper_name):
                args_raw = match.group(1).strip()
                if not args_raw:
                    new_args = "page"
                elif args_raw.startswith("page"):
                    new_args = args_raw
                else:
                    new_args = f"page, {args_raw}"
                return f"{helper_name}({new_args})"

            steps = re.sub(pattern, repl, steps)
            module_pattern = rf"(?:[a-zA-Z_][a-zA-Z0-9_]*\.)+{re.escape(helper_name)}\("
            steps = re.sub(module_pattern, f"{helper_name}(", steps)

        if storage_override_js:
            storage_snippet = f"""        try:
            context = browser.new_context(storage_state={storage_override_js})
            page = context.new_page()
            print(f"[{category}_runner] Restored storage_state from provided path")
        except Exception as e:
            print(f"[{category}_runner] Failed to restore provided storage_state: {{e}}")
            context = browser.new_context()
            page = context.new_page()
"""
        else:
            storage_snippet = f"""        # Attempt to restore cookies / localStorage from a Playwright storage_state file.
        # Priority: auth/storage.json
        storage_file = None
        project_root = os.getenv("SMARTAI_PROJECT_DIR", "").strip()
        if not project_root:
            for parent in _Path(__file__).resolve().parents:
                if parent.name == "generated_runs":
                    project_root = str(parent.parent)
                    break
        if project_root and not os.getenv("SMARTAI_PROJECT_DIR", "").strip():
            os.environ["SMARTAI_PROJECT_DIR"] = project_root
        if project_root:
            candidate = _Path(project_root) / "auth" / "storage.json"
            if candidate.exists():
                storage_file = candidate

        if storage_file and storage_file.exists():
            try:
                context = browser.new_context(storage_state=str(storage_file))
                page = context.new_page()
                print(f"[{category}_runner] Restored storage_state from: {{storage_file}}")
            except Exception as e:
                print(f"[{category}_runner] Failed to restore storage_state: {{e}}")
                context = browser.new_context()
                page = context.new_page()
        else:
            expected = ""
            if project_root:
                expected = str(_Path(project_root) / "auth" / "storage.json")
            print(f"[{category}_runner] No storage_state file found. Expected: {{expected}}")
            context = browser.new_context()
            page = context.new_page()
"""

        goto_target = ""
        try:
            goto_target = (site_url or "").strip()
            if not goto_target:
                goto_target = os.getenv("SITE_URL", "").strip()
        except Exception:
            goto_target = ""

        goto_line = ""
        auth_guard = (
            "        page.wait_for_load_state(\"domcontentloaded\")\n"
            "        _dismiss_cookie_banner(page)\n"
            "        auth_landing = os.getenv(\"SMARTAI_AUTH_LANDING_URL\", \"\").strip()\n"
            "        if not auth_landing:\n"
            "            project_root = os.getenv(\"SMARTAI_PROJECT_DIR\", \"\").strip()\n"
            "            if not project_root:\n"
            "                for parent in _Path(__file__).resolve().parents:\n"
            "                    if parent.name == \"generated_runs\":\n"
            "                        project_root = str(parent.parent)\n"
            "                        break\n"
            "            if project_root:\n"
            "                landing_file = _Path(project_root) / \"auth\" / \"landing_url.txt\"\n"
            "                try:\n"
            "                    if landing_file.exists():\n"
            "                        auth_landing = landing_file.read_text(encoding=\"utf-8\").strip()\n"
            "                except Exception:\n"
            "                    auth_landing = \"\"\n"
            "        try:\n"
            "            current_url = page.url or \"\"\n"
            "        except Exception:\n"
            "            current_url = \"\"\n"
            "        if auth_landing:\n"
            "            page.goto(auth_landing)\n"
            "            page.wait_for_load_state(\"domcontentloaded\")\n"
            "            _dismiss_cookie_banner(page)\n"
            "            try:\n"
            "                current_url = page.url or \"\"\n"
            "            except Exception:\n"
            "                current_url = \"\"\n"
            "        if current_url and any(k in current_url.lower() for k in (\"login\", \"signin\", \"sign-in\", \"auth\")):\n"
            "            raise RuntimeError(\n"
            "                \"Session not authenticated (still on login page). \"\n"
            "                \"Refresh auth storage and re-run.\"\n"
            "            )\n"
        )
        if goto_target and not re.search(r"page\.goto\(", steps):
            goto_literal = json.dumps(goto_target)
            goto_line = (
                f"        page.goto({goto_literal})\n"
                f"{auth_guard}"
            )
        elif re.search(r"page\.goto\(", steps):
            def _inject_guard(match: re.Match[str]) -> str:
                indent = match.group(1) or ""
                guard = auth_guard.replace("        ", indent)
                return match.group(0) + guard

            steps = re.sub(
                r"(^[ \t]*)page\.goto\([^\n]*\)\n",
                _inject_guard,
                steps,
                count=1,
                flags=re.MULTILINE,
            )

        runner_block = f"""{marker_prefix}def {runner_name}():
    import time
    import os
    from pathlib import Path as _Path
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=300)

{storage_snippet}
        _attach_page_helpers(page)
        # Patch SmartAI
        metadata_path = _SRC_ROOT / "metadata" / "after_enrichment.json"
        with open(metadata_path, "r") as f:
            actual_metadata = json.load(f)
{goto_line}        patch_page_with_smartai(page, actual_metadata)
{steps}
        time.sleep(3)
        browser.close()

"""
        wrapper_blocks.append(runner_block)

    page_imports = "\n".join([ln for ln in import_lines if ln.startswith("from pages.")])
    extra_imports = ""
    if category == "accessibility":
        extra_imports = "from services.accessibility_test_utils import run_accessibility_scan\n"
    pytest_import = "import pytest\n" if has_markers else ""

    header = f"""# Auto-generated {category} runner
import sys
import os
import re
from pathlib import Path as _Path

# Ensure src is on sys.path
_SCRIPT_PATH = _Path(__file__).resolve()
_ENV_SRC = os.getenv("SMARTAI_SRC_DIR", "").strip()
if _ENV_SRC:
    _SRC_ROOT = _Path(_ENV_SRC).resolve()
else:
    _SRC_ROOT = None
    for _parent in _SCRIPT_PATH.parents:
        if _parent.name == "src":
            _SRC_ROOT = _parent
            break
    if _SRC_ROOT is None:
        _SRC_ROOT = _SCRIPT_PATH.parents[2]

if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

# Add backend root to sys.path
_ENV_BACKEND = os.getenv("SMARTAI_BACKEND_ROOT", "").strip()
if _ENV_BACKEND:
    _BACKEND_ROOT = _Path(_ENV_BACKEND).resolve()
else:
    _BACKEND_ROOT = None
    for _parent in _SCRIPT_PATH.parents:
        if _parent.name == "backend":
            _BACKEND_ROOT = _parent
            break
    if _BACKEND_ROOT is None:
        _BACKEND_ROOT = _SCRIPT_PATH.parents[7]

if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from playwright.sync_api import sync_playwright
{pytest_import}import json
import inspect
import functools
from pathlib import Path
{page_imports}
{extra_imports}from lib.smart_ai import patch_page_with_smartai
from lib.allure_runtime import run_allure_case

def _attach_page_helpers(target_page):
    for name, helper in globals().items():
        if not inspect.isfunction(helper):
            continue
        module = getattr(helper, "__module__", "")
        if not module.startswith("pages."):
            continue
        if name.startswith("_"):
            continue
        if hasattr(target_page, name):
            continue
        setattr(target_page, name, functools.partial(helper, target_page))

def _dismiss_cookie_banner(page):
    selectors = [
        "text=/^Accept all$/i",
        "text=/^Accept$/i",
        "text=/^I agree$/i",
        "text=/^Agree$/i",
        "text=/^Close$/i",
        "text=/^Not now$/i",
        "text=/^Skip$/i",
        "text=/^No thanks$/i",
        "button:has-text('Accept all')",
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('Close')",
        "button:has-text('Not now')",
        "button:has-text('Skip')",
        "button:has-text('No thanks')",
        "button:has-text('×')",
        "button[aria-label*='close' i]",
        "[aria-label*='accept' i]",
        "[aria-label='close']",
        "[aria-label*='close' i]",
        "[class*='modal' i] [class*='close' i]",
        "[class*='login' i] [class*='close' i]",
        "span:has-text('×')",
        "svg[aria-label*='close' i]",
        "[id*='cookie' i] button",
        "[class*='cookie' i] button",
        "[data-testid*='cookie' i] button",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1000):
                locator.click(timeout=1000)
                return True
        except Exception:
            continue
    return False

"""

    run_tag_map_block = f"RUN_TAGS = {json.dumps(run_tag_map, indent=4)}\n\n"

    main_block = "\nif __name__ == '__main__':\n"
    main_block += "    import sys\n"
    main_block += "    import os\n"
    main_block += "    selected_tags = {t.strip().lower() for t in os.getenv('SMARTAI_RUN_TAGS', '').split(',') if t.strip()}\n"
    main_block += "    selected_names = {n.strip() for n in os.getenv('SMARTAI_RUN_FUNCTIONS', '').split(',') if n.strip()}\n"
    main_block += "    def _should_run(name):\n"
    main_block += "        if selected_names:\n"
    main_block += "            return name in selected_names\n"
    main_block += "        if not selected_tags:\n"
    main_block += "            return True\n"
    main_block += "        return any(tag in selected_tags for tag in RUN_TAGS.get(name, []))\n"
    main_block += "    failures = 0\n"

    for runner_name in runner_names:
        main_block += (
            f"    if _should_run('{runner_name}'):\n"
            f"        try:\n"
            f"            print(f'\\n[{category}_runner] Running test: {runner_name}...\\n')\n"
            f"            run_allure_case('{runner_name}', {runner_name})\n"
            f"            print(f'\\n[{category}_runner] {runner_name}: PASS\\n')\n"
            f"        except Exception as exc:\n"
            f"            failures += 1\n"
            f"            print(f'\\n[{category}_runner] {runner_name}: FAIL\\nDetails: {{exc}}\\n')\n"
            f"    else:\n"
            f"        print(f'\\n[{category}_runner] Skipping {runner_name} (tag filter)\\n')\n"
        )

    main_block += (
        "\n    if failures > 0:\n"
        "        print(f'\\n[" + category + "_runner] Summary: {failures} test(s) failed.')\n"
        "        sys.exit(1)\n"
        "    else:\n"
        "        print(f'\\n[" + category + "_runner] Summary: All tests passed.')\n"
        "        sys.exit(0)\n"
    )

    base_slug = _infer_script_slug(original_story, category)
    if category in {"ui", "security", "accessibility"}:
        script_name = f"{category}_script_{base_slug}.py"
        if (target_dir / script_name).exists():
            suffix = 1
            while (target_dir / f"{category}_script_{base_slug}_{suffix}.py").exists():
                suffix += 1
            script_name = f"{category}_script_{base_slug}_{suffix}.py"
    else:
        script_idx = next_index(target_dir, f"{category}_script_{{}}.py")
        script_name = f"{category}_script_{script_idx}.py"
    script_path = target_dir / script_name

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(run_tag_map_block)
        for block in wrapper_blocks:
            f.write(block)
        f.write(main_block)

    print(f"{script_name} generated with {len(wrapper_blocks)} runner(s) in {target_dir}")
    return script_path
