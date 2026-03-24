from fastapi import APIRouter, Depends, Query
from pathlib import Path
import re
import json
import os

from requests import Session
from services.test_generation_utils import runtime_collection, filter_all_pages
from utils.match_utils import normalize_page_name
from utils.project_context import (
    filter_metadata_by_project,
    set_current_project_id,
    reset_current_project_id,
)
from utils.smart_ai_utils import ensure_smart_ai_module
from database.project_storage import DatabaseBackedProjectStorage
from contextlib import contextmanager
from .projects_api import _ensure_project_structure, get_current_user, get_user_project
from database.models import User
from database.session import get_db

router = APIRouter()

@contextmanager
def _temporary_project_env(project_paths: dict, project_id: int):
    previous = {
        "SMARTAI_PROJECT_DIR": os.environ.get("SMARTAI_PROJECT_DIR"),
        "SMARTAI_SRC_DIR": os.environ.get("SMARTAI_SRC_DIR"),
        "SMARTAI_CHROMA_PATH": os.environ.get("SMARTAI_CHROMA_PATH"),
        "SMARTAI_PROJECT_ID": os.environ.get("SMARTAI_PROJECT_ID"),
    }
    token = set_current_project_id(project_id)
    os.environ["SMARTAI_PROJECT_DIR"] = project_paths["project_root"]
    os.environ["SMARTAI_SRC_DIR"] = project_paths["src_dir"]
    os.environ["SMARTAI_CHROMA_PATH"] = project_paths["chroma_path"]
    os.environ["SMARTAI_PROJECT_ID"] = str(project_id)
    try:
        yield
    finally:
        reset_current_project_id(token)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def safe(s: str) -> str:
    return re.sub(r'\W+', '_', (s or '').lower()).strip('_') or 'element'

def ensure_unique(base_name: str, used: dict) -> str:
    name = base_name
    if name not in used:
        used[name] = 1
        return name
    used[name] += 1
    return f"{base_name}_{used[name]}"

def _load_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []
    return data if isinstance(data, list) else []

def _page_entries_from_enrichment(meta_dir: Path, prefix: str) -> dict:
    page_entries: dict = {}
    pattern = f"{prefix}_*.json"
    for f in meta_dir.glob(pattern):
        if f.name == f"{prefix}.json":
            continue
        page_key = f.stem[len(prefix) + 1 :]
        entries = _load_json_list(f)
        if not entries:
            continue
        keys = {normalize_page_name(page_key)}
        if page_key:
            keys.add(page_key)
            keys.add(page_key.lower())
        keys.discard("")
        for key in keys:
            page_entries.setdefault(key, []).extend(entries)
    if page_entries:
        return page_entries

    aggregate_path = meta_dir / f"{prefix}.json"
    aggregate_entries = _load_json_list(aggregate_path)
    for entry in aggregate_entries:
        if not isinstance(entry, dict):
            continue
        page_name = (entry.get("page_name") or entry.get("page") or "").strip()
        if not page_name:
            continue
        keys = {normalize_page_name(page_name)}
        keys.add(page_name)
        keys.add(page_name.lower())
        keys.discard("")
        for key in keys:
            page_entries.setdefault(key, []).append(entry)
    return page_entries

def _resolve_meta_dir() -> Path:
    meta_dir = Path(os.environ.get("SMARTAI_SRC_DIR", "")).resolve() / "metadata"
    if meta_dir.exists():
        return meta_dir

    repo_root = Path(__file__).resolve().parents[2]
    org_root = repo_root / "backend" / "organizations"
    candidates = []
    if org_root.exists():
        for cand in org_root.rglob("generated_runs/src/metadata"):
            try:
                if cand.exists():
                    candidates.append(cand)
            except Exception:
                continue

    if not candidates:
        return meta_dir

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except Exception:
            return 0.0

    return max(candidates, key=_mtime)

# Helper block to prepend to every page file
ASSERT_HELPER_BLOCK = """import re
from playwright.sync_api import expect
 
def _ci(s):  # case-insensitive canonical
    return (s or "").strip().lower()
 
def _digits_only(s):
    return re.sub(r"\\D+", "", (s or ""))
 
def _values_match(actual, expected):
    a = "" if actual is None else str(actual)
    e = "" if expected is None else str(expected)
    if _ci(a) == _ci(e):
        return True
    da = _digits_only(a)
    de = _digits_only(e)
    return bool(da and de and da == de)
 
def _safe_input_value(locator):
    if locator is None:
        return None
    getters = (
        lambda: locator.input_value(),
        lambda: locator.evaluate("el => el ? (el.value || el.innerText || el.textContent) : null"),
        lambda: locator.inner_text(),
    )
    for getter in getters:
        try:
            value = getter()
            if value is not None:
                return value
        except Exception:
            continue
    return None
"""

WRAPPER_BLOCK = '''
# ---- Allure step wrapper (added automatically) ----
try:
    import allure
except Exception:
    from contextlib import nullcontext
    class _AllureShim:
        def step(self, name):
            return nullcontext()
    allure = _AllureShim()

try:
    _step_prefixes = ('enter_', 'click_', 'select_', 'verify_', 'toggle_', 'hover_', 'upload_')
    for _name, _obj in list(globals().items()):
        if callable(_obj) and any(_name.startswith(p) for p in _step_prefixes):
            def _make_wrapped(f, display_name=_name):
                def _wrapped(*a, **kw):
                    try:
                        dyn = getattr(allure, 'dynamic', None)
                        param_fn = None
                        if dyn and hasattr(dyn, 'parameter'):
                            param_fn = dyn.parameter
                        elif hasattr(allure, 'parameter'):
                            param_fn = allure.parameter
                        if param_fn:
                            start_idx = 1 if len(a) and getattr(a[0], '__class__', None) and getattr(a[0].__class__, '__name__', '').lower().find('page') != -1 else 0
                            for i, val in enumerate(a[start_idx:], start=1):
                                try:
                                    param_fn(f"{display_name}_arg{i}", str(val))
                                except Exception:
                                    pass
                            for k, v in kw.items():
                                try:
                                    param_fn(str(k), str(v))
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    with allure.step(display_name):
                        return f(*a, **kw)
                return _wrapped
            globals()[_name] = _make_wrapped(_obj)
except Exception:
    pass
# ---- end wrapper ----
'''

def _assert_method_for_input(unique: str, method_name: str) -> str:
    """
    Emits: def assert_<method_name>(page, expected, timeout=...)
    """
    return (
        f"def assert_{method_name}(page, expected: str, timeout: int = 6000):\n"
        f"    locator = page.smartAI('{unique}')\n"
        f"    try:\n"
        f"        expect(locator).to_have_value(str(expected), timeout=timeout)\n"
        f"    except Exception as e:\n"
        f"        actual = _safe_input_value(locator)\n"
        f"        if not _values_match(actual, str(expected)):\n"
        f"            raise AssertionError(f\"Assertion failed for '{unique}' expecting '{{str(expected)}}' but got '{{actual}}': {{e}}\")\n"
    )

def build_method(entry: dict, used_names: dict) -> str:
    """
    Builds a simple, robust page method for a given metadata entry.
    """
    ocr_type = (entry.get("ocr_type") or "").lower()
    label_text = (entry.get("label_text") or entry.get("intent") or "element").strip()
    unique = entry.get("unique_name")

    if not unique:
        return ""

    def stem(name):
        return safe(label_text or name)

    code_blocks = []

    # Input types
    if ocr_type in ("textbox", "text", "input", "textarea", "email", "password", "date", "datepicker", "time", "timepicker"):
        fn_name = ensure_unique(f"enter_{stem('input')}", used_names)
        method_code = (
            f"def {fn_name}(page, value):\n"
            f"    page.smartAI('{unique}').fill(str(value))"
        )
        assert_code = _assert_method_for_input(unique, fn_name)
        code_blocks.extend([method_code, assert_code])

    # Select/Dropdown types
    elif ocr_type in ("select", "dropdown", "combobox"):
        fn_name = ensure_unique(f"select_{stem('option')}", used_names)
        method_code = (
            f"def {fn_name}(page, value):\n"
            f"    page.smartAI('{unique}').select_option(value)"
        )
        code_blocks.append(method_code)

    # Clickable types
    elif ocr_type in ("button", "submit", "iconbutton", "link", "anchor", "imagebutton", "checkbox", "radio", "radiogroup", "toggle", "switch", "tab", "tabpanel", "accordion", "panel", "menu", "menubar"):
        fn_name = ensure_unique(f"click_{stem(ocr_type)}", used_names)
        method_code = (
            f"def {fn_name}(page):\n"
            f"    page.smartAI('{unique}').click()"
        )
        code_blocks.append(method_code)

    # File upload
    elif ocr_type in ("file", "fileinput", "upload"):
        fn_name = ensure_unique(f"upload_{stem('file')}", used_names)
        method_code = (
            f"def {fn_name}(page, file_path):\n"
            f"    page.smartAI('{unique}').set_input_files(file_path)"
        )
        code_blocks.append(method_code)

    # Default/Fallback for verification
    else:
        fn_name = ensure_unique(f"verify_{stem('element')}_visible", used_names)
        method_code = (
            f"def {fn_name}(page):\n"
            f"    expect(page.smartAI('{unique}')).to_be_visible()"
        )
        code_blocks.append(method_code)

    return "\n\n".join(code_blocks)

@router.post("/{project_id}/rag/generate-page-methods")
def generate_page_methods( 
    project_id: int,
    pages: str | None = Query(None, description="Comma-separated page names to regenerate"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    ):
    project = get_user_project(db, project_id, current_user)
    project_paths = _ensure_project_structure(project)
    projectSrcDir = project_paths["src_dir"]
    
    run_folder = Path(projectSrcDir)
    pages_dir = run_folder / "pages"
    tests_dir = run_folder / "tests"
    meta_dir = run_folder / "metadata"
    projectChromaPath= project_paths["chroma_path"]      
    
    storage = DatabaseBackedProjectStorage(project, run_folder, db)
    with _temporary_project_env(project_paths, project.id):
        ensure_smart_ai_module(storage)
        collection = runtime_collection(projectChromaPath)
    if pages:
        target_pages = [
            normalize_page_name(p.strip())
            for p in pages.split(",")
            if p.strip()
        ]
        target_pages = [p for p in target_pages if p]
    else:
        target_pages = filter_all_pages(projectChromaPath)

    result = {}
    
    records = collection.get()
    page_entries = {}
    for meta in filter_metadata_by_project(records.get("metadatas", [])):
        original = (meta.get("page_name") or "").strip()
        keys = {normalize_page_name(original)}
        if original:
            keys.add(original)
            keys.add(original.lower())
        keys.discard("")
        for key in keys:
            page_entries.setdefault(key, []).append(meta)
    after_entries = _page_entries_from_enrichment(meta_dir, "after_enrichment")
    before_entries = _page_entries_from_enrichment(meta_dir, "before_enrichment")
    for key, entries in after_entries.items():
        page_entries.setdefault(key, []).extend(entries)
    for key, entries in before_entries.items():
        page_entries.setdefault(key, []).extend(entries)

    if not target_pages:
        target_pages = sorted(page_entries.keys())
        if not target_pages:
            target_pages = filter_all_pages(projectChromaPath)

    outdir = pages_dir
    outdir.mkdir(parents=True, exist_ok=True)

    for page in target_pages:
        entries = list(page_entries.get(page, []))
        if not entries:
            direct_file = meta_dir / f"after_enrichment_{page}.json"
            if direct_file.exists():
                entries = _load_json_list(direct_file)
        if not entries:
            direct_file = meta_dir / f"before_enrichment_{page}.json"
            if direct_file.exists():
                entries = _load_json_list(direct_file)
        if not entries:
            continue

        page_name_slug = safe(page)
        filename = outdir / f"{page_name_slug}_page_methods.py"
        
        all_methods_code = [ASSERT_HELPER_BLOCK]
        used_names = {}

        for entry in entries:
            method_code = build_method(entry, used_names)
            if method_code:
                all_methods_code.append(method_code)
        
        final_code = "\n\n".join(all_methods_code)
        final_code += "\n\n" + WRAPPER_BLOCK

        filename.write_text(final_code, encoding="utf-8")
        storage.write_file(filename.relative_to(run_folder).as_posix(), final_code, "utf-8")
        result[page] = {"filename": str(filename)}

    return result
