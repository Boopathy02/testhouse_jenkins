
# enrichment_api.py
from __future__ import annotations

import os
import json
import re
import pprint
import asyncio
import hashlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union, Tuple
from urllib.parse import urlparse, urljoin
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from chromadb import PersistentClient
from config.settings import get_chroma_path
from playwright.async_api import (
    async_playwright,
    Page,
    Browser,
    Frame,
    TimeoutError as PWTimeoutError,
)

from utils.enrichment_status import reset_enriched
from logic.manual_capture_mode import (
    extract_dom_metadata,
    match_and_update,
    set_last_match_result,
)
from utils.match_utils import normalize_page_name
from utils.project_context import filter_metadata_by_project
from utils.request_context import get_project_dir as get_request_project_dir
from utils.request_context import get_project_id as get_request_project_id
from utils.request_context import get_src_dir as get_request_src_dir
from utils.file_utils import build_standard_metadata, generate_unique_name
from utils.smart_ai_utils import get_smartai_src_dir
from database.project_storage import DatabaseBackedProjectStorage
from database.session import get_db, session_scope
from database.models import Project, User
from apis.projects_api import _ensure_project_structure, get_current_user
from utils.session_manager import (
    auth_storage_path,
    auth_landing_path,
    should_start_auth_watch,
    wait_for_login_and_save,
)
from sqlalchemy.orm import Session

# -----------------------------------------------------------------------------
# Router & DB
# -----------------------------------------------------------------------------
router = APIRouter()
_ACTIVE_STORAGE: Optional[DatabaseBackedProjectStorage] = None
# Lazy chroma accessor to avoid creating repo-level data folder before a project is active
def _get_chroma_collection():
    client = PersistentClient(path=get_chroma_path())
    return client.get_or_create_collection(
        name=os.environ.get("SMARTAI_CHROMA_COLLECTION", "element_metadata")
    )

# -----------------------------------------------------------------------------
# Runtime state
# -----------------------------------------------------------------------------
PLAYWRIGHT = None
BROWSER: Optional[Browser] = None
PAGE: Optional[Page] = None
TARGET: Optional[Union[Page, Frame]] = None
CURRENT_PAGE_NAME: str = "unknown_page"

# execution/enrichment toggles
EXECUTION_MODE: bool = False
ENRICH_UI_ENABLED: bool = False           # modal disabled by default
AUTOSCROLL_ENABLED: bool = True           # on by default for better capture
# Keep modal dwell modest; override via SMARTAI_MODAL_CAPTURE_SEC if needed
MODAL_CAPTURE_PAUSE_SEC: float = float(os.getenv("SMARTAI_MODAL_CAPTURE_SEC", "0.5"))
_AUTH_WATCH_TASK: Optional[asyncio.Task] = None

# -----------------------------------------------------------------------------
# Config (paths resolved lazily so project activation can update them)
# -----------------------------------------------------------------------------
def _src_dir() -> Path:
    request_src = get_request_src_dir()
    env_src = os.environ.get("SMARTAI_SRC_DIR")
    path = Path(request_src) if request_src else (Path(env_src) if env_src else get_smartai_src_dir())
    path.mkdir(parents=True, exist_ok=True)
    if not getattr(_src_dir, "_logged", False):
        print(f"[DEBUG] Using SRC_DIR={path} (SMARTAI_SRC_DIR={'set' if env_src else 'unset'})")
        _src_dir._logged = True
    return path


def _pages_dir() -> Path:
    env_pages = os.environ.get("SMARTAI_PAGES_DIR")
    path = Path(env_pages) if env_pages else _src_dir() / "pages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_dir() -> Path:
    env_meta = os.environ.get("SMARTAI_META_DIR")
    path = Path(env_meta) if env_meta else _src_dir() / "metadata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _debug_dir() -> Path:
    path = _src_dir() / "ocr-dom-metadata"
    path.mkdir(parents=True, exist_ok=True)
    return path


# For default cookie path: backend/apis/enrichment_api.py -> backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STORAGE_DIR = _BACKEND_ROOT / "storage"
_DEFAULT_COOKIES = _DEFAULT_STORAGE_DIR / "cookies.json"


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class LaunchRequest(BaseModel):
    url: str = Field(..., description="Target URL (scheme optional; https tried first)")
    headless: bool = True
    slow_mo: int = 80
    ignore_https_errors: bool = True
    viewport_width: int = 1400
    viewport_height: int = 900
    wait_until: str = "auto"
    user_agent: Optional[str] = None
    extra_http_headers: Optional[Dict[str, str]] = None
    http_username: Optional[str] = None
    http_password: Optional[str] = None
    nav_timeout_ms: int = 60000

    # Stability toggles (opt-in)
    apply_visual_patches: bool = False
    enable_watchdog_reload: bool = False
    disable_gpu: bool = False
    disable_pinch_zoom: bool = True

    # UI / scroll
    enable_enrichment_ui: bool = False
    enable_autoscroll: bool = True

    # Auto-enrichment
    auto_enrich: bool = True
    enrich_strategy: str = Field("mixed", description="one of: 'ocr' | 'crawl' | 'mixed'")
    crawl_max_pages: int = 10
    crawl_max_depth: int = 2
    crawl_delay_ms: int = 400
    crawl_same_origin_only: bool = True
    close_after_enrich: bool = True  # <-- close browser when auto-enrich completes

class CaptureRequest(BaseModel):
    pass

class PageNameSetRequest(BaseModel):
    page_name: str

class ExecutionModeRequest(BaseModel):
    enabled: bool

class AutoEnrichRequest(BaseModel):
    enrich_strategy: str = "mixed"
    crawl_max_pages: int = 10
    crawl_max_depth: int = 2
    crawl_delay_ms: int = 400
    crawl_same_origin_only: bool = True
    close_after_enrich: bool = True

class CrawlRequest(BaseModel):
    start_url: Optional[str] = None
    max_pages: int = 10
    max_depth: int = 2
    delay_ms: int = 400
    same_origin_only: bool = True
    close_after_enrich: bool = True

class EnrichFromUrlRequest(BaseModel):
    url: str = Field(..., description="Target URL to enrich")
    page_name: Optional[str] = None
    headless: bool = False
    slow_mo: int = 80
    wait_until: str = "auto"
    nav_timeout_ms: int = 60000
    ignore_https_errors: bool = True
    enable_autoscroll: bool = True
    close_after_enrich: bool = True

# -----------------------------------------------------------------------------
# Optional UI (kept for compatibility; disabled by default)
# -----------------------------------------------------------------------------
UI_KEYBRIDGE_JS = r"""
(() => {
  if (window === window.top) return;
  if (window._smartaiKeyBridgeInstalled) return;
  window._smartaiKeyBridgeInstalled = true;
  function isEditable(el){ if(!el) return false; const t=(el.tagName||'').toLowerCase(); return t==='input'||t==='textarea'||t==='select'||el.isContentEditable; }
  function onKey(e){ if(window._smartaiDisabled) return; if(!(e.altKey && (e.key==='q'||e.key==='Q'))) return; if(e.ctrlKey||e.metaKey) return; if(isEditable(document.activeElement)) return; try{e.preventDefault();e.stopPropagation();}catch(_){} try{window.top.postMessage({__smartai:'TOGGLE_MODAL'},'*');}catch(_){}}  
  window.addEventListener('keydown', onKey, true);
})();
"""

UI_MODAL_TOP_JS = r"""
(() => {
  if (window !== window.top) return;
  if (window._smartaiTopInstalled) return;
  window._smartaiTopInstalled = true;
  const AUTO_HIDE_MS = 1500;
  let openedOnce = false;
  function isEditable(el){ if(!el) return false; const t=(el.tagName||'').toLowerCase(); return t==='input'||t==='textarea'||t==='select'||el.isContentEditable; }
  function ensureModal(){
    if(document.getElementById('smartaiModal')) return;
    const modal=document.createElement('div');
    modal.id='smartaiModal';
    modal.style.cssText=`position:fixed;top:40%;left:50%;transform:translate(-50%,-50%);background:#fff;padding:16px;border:2px solid #000;z-index:2147483647;display:none;min-width:260px;max-width:90vw;max-height:80vh;overflow:auto;border-radius:10px;font-family:Arial,sans-serif;`;
    modal.innerHTML=`
      <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap;">
        <button id="smartai_enrich_btn">Capture All</button>
        <button id="smartai_close_btn">Close</button>
      </div>
      <div id="smartai_msg" style="margin-top:10px;font-weight:bold;text-align:center;"></div>
      <div style="margin-top:8px;color:#666;font-size:12px;text-align:center;">Tip: press <b>Alt+Q</b> to open/close</div>
    `;
    document.body.appendChild(modal);
    const hideModal=()=>{ modal.style.display='none'; };
    const showModal=()=>{ modal.style.display='block'; };
    const capture=async()=>{
      const msg=document.getElementById('smartai_msg');
      msg.style.color='blue';
      msg.textContent='Capturing metadata...';
      try{
        const res=JSON.parse(await window.smartAI_enrich()||'{}');
        if(res.status==='success'){
          msg.style.color='green';
          msg.textContent=`Captured ${res.count||0} elements`;
          setTimeout(hideModal, AUTO_HIDE_MS);
        } else {
          msg.style.color='red';
          msg.textContent=res.error||'Enrichment failed';
        }
      }catch(e){
        msg.style.color='red';
        msg.textContent='Error during enrichment';
      }
    };
    document.getElementById('smartai_enrich_btn').onclick=capture;
    document.getElementById('smartai_close_btn').onclick=hideModal;
    window.addEventListener('load', ()=>{ if(!openedOnce){ openedOnce=true; showModal(); } }, {once:true});
    window.smartaiToggleModal=()=>{ modal.style.display = (modal.style.display==='none') ? 'block' : 'none'; };
    window.smartaiShowModal=showModal;
    window.smartaiHideModal=hideModal;
  }
  function toggle(){ if(window._smartaiDisabled) return; ensureModal(); if(window.smartaiToggleModal) window.smartaiToggleModal(); }
  window.addEventListener('keydown', e=>{ if(window._smartaiDisabled) return; if(!(e.altKey && (e.key==='q'||e.key==='Q'))) return; if(e.ctrlKey||e.metaKey) return; if(isEditable(document.activeElement)) return; try{e.preventDefault();e.stopPropagation();}catch(_){ } toggle(); }, true);
  window.addEventListener('message', ev => { if(window._smartaiDisabled) return; if((ev&&ev.data||{}).__smartai==='TOGGLE_MODAL') toggle(); });
  window.smartAI_disableUI = () => { try{ window._smartaiDisabled=true; const w=document.getElementById('smartaiModal'); if(w) w.remove(); }catch(_){ } };
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded', ensureModal); else ensureModal();
})();
"""

# -----------------------------------------------------------------------------
# Optional stability JS
# -----------------------------------------------------------------------------
STABILITY_VIEWPORT_CSS_JS = r"""
(() => {
  try {
    let m=document.querySelector('meta[name="viewport"]');
    if(!m){ m=document.createElement('meta'); m.name='viewport'; document.head.appendChild(m); }
    const content=m.getAttribute('content')||'';
    const kv=new Map(content.split(',').map(s=>s.trim()).filter(Boolean).map(s=>s.split('=')));
    kv.set('width','device-width'); kv.set('initial-scale','1'); kv.set('maximum-scale','1'); kv.set('user-scalable','no');
    m.setAttribute('content', Array.from(kv.entries()).map(([k,v])=>`${k}=${v}`).join(','));
    const css=`html,body{scroll-behavior:auto!important;overscroll-behavior:none!important;} *{animation:none!important;transition:none!important;}`;
    const s=document.createElement('style'); s.textContent=css; document.head.appendChild(s);
  } catch(e) {}
})();
"""

WATCHDOG_RELOAD_JS = r"""
(() => {
  if (window._smartaiWatchdog) return;
  window._smartaiWatchdog = true;
  let blanks = 0;
  const tick = async () => {
    try {
      const body = document.body;
      const hasBox = !!(body && body.getBoundingClientRect && body.getBoundingClientRect().width>0);
      const len = (body && (body.innerText||"").trim().length) || 0;
      const looksBlank = hasBox && len === 0;
      blanks = looksBlank ? (blanks+1) : 0;
      if (blanks >= 6) { blanks = 0; location.reload(); }
    } catch(e) {}
    setTimeout(tick, 200);
  };
  setTimeout(tick, 200);
})();
"""

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _safe_log(*a):
    try: print(*a)
    except Exception: pass

def _ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

def _canonical(name: str) -> str:
    n = normalize_page_name(name or "")
    if not n: return n
    n = re.sub(r'(?i)^(page|screen|view)+', '', n)
    n = re.sub(r'(?i)(page|screen|view)+$', '', n)
    n = re.sub(r'[_\-\s]+', '_', n).strip('_')
    return n or "page"

def _file_key(name: str) -> str:
    """
    Sanitize a name for safe file paths on Windows/macOS/Linux.
    """
    base = _canonical(name or "page")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    return safe or "page"

def _ensure_dirs() -> Dict[str, Path]:
    return {"debug": _debug_dir(), "meta": _meta_dir()}


def _set_active_storage(storage: Optional[DatabaseBackedProjectStorage]) -> None:
    global _ACTIVE_STORAGE
    _ACTIVE_STORAGE = storage

@contextmanager
def _activate_project_storage(db: Session, org_id: Optional[int] = None):
    project = _get_active_project(db, org_id=org_id)
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)
    try:
        yield project, storage
    finally:
        _set_active_storage(None)

@contextmanager
def _activate_project_storage_from_scope(org_id: Optional[int] = None):
    with session_scope() as scoped_db:
        with _activate_project_storage(scoped_db, org_id=org_id) as ctx:
            yield ctx

def _persist_project_file(path: Path, content: str, encoding: str = "utf-8") -> None:
    if path.suffix.lower() == ".txt":
        return
    storage = _ACTIVE_STORAGE
    if not storage:
        return
    try:
        relative = path.relative_to(storage.base_dir)
    except ValueError:
        try:
            relative = path.relative_to(_src_dir())
        except ValueError:
            return
    storage.write_file(relative.as_posix(), content, encoding)

def _write_project_file(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.write_text(content, encoding=encoding)
    _persist_project_file(path, content, encoding)

def _project_query(db: Session, org_id: Optional[int]):
    query = db.query(Project)
    if org_id is not None:
        query = query.filter(Project.organization_id == org_id)
    return query


def _get_active_project(db: Session, org_id: Optional[int] = None) -> Project:
    request_project_id = get_request_project_id()
    if request_project_id is not None:
        project = (
            _project_query(db, org_id)
            .filter(Project.id == int(request_project_id))
            .first()
        )
        if project:
            return project

    project_id_value = os.environ.get("SMARTAI_PROJECT_ID")
    if project_id_value:
        try:
            project = (
                _project_query(db, org_id)
                .filter(Project.id == int(project_id_value))
                .first()
            )
            if project:
                return project
        except ValueError:
            pass

    project_dir = get_request_project_dir() or os.environ.get("SMARTAI_PROJECT_DIR")
    if project_dir:
        segment = Path(project_dir).name
        match = re.match(r"(?P<id>\d+)-", segment)
        if match:
            candidate_id = int(match.group("id"))
            project = (
                _project_query(db, org_id)
                .filter(Project.id == candidate_id)
                .first()
            )
            if project:
                os.environ["SMARTAI_PROJECT_ID"] = str(project.id)
                return project

        normalized_slug = Project.normalized_key(segment.replace("-", " ").replace("_", " "))
        project = (
            _project_query(db, org_id)
            .filter(Project.project_key == normalized_slug)
            .order_by(Project.created_at.desc())
            .first()
        )
        if project:
            os.environ["SMARTAI_PROJECT_ID"] = str(project.id)
            return project

    raise HTTPException(
        status_code=400,
        detail="Active project not found in database. Activate a project before enriching.",
    )

def _same_origin(a: Optional[str], b: Optional[str]) -> bool:
    pa, pb = urlparse(a or ""), urlparse(b or "")
    return (pa.netloc or "").lower() != "" and (pa.netloc or "").lower() == (pb.netloc or "").lower()


def _resolve_storage_file(env_val: Optional[str] = None) -> Path:
    """
    Accept a file path or a directory; if directory, auto-append cookies.json.
    Priority:
      0) auth/storage.json (project-level auth storage)
      1) UI_STORAGE_FILE env (if set)
      2) SMARTAI_STORAGE_FILE env (if set)
      3) backend/storage/cookies.json (default)
    """
    raw = (env_val or "").strip()
    if not raw:
        raw = os.getenv("UI_STORAGE_FILE", "").strip() or os.getenv("SMARTAI_STORAGE_FILE", "").strip()
    if not raw:
        project_root = os.getenv("SMARTAI_PROJECT_DIR", "").strip()
        if project_root:
            candidate = auth_storage_path(Path(project_root))
            if candidate.exists():
                return candidate.resolve()
    if raw:
        p = Path(raw)
        if p.exists() and p.is_dir():
            return (p / "cookies.json").resolve()
        return p.resolve()
    return _DEFAULT_COOKIES.resolve()

def _ocr_name_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    try:
        recs = _get_chroma_collection().get() or {}
        for m in filter_metadata_by_project(recs.get("metadatas") or []):
            pn = (m or {}).get("page_name")
            if not pn: continue
            c = _canonical(pn)
            if not c: continue
            counts[c] = counts.get(c, 0) + 1
    except Exception:
        pass
    return counts

def _available_pages_for_dropdown() -> List[str]:
    counts = _ocr_name_counts()
    noise = {"unknown", "unknown_page", "basepage"}
    for k in list(counts.keys()):
        if k in noise: counts.pop(k, None)
    names = list(counts.keys())
    names.sort(key=lambda n: (-counts[n], n))
    return names

def _short_hash(s: str) -> str:
    try: return hashlib.md5((s or '').encode('utf-8')).hexdigest()[:8]
    except Exception: return "00000000"

def _clean_label_text(label: str) -> str:
    """
    Remove special characters from label_text to avoid noisy unique names.
    """
    if not label:
        return label
    return re.sub(r"[^A-Za-z0-9\s]", "", label).strip()

def _slug_from_url(source_url: Optional[str]) -> Optional[str]:
    """
    Derive a friendly slug from the URL path for naming output files.
    Example: https://site/app/customers -> "customers".
    """
    if not source_url:
        return None
    try:
        parsed = urlparse(source_url)
        path = (parsed.path or "").strip("/")
        if not path:
            return None
        # use last non-empty segment
        for segment in reversed(path.split("/")):
            if segment:
                return _canonical(segment)
        return None
    except Exception:
        return None

def _url_hash(source_url: Optional[str]) -> Optional[str]:
    """
    Stable hash for a URL (netloc + path) to split metadata per reachable variant.
    """
    if not source_url:
        return None
    try:
        parsed = urlparse(source_url)
        key = (parsed.netloc or "") + (parsed.path or "")
    except Exception:
        key = source_url
    try:
        return hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    except Exception:
        return None

def _strip_dom_flags(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove dom_matched flag and any transient match-only fields before persisting.
    Also ensure bbox is present in {x,y,width,height} form for downstream consumers.
    """
    cleaned = dict(rec or {})
    cleaned.pop("dom_matched", None)

    # Normalize bbox
    bbox = cleaned.get("bbox")
    if isinstance(bbox, str):
        parts = [p.strip() for p in bbox.split(",")]
        try:
            if len(parts) == 4:
                cleaned["bbox"] = {
                    "x": float(parts[0]) if parts[0] else 0,
                    "y": float(parts[1]) if parts[1] else 0,
                    "width": float(parts[2]) if parts[2] else 0,
                    "height": float(parts[3]) if parts[3] else 0,
                }
        except Exception:
            pass
    elif isinstance(bbox, dict):
        cleaned["bbox"] = {
            "x": bbox.get("x", 0),
            "y": bbox.get("y", 0),
            "width": bbox.get("width", 0),
            "height": bbox.get("height", 0),
        }
    else:
        # If bbox missing, derive from x/y/width/height when available
        x = cleaned.get("x", 0)
        y = cleaned.get("y", 0)
        w = cleaned.get("width", 0)
        h = cleaned.get("height", 0)
        if any([x, y, w, h]):
            cleaned["bbox"] = {"x": x, "y": y, "width": w, "height": h}

    return cleaned

def _coerce_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value
    if s[0] in "{[" and s[-1] in "]}":
        try:
            return json.loads(s)
        except Exception:
            try:
                return json.loads(s.replace("'", "\""))
            except Exception:
                return value
    return value

def _normalize_metadata_json(rec: Dict[str, Any]) -> Dict[str, Any]:
    cleaned = dict(rec or {})
    for key in ("bbox", "position_relation", "used_in_tests"):
        if key in cleaned:
            cleaned[key] = _coerce_json_value(cleaned.get(key))
    return cleaned


def _is_container_stub(rec: Dict[str, Any]) -> bool:
    """
    Detect obvious page-wide container records that should be skipped.
    """
    tag = (rec.get("tag_name") or rec.get("tag") or "").lower()
    dom_id = (rec.get("dom-id") or rec.get("dom_id") or rec.get("id") or "").lower()
    bbox = rec.get("bbox")
    if isinstance(bbox, str):
        try:
            parsed = json.loads(bbox.replace("'", "\""))
            bbox = parsed if isinstance(parsed, dict) else {}
        except Exception:
            bbox = {}
    if not isinstance(bbox, dict):
        bbox = {}
    w = float(bbox.get("width", 0) or 0)
    h = float(bbox.get("height", 0) or 0)

    looks_fullpage = w >= 1200 and h >= 800
    too_large = w >= 0.8 * 2000 and h >= 0.8 * 2000  # guard very large

    if tag == "body":
        return True
    if tag == "div" and (dom_id in {"root", "app"} or looks_fullpage or too_large):
        return True
    return False


def _is_form_wrapper(rec: Dict[str, Any]) -> bool:
    """
    Filter out wrapper text blocks that combine multiple field labels
    (e.g., 'Full Name * Email * Account type * ...').
    These are usually container divs/spans/forms, not real inputs.
    """
    tag = (rec.get("tag_name") or rec.get("tag") or "").lower()
    label = ((rec.get("label_text") or rec.get("text") or "") or "").strip()
    if not label:
        return False

    star_count = label.count("*")
    word_count = len(label.split())

    is_wrapper_tag = tag in {"div", "span", "p", "section", "article", "form"}
    has_input_semantics = (rec.get("type") or "").strip() or tag in {"input", "textarea", "select", "button"}

    # Only treat as wrapper when it's a container element with no direct input semantics
    if not is_wrapper_tag or has_input_semantics:
        return False

    # Heuristics for combined labels:
    # - many stars, or
    # - at least one star plus many words, or
    # - just very long text
    if star_count >= 2:
        return True
    if star_count >= 1 and word_count >= 8:
        return True
    if word_count >= 20:
        return True

    return False


def _split_label_segments(label: str) -> List[str]:
    if not label:
        return []
    parts = re.split(r"[\n\r]+|\s{2,}", label)
    cleaned: List[str] = []
    for part in parts:
        segment = part.strip(" *:\t")
        if segment:
            cleaned.append(segment)
    return cleaned


def _has_other_record_with_norm(recs: List[Dict[str, Any]], norm: str, current_index: int) -> bool:
    for idx, candidate in enumerate(recs):
        if idx == current_index:
            continue
        cand_label = _norm_text(candidate.get("label_text") or candidate.get("text") or "")
        if cand_label == norm:
            return True
    return False


def _is_combined_label(rec: Dict[str, Any], recs: List[Dict[str, Any]], current_index: int) -> bool:
    label = (rec.get("label_text") or rec.get("text") or "") or ""
    segments = [seg for seg in _split_label_segments(label) if _norm_text(seg)]
    if len(segments) < 2:
        return False
    normalized_segments = [_norm_text(seg) for seg in segments if _norm_text(seg)]
    if len(normalized_segments) < 2:
        return False
    for norm in normalized_segments:
        if not _has_other_record_with_norm(recs, norm, current_index):
            return False
    return True


def _filter_secondary_labels(recs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    If a strong form control (input/select/etc.) exists for a label, drop the accompanying
    label-only or wrapper entries with the same normalized label.
    """
    primaries: set[str] = set()
    for r in recs:
        lbl = _norm_text(r.get("label_text") or r.get("text") or "")
        tag = (r.get("tag_name") or r.get("tag") or "").lower()
        ocr_type = (r.get("ocr_type") or "").lower()
        primary_hint = tag in {"input", "select", "textarea", "option"} or ocr_type in {
            "textbox", "text", "input", "textarea", "email", "password",
            "select", "dropdown", "combobox", "checkbox", "radio", "toggle",
            "switch", "date", "datepicker", "time", "timepicker", "file", "upload"
        }
        if primary_hint and lbl:
            primaries.add(lbl)

    filtered: List[Dict[str, Any]] = []
    for idx, r in enumerate(recs):
        lbl = _norm_text(r.get("label_text") or r.get("text") or "")
        tag = (r.get("tag_name") or r.get("tag") or "").lower()
        ocr_type = (r.get("ocr_type") or "").lower()
        is_primary = tag in {"input", "select", "textarea", "option"} or ocr_type in {
            "textbox", "text", "input", "textarea", "email", "password",
            "select", "dropdown", "combobox", "checkbox", "radio", "toggle",
            "switch", "date", "datepicker", "time", "timepicker", "file", "upload"
        }
        is_wrapper_tag = tag in {"div", "span", "p", "section", "article"}
        if not is_primary and is_wrapper_tag and _is_combined_label(r, recs, idx):
            continue
        if lbl in primaries and not is_primary:
            continue
        filtered.append(r)
    return filtered

def _output_path_for_page(page_name: str, source_url: Optional[str]) -> Path:
    meta_dir = _ensure_dirs()["meta"]
    page_key = _file_key(page_name or "page")
    url_slug = _slug_from_url(source_url)
    filename_key = _file_key(url_slug or page_key)
    return meta_dir / f"{filename_key}.json"

def _ocr_only_path_for_page(page_name: str, source_url: Optional[str] = None) -> Path:
    meta_dir = _ensure_dirs()["meta"]
    page_key = _file_key(page_name or "page")
    url_slug = _slug_from_url(source_url)
    filename_key = _file_key(url_slug or page_key)
    return meta_dir / f"after_enrichment_{filename_key}.json"

def _is_page_filtered_file(path: Path) -> bool:
    name = path.name
    if name == "after_enrichment.json":
        return False
    if name.startswith("after_enrichment_"):
        return False
    return name.endswith(".json")

def _page_from_after_enrichment_file(path: Path) -> Optional[str]:
    name = path.name
    if not name.startswith("after_enrichment_") or not name.endswith(".json"):
        return None
    page = name[len("after_enrichment_") : -len(".json")]
    return _canonical(page)

def _write_filtered_aggregate(meta_dir: Path, current_payload: List[Dict[str, Any]]) -> None:
    """
    Build after_enrichment.json containing only records that have an ocr_type.
    Aggregates across per-page filtered files named by page.
    """
    def _with_ocr_type(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [r for r in (items or []) if (r.get("ocr_type") or "").strip()]

    aggregate: List[Dict[str, Any]] = []
    aggregate.extend(_with_ocr_type(current_payload))

    try:
        # Aggregate from per-page after_enrichment_*.json files
        for f in sorted(meta_dir.glob("after_enrichment_*.json")):
            if f.name == "after_enrichment.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8") or "[]")
                if isinstance(data, list):
                    aggregate.extend(_with_ocr_type(data))
            except Exception:
                continue
    except Exception:
        pass

    aggregate = _merge_enrichment_records([], aggregate)
    aggregate = [
        _normalize_metadata_json(_strip_dom_flags(r))
        for r in aggregate
        if (r.get("ocr_type") or "").strip()
    ]
    _write_project_file(meta_dir / "after_enrichment.json", json.dumps(aggregate, indent=2), encoding="utf-8")

def _merge_enrichment_records(existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge existing + incoming metadata while:
      - Not merging distinct controls that just share the same label.
      - Avoiding exact duplicates of the same element across runs.
    """
    merged: List[Dict[str, Any]] = []
    index_map: Dict[tuple, int] = {}

    def _key(item: Dict[str, Any]) -> tuple:
        if not item:
            return ("", "")

        intent = (item.get("intent") or "").strip().lower()

        # 1) Prefer strong identifiers
        unique_name = (item.get("unique_name") or "").strip().lower()
        ocr_id = (item.get("ocr_id") or "").strip().lower()
        dom_id = (item.get("id") or "").strip().lower()

        if unique_name:
            return ("un|" + unique_name, intent)
        if ocr_id:
            return ("ocr|" + ocr_id, intent)
        if dom_id:
            return ("id|" + dom_id, intent)

        # 2) Fallback: label + tag + approx position
        label_norm = _norm_text(item.get("label_text") or item.get("text") or "")
        tag = (item.get("tag_name") or item.get("tag") or "").lower()

        bbox = item.get("bbox") or {}
        # bbox might be a string; try to parse if needed
        if isinstance(bbox, str):
            try:
                parsed = json.loads(bbox.replace("'", "\""))
                if isinstance(parsed, dict):
                    bbox = parsed
            except Exception:
                bbox = {}

        try:
            cx = float(bbox.get("x", 0) or 0) + float(bbox.get("width", 0) or 0) / 2.0
            cy = float(bbox.get("y", 0) or 0) + float(bbox.get("height", 0) or 0) / 2.0
        except Exception:
            cx, cy = 0.0, 0.0

        # round to reduce micro-differences between runs
        pos_key = f"{round(cx):04d}:{round(cy):04d}"

        return (f"{tag}|{label_norm}|{pos_key}", intent)

    # Seed with existing
    for entry in existing or []:
        k = _key(entry)
        index_map[k] = len(merged)
        merged.append(entry)

    # Merge/append incoming
    for entry in incoming or []:
        k = _key(entry)
        if k in index_map:
            # same element -> overwrite with latest version
            merged[index_map[k]] = entry
        else:
            index_map[k] = len(merged)
            merged.append(entry)

    return merged

def _norm_text(s: Optional[str]) -> str:
    if not s: return ""
    return " ".join((s or "").replace("\n"," ").replace("\r"," ").strip().strip(":").split()).lower()

def _trim_label_noise(label: str) -> str:
    """
    Collapse whitespace and trim trailing price/option clutter from labels.
    Example: "Farmhouse ... Rs259 Regular New Hand Tossed Add" -> "Farmhouse ...".
    """
    if not label:
        return label
    lbl = " ".join(str(label).split())
    price_pat = re.compile(r"\s(?:rs\.?|₹|\$|€)\s*\d", re.IGNORECASE)
    m = price_pat.search(lbl)
    if m:
        lbl = lbl[: m.start()].strip()
    if len(lbl) > 100:
        lbl = lbl[:100].rsplit(" ", 1)[0].strip()
    return lbl or label

def _is_option_list_label(label: str, tag: str, role: str) -> bool:
    """
    Detect labels that are just concatenated option values (e.g., 'Basic Standard Premium').
    We drop these to avoid generating noisy select methods from option lists.
    """
    if not label:
        return False
    tag = (tag or "").lower()
    role = (role or "").lower()
    tokens = [t for t in label.split() if t]
    if len(tokens) < 3:
        return False
    if tag not in {"select"} and role not in {"combobox", "listbox"}:
        return False
    title_like = all(t[0].isupper() for t in tokens if t and t[0].isalpha())
    alpha_only = all(t.isalpha() for t in tokens)
    return bool(title_like and alpha_only)

def _standardize_dom_only(rec: Dict[str, Any], page_name: str, source_url: Optional[str]) -> Dict[str, Any]:
    bbox = rec.get("bbox") or {}
    label = rec.get("label_text") or rec.get("aria_label") or rec.get("placeholder") or rec.get("text") or ""
    label = _trim_label_noise(label)
    ocr_type = (rec.get("ocr_type") or rec.get("tag") or rec.get("role") or "").strip().lower()
    intent = (rec.get("intent") or "").strip()

    tag = (rec.get("tag") or rec.get("tag_name") or "").lower()
    role = (rec.get("role") or "").lower()
    get_by_role = (rec.get("get_by_role") or "").lower()
    data_sidebar = (rec.get("data_sidebar") or rec.get("data-sidebar") or rec.get("data-sidebar-menu") or "")
    clickable = rec.get("clickable") or rec.get("editable") or False

    # Skip noisy option-list artifacts (e.g., labels made only of option values)
    if _is_option_list_label(label, tag, role):
        return {}

    # Classification overrides
    select_like_roles = {"combobox", "listbox", "tree", "option"}
    aria_multiselectable = str(rec.get("aria_multiselectable") or rec.get("aria-multiselectable") or "").lower()
    is_select_like = (
        role in select_like_roles
        or get_by_role in select_like_roles
        or tag == "select"
        or aria_multiselectable == "true"
    )
    if is_select_like:
        ocr_type = "select"
    elif tag == "li" and clickable:
        ocr_type = "button"
    elif tag in {"button", "a"} or role in {"menuitem", "link"} or data_sidebar:
        ocr_type = "button"
    elif tag in {"input", "textarea"}:
        ocr_type = "textbox"
    elif clickable and (not ocr_type or ocr_type == "button"):
        ocr_type = "button"


    unique_name = generate_unique_name(page_name, label, ocr_type, intent)
    return {
        "page_name": page_name,
        "source_url": source_url or "",
        "label_text": label,
        "text": rec.get("text"),
        "aria_label": rec.get("aria_label"),
        "placeholder": rec.get("placeholder"),
        "title": rec.get("title"),
        "data_testid": rec.get("data_testid"),
        "tag_name": rec.get("tag"),
        "role": rec.get("role"),
        "id": rec.get("id"),
        "name": rec.get("name"),
        "type": rec.get("type"),
        "unique_name": unique_name,
        "intent": intent,
        "ocr_type": ocr_type or "unknown",
        "bbox": {
            "x": bbox.get("x", 0), "y": bbox.get("y", 0),
            "width": bbox.get("width", 0), "height": bbox.get("height", 0),
        },
        "dom_matched": False,
        "ocr_present": False,
        "ts": _ts(),
    }

def _no_elements_record(page_name: str, source_url: Optional[str]) -> Dict[str, Any]:
    return {
        "page_name": page_name,
        "source_url": source_url or "",
        "dom_matched": False,
        "ocr_present": False,
        "no_elements_found": True,
        "ts": _ts(),
    }

# -----------------------------------------------------------------------------
# Page / frame helpers
# -----------------------------------------------------------------------------
async def _is_js_accessible(fr: Union[Page, Frame]) -> bool:
    try: return await fr.evaluate("!!document && !!document.body")
    except Exception: return False

async def _pre_settle(fr: Union[Page, Frame], timeout_ms: int = 8000) -> None:
    try:
        try: await fr.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception: pass
        try: await fr.wait_for_selector("body", state="attached", timeout=timeout_ms)
        except Exception: pass
    except Exception: pass

async def _progressive_autoscroll(fr: Union[Page, Frame], steps: int = 6, pause_ms: int = 250):
    if EXECUTION_MODE or not AUTOSCROLL_ENABLED: return
    try:
        await fr.evaluate(f"""
        (async () => {{
          const sleep = t=>new Promise(r=>setTimeout(r,t));
          const doc=document; const se=doc.scrollingElement||doc.documentElement||doc.body;
          const H = se ? (se.scrollHeight||0) : (doc.documentElement.scrollHeight||doc.body.scrollHeight||0);
          const step = Math.max(1, Math.floor(H/{max(1, steps)}));
          let y=0; for(let i=0;i<{max(1, steps)};i++){{ y+=step; window.scrollTo(0,y); await sleep({max(0, pause_ms)}); }}
          await sleep({max(0, pause_ms)}); window.scrollTo(0,0);
        }})()""")
    except Exception: pass

async def _open_potential_modals(fr: Union[Page, Frame]):
    """
    Attempt to open modals by clicking elements that might trigger them.
    This is intentionally conservative to avoid long delays.
    """
    try:
        selectors = [
            'button[data-bs-toggle="modal"]',
            'button[data-toggle="modal"]',
            '[data-bs-target]',
            '[data-target]',
            '[aria-haspopup="dialog"]',
        ]
        opened_any = False
        for selector in selectors:
            try:
                elements = await fr.query_selector_all(selector)
                # Click at most 2 per selector to keep this cheap
                for el in elements[:2]:
                    try:
                        is_visible = await el.is_visible()
                        is_disabled = await el.get_attribute('disabled')
                        if is_visible and not is_disabled:
                            await el.click()
                            opened_any = True
                            # small wait, not 0.4s per element
                            await asyncio.sleep(0.2)
                    except Exception:
                        pass
            except Exception:
                pass
            # If we already opened something, don't keep hunting
            if opened_any:
                break
    except Exception:
        pass


async def _close_open_modals(fr: Union[Page, Frame], wait_ms: int = 400):
    """
    Attempt to close any open modal dialogs so navigation can continue.
    """
    try:
        # Try ESC first (many modals close on escape)
        try:
            await fr.keyboard.press("Escape")
        except Exception:
            pass

        close_selectors = [
            '[data-bs-dismiss="modal"]',
            '[aria-label="Close"]',
            '.modal button:has-text("Close")',
            '.modal button:has-text("Cancel")',
            'button:has-text("Close")',
            'button:has-text("Cancel")',
        ]
        for sel in close_selectors:
            try:
                buttons = await fr.query_selector_all(sel)
                for btn in buttons[:5]:
                    try:
                        if await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(max(0, wait_ms) / 1000)
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass

def __log_page_events(page: Page):
    page.on(
        "console",
        lambda m: _safe_log(
            f"[console:{m.type() if callable(getattr(m, 'type', None)) else getattr(m, 'type', None)}] {m.text()}"
        ),
    )
    page.on("pageerror", lambda e: _safe_log(f"[pageerror] {e}"))
    page.on("requestfailed", lambda req: _safe_log(f"[requestfailed] {req.url} -> {req.failure and req.failure.error_text}"))
    page.on("response", lambda resp: (_safe_log(f"[http {resp.status}] {resp.url}") if resp.status >= 400 else None))

async def __snapshot_if_blank(page: Page, tag: str):
    try:
        is_blank = await page.evaluate("""() => {
          const b=document.body; if(!b) return false;
          const rect=b.getBoundingClientRect(); const len=(b.innerText||"").trim().length;
          return rect && rect.width>0 && rect.height>0 && len===0;
        }""")
        if is_blank:
            try:
                dbg = _ensure_dirs()["debug"]
                path = dbg / f"blank_{tag}_{_ts()}.png"
                await page.screenshot(path=str(path), full_page=True)
                _safe_log(f"[blank-detector] Saved screenshot: {path}")
            except Exception as e:
                _safe_log(f"[blank-detector] could not write blank snapshot: {e}")
    except Exception as e:
        _safe_log(f"[blank-detector] snapshot error: {e}")

async def _dismiss_cookie_banner(page: Page) -> bool:
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
            if await locator.is_visible(timeout=1000):
                await locator.click(timeout=1000)
                return True
        except Exception:
            continue
    return False

async def _smart_navigate(page: Page, raw_url: str, wait_until: str = "auto", timeout_ms: int = 60000):
    def _with_scheme(u: str, scheme: str) -> str:
        p = urlparse(u)
        return f"{scheme}://{u}" if not p.scheme else u

    strategies = (["domcontentloaded", "load", "commit", "networkidle"] if (wait_until or "").lower()=="auto" else [wait_until])

    for scheme in ("https","http"):
        url = _with_scheme(raw_url, scheme)
        for wu in strategies:
            try:
                resp = await page.goto(url, wait_until=wu, timeout=timeout_ms)
                try: await page.wait_for_load_state("domcontentloaded", timeout=min(10000, timeout_ms))
                except Exception: pass
                try: await page.wait_for_selector("body", state="attached", timeout=5000)
                except Exception: pass
                try: await _dismiss_cookie_banner(page)
                except Exception: pass
                return resp
            except PWTimeoutError:
                continue
            except Exception:
                break
    try: await page.goto(_with_scheme(raw_url,"https"), timeout=timeout_ms)
    except Exception: pass
    try: await _dismiss_cookie_banner(page)
    except Exception: pass
    return None

async def _clean_restart():
    global PLAYWRIGHT, BROWSER, PAGE, TARGET
    try:
        if PAGE and hasattr(PAGE, "is_closed") and not PAGE.is_closed():
            await PAGE.close()
    except Exception: pass
    try:
        if BROWSER:
            await BROWSER.close()
    except Exception: pass
    try:
        if PLAYWRIGHT:
            await PLAYWRIGHT.stop()
    except Exception: pass
    PLAYWRIGHT = None; BROWSER = None; PAGE = None; TARGET = None

async def _select_extraction_target(page: Page) -> Union[Page, Frame]:
    try: await page.wait_for_selector("iframe", timeout=3000)
    except Exception: pass

    candidates: List[Union[Page, Frame]] = []
    if page.main_frame: candidates.append(page.main_frame)
    candidates.extend([f for f in page.frames if f is not page.main_frame])

    top_url = getattr(page, "url", "") or ""
    ad_like = re.compile(r"(onetag|rubicon|adnxs|doubleclick|googlesyndication|taboola|adsystem|bidswitch|pubmatic|criteo|cloudflare|googletagmanager|trustarc|consent|cookie|privacy-center)", re.I)

    async def _score_frame(fr: Union[Page, Frame]) -> Tuple[int, bool, str]:
        try:
            ok = await fr.evaluate("Boolean(document && document.body)")
            if not ok: return (-10_000, False, "")
            area = await fr.evaluate("""() => { try { const w=window.innerWidth||0, h=window.innerHeight||0; return Math.max(1,w)*Math.max(1,h); } catch { return 1; } }""")
            interactive = int(await fr.evaluate("document.querySelectorAll('input,select,textarea,button,a,[role],[contenteditable=\"true\"]').length"))
            url = ""
            try: url = fr.url or ""
            except Exception: url = ""
            same = _same_origin(top_url, url)
            score = int(area/10) + interactive*20 + (3000 if same else 0) - (8000 if ad_like.search(url) else 0)
            return (score, same, url)
        except Exception:
            return (-10_000, False, "")

    best: Union[Page, Frame] = page
    best_score = -10_000
    for fr in candidates:
        score, _, _ = await _score_frame(fr)
        if score > best_score:
            best_score = score; best = fr
    return best or page

# -----------------------------------------------------------------------------
# Derive page name & links
# -----------------------------------------------------------------------------
async def _derive_page_name(p: Page) -> str:
    try: title = (await p.title()) or ""
    except Exception: title = ""
    url = getattr(p, "url", "") or ""
    parsed = urlparse(url)
    path = (parsed.path or "/").strip("/").replace("/", "_") or "home"
    # Prefer URL-based path so distinct routes don't collapse to a shared title.
    url_piece = normalize_page_name(path)
    title_piece = normalize_page_name(title or "")
    pieces = [url_piece, title_piece] if path and path != "home" else [title_piece, url_piece]
    candidate = next((c for c in pieces if c), "unknown_page")
    return _canonical(candidate)

async def _enumerate_links(p: Page, same_origin_only: bool = True) -> List[str]:
    top = getattr(p, "url", "") or ""
    try:
        hrefs = await p.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a=>a.getAttribute('href')).filter(Boolean)""")
    except Exception:
        return []
    out = []
    for h in hrefs:
        full = urljoin(top, h)
        if same_origin_only and not _same_origin(top, full): continue
        if full.startswith("mailto:") or full.startswith("tel:"): continue
        if urlparse(full).fragment: full = full.split("#",1)[0]
        if full not in out: out.append(full)
    return out

# -----------------------------------------------------------------------------
# NEW: URL harvesting & navigation-to-page logic
# -----------------------------------------------------------------------------
def _candidate_urls_for_page(page_name: str) -> List[str]:
    """
    Collect possible URLs for a given canonical page name from ChromaDB metadatas.
    We look for common fields and rank by frequency.
    """
    can = _canonical(page_name)
    url_fields = ("source_url", "url", "page_url", "origin_url")
    freq: Dict[str, int] = {}
    try:
        recs = _get_chroma_collection().get() or {}
        for m in (recs.get("metadatas") or []):
            if _canonical((m or {}).get("page_name", "")) != can:
                continue
            for f in url_fields:
                u = (m or {}).get(f)
                if not u: continue
                u = str(u).strip()
                if not u: continue
                # ignore data URLs / mailto / tel etc.
                if u.startswith("data:") or u.startswith("mailto:") or u.startswith("tel:"):
                    continue
                freq[u] = freq.get(u, 0) + 1
    except Exception:
        pass
    # rank by count desc, then shorter path first (heuristic)
    urls = list(freq.keys())
    def _rank(u: str) -> Tuple[int, int]:
        try:
            p = urlparse(u)
            path_len = len((p.path or "").strip("/").split("/"))
        except Exception:
            path_len = 999
        return (-freq[u], path_len)
    urls.sort(key=_rank)
    return urls

async def _click_nav_element_for_tokens(p: Page, token_words: List[str], timeout_ms: int = 8000) -> bool:
    """
    Try clicking a link/button/menu whose visible text contains most of the token words.
    """
    words = [w for w in token_words if w]
    if not words: return False
    pattern = " ".join(words)
    # Try roles first for accessibility-friendly sites
    try:
        btn = p.get_by_role("link", name=re.compile(pattern, re.I))
        await btn.first.click(timeout=timeout_ms)
        return True
    except Exception:
        pass
    try:
        btn = p.get_by_role("button", name=re.compile(pattern, re.I))
        await btn.first.click(timeout=timeout_ms)
        return True
    except Exception:
        pass
    # Generic locator with :has-text()
    try:
        loc = p.locator(f"a:has-text(/{'|'.join(map(re.escape, words))}/i), button:has-text(/{'|'.join(map(re.escape, words))}/i)")
        count = await loc.count()
        if count > 0:
            await loc.first.click(timeout=timeout_ms)
            return True
    except Exception:
        pass
    return False

async def _derive_matches_name(p: Page, desired_can: str) -> bool:
    try:
        got = await _derive_page_name(p)
        return got == desired_can
    except Exception:
        return False

async def _ensure_on_page(page_name: str, same_origin_only: bool = True, nav_timeout_ms: int = 60000) -> None:
    """
    Make best effort to navigate the browser to the page corresponding to `page_name`.
    Strategy:
      1) If current page already matches canonical name -> return.
      2) Navigate to best candidate URL(s) harvested from Chroma metadata.
      3) Try clicking a nav link/button based on page-name tokens.
      4) Probe discovered links (limited BFS) looking for a page-name match.
    """
    global PAGE
    if PAGE is None:
        raise HTTPException(status_code=500, detail="No active page to navigate")

    desired_can = _canonical(page_name)

    # 1) Already there?
    try:
        if await _derive_matches_name(PAGE, desired_can):
            _safe_log(f"[nav] Already on target page '{desired_can}'")
            return
    except Exception:
        pass

    base_url = getattr(PAGE, "url", None)

    # 2) Candidate URLs from metadata
    candidates = _candidate_urls_for_page(page_name)
    for u in candidates:
        if same_origin_only and base_url and not _same_origin(base_url, u):
            continue
        _safe_log(f"[nav] Trying candidate URL for '{desired_can}': {u}")
        try:
            await _smart_navigate(PAGE, u, wait_until="auto", timeout_ms=nav_timeout_ms)
            await __snapshot_if_blank(PAGE, "after-candidate-url")
            if await _derive_matches_name(PAGE, desired_can):
                _safe_log(f"[nav] Matched page after direct URL: {u}")
                return
        except Exception as e:
            _safe_log(f"[nav] Candidate URL failed: {e}")

    # 3) Try clicking nav element(s) that match page-name tokens
    tokens = re.split(r"[_\-\s]+", desired_can)
    try:
        clicked = await _click_nav_element_for_tokens(PAGE, tokens, timeout_ms=min(8000, nav_timeout_ms))
        if clicked:
            try:
                await PAGE.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            await __snapshot_if_blank(PAGE, "after-click-nav")
            if await _derive_matches_name(PAGE, desired_can):
                _safe_log("[nav] Matched page after clicking nav element")
                return
    except Exception:
        pass

    # 4) Limited BFS over discovered links from current page
    try:
        links = await _enumerate_links(PAGE, same_origin_only=same_origin_only)
    except Exception:
        links = []

    queue = links[:20]  # cap exploration
    visited: Set[str] = set()
    while queue:
        u = queue.pop(0)
        if u in visited: continue
        visited.add(u)
        _safe_log(f"[nav] BFS probing: {u}")
        try:
            await _smart_navigate(PAGE, u, wait_until="auto", timeout_ms=nav_timeout_ms)
            await __snapshot_if_blank(PAGE, "after-bfs")
            if await _derive_matches_name(PAGE, desired_can):
                _safe_log(f"[nav] Matched page via BFS: {u}")
                return
        except Exception:
            continue

    _safe_log(f"[nav] ⚠️ Could not confidently navigate to page '{desired_can}'. Continuing with current page.")

# -----------------------------------------------------------------------------
# Extraction & enrichment core
# -----------------------------------------------------------------------------
def _dedupe_records(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set(); out: List[Dict[str, Any]] = []
    for r in rows or []:
        key = (
            (r.get("tag") or ""),
            (r.get("id") or ""),
            (r.get("name") or ""),
            (r.get("type") or ""),
            _norm_text(r.get("label_text") or r.get("aria_label") or r.get("placeholder") or r.get("text") or "")
        )
        if key in seen: continue
        seen.add(key); out.append(r)
    return out
async def _rich_extract_dom_metadata(fr: Union[Page, Frame]) -> List[Dict[str, Any]]:
    js = r"""
    (() => {
      const out = [];
      const seen = new Set();
      const norm = s => (s || "").replace(/\s+/g, " ").trim();

      const textOf = el => norm(el ? (el.innerText || el.textContent || "") : "");

      const isVisible = el => {
        if (!el || !el.ownerDocument) return false;
        const cs = el.ownerDocument.defaultView.getComputedStyle(el);
        if (!cs || cs.visibility === "hidden" || cs.display === "none" || parseFloat(cs.opacity || "1") < 0.01)
          return false;

        const rect = el.getBoundingClientRect();
        if (!rect || rect.width < 1 || rect.height < 1) return false;
        if (rect.bottom < 0 || rect.right < 0) return false;
        return true;
      };

      const labelForMap = doc => {
        const m = new Map();
        doc.querySelectorAll("label[for]").forEach(l => {
          const f = l.getAttribute("for");
          if (f) m.set(f, (m.get(f) || "") + " " + textOf(l));
        });
        return m;
      };

      const nearbyLabel = el => {
        try {
          if (!el) return "";
          // direct label element
          const lab = el.closest("label");
          if (lab) return textOf(lab);

          const parent = el.parentElement;
          if (parent) {
            const directLabel = parent.querySelector("label");
            if (directLabel) return textOf(directLabel);
            const labelLike = parent.querySelector("[class*='label' i], [class*='input-label' i]");
            if (labelLike) return textOf(labelLike);
          }

          const group = el.closest("[class*='input-group' i], [class*='form-group' i], [class*='field' i]");
          if (group) {
            const groupLabel = group.querySelector("label, [class*='label' i], [class*='input-label' i]");
            if (groupLabel) return textOf(groupLabel);
          }

          const prev = el.previousElementSibling;
          if (prev) {
            const prevText = textOf(prev);
            if (prevText) return prevText;
          }
        } catch (e) {}
        return "";
      };

      const accessibleName = (el, doc, _lmap) => {
        const aria = el.getAttribute && el.getAttribute("aria-label");
        if (aria) return norm(aria);

        const lb = el.getAttribute && el.getAttribute("aria-labelledby");
        if (lb) {
          const txt = lb.split(/\s+/).map(id => {
            const n = doc.getElementById(id);
            return n ? textOf(n) : "";
          }).join(" ");
          if (txt) return norm(txt);
        }

        const id = el.id || "";
        if (id && _lmap.has(id)) return norm(_lmap.get(id));

        const lab = el.closest("label");
        if (lab) return norm(textOf(lab));

        const ph = el.getAttribute && el.getAttribute("placeholder");
        if (ph) return norm(ph);

        const title = el.getAttribute && el.getAttribute("title");
        if (title) return norm(title);

        const nearby = nearbyLabel(el);
        if (nearby) return norm(nearby);

        return norm(el.innerText || el.value || el.textContent || "");
      };

      const push = r => {
        const key = JSON.stringify([
          r.tag || "", r.id || "", r.name || "", r.type || "",
          norm(r.label_text || r.aria_label || r.placeholder || r.text || "")
        ]).slice(0, 400);
        if (!seen.has(key)) {
          seen.add(key);
          out.push(r);
        }
      };

      const pick = (el, doc, _lmap, framePrefix="") => {
        if (!isVisible(el)) return;

        const tag = (el.tagName || "").toLowerCase();
        const rect = el.getBoundingClientRect();

        // Detect clickable elements
        let clickable = false;

        // Native clickable tags
        if (["button", "a", "option"].includes(tag)) clickable = true;

        // Elements with role=button/menuitem/link
        const role = el.getAttribute("role");
        if (role === "button" || role === "menuitem" || role === "link")
          clickable = true;

        // NEW: Detect clickable <li>
        if (tag === "li" || el.getAttribute("role") === "listitem") {
          const style = window.getComputedStyle(el);
          const cursorPointer = style && style.cursor === "pointer";
          const hasClickEvent =
            el.onclick ||
            el.getAttribute("onclick") ||
            el.getAttribute("role") === "button";

          if (cursorPointer || hasClickEvent) clickable = true;
        }

        const txt = accessibleName(el, doc, _lmap);

        push({
          tag,
          role: role || "",
          id: el.id || "",
          name: el.getAttribute("name") || "",
          type: el.getAttribute("type") || "",
          text: txt,
          aria_label: el.getAttribute("aria-label") || "",
          placeholder: el.getAttribute("placeholder") || "",
          label_text: txt,
          clickable: clickable,
          bbox: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          frame: framePrefix
        });
      };

      const pickText = (el, doc, _lmap, framePrefix="") => {
        if (!isVisible(el)) return;
        const tag = (el.tagName || "").toLowerCase();
        if (!/^(h1|h2|h3|h4|h5|h6|label|legend|span|strong|em|p|div)$/.test(tag))
          return;

        const txt = textOf(el);
        if (!txt || txt.length < 2) return;

        const rect = el.getBoundingClientRect();
        if (!rect || rect.width < 1 || rect.height < 1) return;

        push({
          tag,
          role: el.getAttribute("role") || "",
          id: el.id || "",
          name: el.getAttribute("name") || "",
          type: "",
          text: txt,
          label_text: txt,
          aria_label: el.getAttribute("aria-label") || "",
          placeholder: "",
          clickable: false,
          bbox: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          frame: framePrefix
        });
      };

      const visit = (root, framePrefix="") => {
        const doc = root;
        const _lmap = labelForMap(doc);
        const walker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);

        while (walker.nextNode()) {
          const el = walker.currentNode;

          pick(el, doc, _lmap, framePrefix);
          pickText(el, doc, _lmap, framePrefix);

          if (el.shadowRoot) visit(el.shadowRoot, framePrefix);
        }

        const iframes = doc.querySelectorAll("iframe");
        for (const f of iframes) {
          try {
            if (f.contentDocument)
              visit(f.contentDocument, framePrefix + "iframe/");
          } catch(e) {}
        }
      };

      visit(document);
      return out;
    })();
    """

 
    try:
        elements = await fr.evaluate(js)
        _safe_log(f"[DEBUG] Extracted {len(elements)} DOM elements (including iframes).")
        # Optional: debug grouping by frame
        counts_by_frame: Dict[str, int] = {}
        for e in elements:
            f = (e or {}).get("frame") or ""
            counts_by_frame[f] = counts_by_frame.get(f, 0) + 1
        _safe_log(f"[DEBUG] Elements grouped by frame: {counts_by_frame}")
        return elements

    except Exception as e:
        print("[ERROR] _rich_extract_dom_metadata failed:", e)
        return []
# -----------------------------------------------------------------------------
# Enrichment
# -----------------------------------------------------------------------------
async def _refresh_target(reason: str = ""):
    global PAGE, TARGET
    try:
        if PAGE is None: return
        TARGET = await _select_extraction_target(PAGE)
        _safe_log(f"[stability] TARGET refreshed ({reason}) → {getattr(TARGET,'url',None)}")
    except Exception as e:
        _safe_log(f"[stability] TARGET refresh failed ({reason}): {e}")

def _get_ocr_data_by_canonical(canonical_page_name: str) -> List[Dict[str, Any]]:
    try:
        recs = _get_chroma_collection().get() or {}
        metas = filter_metadata_by_project(recs.get("metadatas", []) or [])
        return [m for m in metas if _canonical((m or {}).get("page_name", "")) == canonical_page_name]
    except Exception:
        return []

def _assess_dom_quality(recs: List[Dict[str, Any]]) -> bool:
    if not recs: return True
    n = len(recs); labeled = 0; with_bbox = 0
    for r in recs:
        if (r.get("aria_label") or r.get("placeholder") or r.get("label") or r.get("text") or r.get("label_text")): labeled += 1
        bb = r.get("bbox") or {}
        if (bb.get("width", 0) or 0) > 0 and (bb.get("height", 0) or 0) > 0: with_bbox += 1
    return n < 5 or (labeled / max(1, n) < 0.30) or (with_bbox / max(1, n) < 0.30)

async def _run_enrichment_for(page_name: str) -> Dict[str, Any]:
    global PAGE, TARGET, CURRENT_PAGE_NAME, AUTOSCROLL_ENABLED
    if PAGE is None: raise HTTPException(status_code=500, detail="❌ Cannot extract. No active page handle.")
    if hasattr(PAGE, "is_closed") and PAGE.is_closed(): raise HTTPException(status_code=500, detail="❌ Cannot extract. Page is already closed.")
    # Ensure chroma path is available via project activation

    CURRENT_PAGE_NAME = _canonical(page_name)

    # >>> NEW: ensure we're on the correct page BEFORE extraction
    await _ensure_on_page(CURRENT_PAGE_NAME)

    await _refresh_target("enrich-start")
    paths = _ensure_dirs()

    # ensure target
    if not await _is_js_accessible(TARGET):
        try:
            if await _is_js_accessible(PAGE.main_frame):
                TARGET = PAGE.main_frame
        except Exception:
            TARGET = PAGE

    await _pre_settle(TARGET, timeout_ms=8000)
    prev_scroll = AUTOSCROLL_ENABLED
    try:
        AUTOSCROLL_ENABLED = True
        await _progressive_autoscroll(TARGET, steps=6, pause_ms=250)
    finally:
        AUTOSCROLL_ENABLED = prev_scroll

    # Attempt to open potential modals before extraction
    await _open_potential_modals(TARGET)

    # extract DOM
    # --- Diagnostic probe: record readyState, node count, and accessibility ---
    try:
        probe = {"url": getattr(TARGET, 'url', None)}
        try:
            probe["readyState"] = await TARGET.evaluate("() => document.readyState")
        except Exception as _e:
            probe["readyState"] = f"eval-error: {_e}"
        try:
            probe["body_node_count"] = await TARGET.evaluate("() => document.querySelectorAll('body *').length")
        except Exception as _e:
            probe["body_node_count"] = f"eval-error: {_e}"
        try:
            probe["is_js_accessible"] = bool(await _is_js_accessible(TARGET))
        except Exception:
            probe["is_js_accessible"] = False
        debug_path = paths["debug"] / f"dom_eval_debug_{_file_key(CURRENT_PAGE_NAME)}.json"
        _write_project_file(debug_path, json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        try:
            error_path = paths["debug"] / f"dom_eval_debug_{_file_key(CURRENT_PAGE_NAME)}_error.txt"
            _write_project_file(error_path, str(e), encoding="utf-8")
        except Exception:
            pass

    # Prefer fast, rich, single-eval extraction first
    dom_data = await _rich_extract_dom_metadata(TARGET) or []
    try:
        _ = len(dom_data)
    except Exception:
        dom_data = list(dom_data)

    # If still low-quality or empty, supplement with Playwright locator-based extraction
    if _assess_dom_quality(dom_data):
        try:
            basic = await extract_dom_metadata(TARGET, CURRENT_PAGE_NAME) or []
            if basic:
                dom_data = _dedupe_records(list(dom_data) + list(basic))
        except Exception:
            pass

    dom_data = _dedupe_records(dom_data)

    # Normalize fields for matching (include nearby_label as fallback)
    for rec in dom_data:
        try:
            # prefer explicit label_text, then nearby_label, then aria/placeholder/text
            label = rec.get("label_text") or rec.get("nearby_label") or rec.get("label") or rec.get("aria_label") or rec.get("text")
            placeholder = rec.get("placeholder"); role = rec.get("role"); tag = rec.get("tag")
            rec["_norm"] = {
                "label": _norm_text(label),
                "nearby": _norm_text(rec.get("nearby_label")),
                "placeholder": _norm_text(placeholder),
                "role": (role or "").lower(),
                "tag": (tag or "").lower(),
            }
        except Exception:
            pass

    ocr_data = _get_ocr_data_by_canonical(CURRENT_PAGE_NAME)
    # debug dumps
    _write_project_file(
        paths["debug"] / f"dom_data_{_file_key(CURRENT_PAGE_NAME)}.txt",
        pprint.pformat(dom_data),
        encoding="utf-8",
    )
    _write_project_file(paths["debug"] / f"ocr_data_{CURRENT_PAGE_NAME}.txt", pprint.pformat(ocr_data), encoding="utf-8")

    # matching
    updated_matches = match_and_update(ocr_data, dom_data, _get_chroma_collection())
    _write_project_file(paths["debug"] / f"after_match_and_update_{CURRENT_PAGE_NAME}.txt", pprint.pformat(updated_matches), encoding="utf-8")

    standardized_matches = []
    for m in (updated_matches or []):
        # 🔥 FIRST: classify DOM element (li → button, etc.)
        m = _standardize_dom_only(m, CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
        if not m:
            continue

        # 🔥 THEN: convert into final metadata schema
        m = build_standard_metadata(m, CURRENT_PAGE_NAME, image_path="", source_url=getattr(PAGE, "url", None))
        standardized_matches.append(m)

    for m in standardized_matches:
        m["label_text"] = _clean_label_text(m.get("label_text", ""))
        m["text"] = _clean_label_text(m.get("text", ""))
    standardized_matches = [m for m in standardized_matches if not _is_container_stub(m) and not _is_form_wrapper(m)]
    standardized_matches = _filter_secondary_labels(standardized_matches)
    set_last_match_result(standardized_matches)

    # fallback if nothing matched
    if not standardized_matches:
        if dom_data:
            standardized_matches = []
            for r in dom_data:
                rec = dict(r or {})
                tag = (rec.get("tag") or rec.get("tag_name") or "").lower()
                if not rec.get("ocr_type") and tag == "li":
                    rec["ocr_type"] = "button"
                # Normalize bbox so it persists correctly
                bbox = rec.get("bbox")
                if isinstance(bbox, str):
                    try:
                        parsed = json.loads(bbox.replace("'", "\""))
                        if isinstance(parsed, dict):
                            bbox = parsed
                    except Exception:
                        bbox = {}
                if isinstance(bbox, dict):
                    rec["bbox"] = {
                        "x": bbox.get("x", 0),
                        "y": bbox.get("y", 0),
                        "width": bbox.get("width", 0),
                        "height": bbox.get("height", 0),
                    }
                else:
                    rec["bbox"] = {
                        "x": rec.get("x", 0),
                        "y": rec.get("y", 0),
                        "width": rec.get("width", 0),
                        "height": rec.get("height", 0),
                    }

                # Carry over common DOM fields so metadata is not empty
                if not rec.get("tag_name") and rec.get("tag"):
                    rec["tag_name"] = rec.get("tag")
                if not rec.get("xpath"):
                    rec["xpath"] = rec.get("xpath") or rec.get("locator_xpath") or ""
                if not rec.get("get_by_text"):
                    rec["get_by_text"] = rec.get("text", "")
                if not rec.get("get_by_role"):
                    rec["get_by_role"] = rec.get("role", "")
                if not rec.get("placeholder"):
                    rec["placeholder"] = rec.get("placeholder", "")

                # Clean labels/text to strip special characters
                rec["label_text"] = _clean_label_text(rec.get("label_text", ""))
                rec["text"] = _clean_label_text(rec.get("text", ""))

                # Heuristic ocr_type from DOM role/tag
                dom_role = (rec.get("role") or "").lower()
                dom_tag = (rec.get("tag") or rec.get("tag_name") or "").lower()
                dom_get_by_role = (rec.get("get_by_role") or "").lower()
                aria_multiselectable = str(rec.get("aria_multiselectable") or rec.get("aria-multiselectable") or "").lower()
                select_like_roles = {"combobox", "listbox", "tree", "option"}
                # Treat combobox/listbox/tree-like controls as selects even if previously typed as button
                if (
                    dom_role in select_like_roles
                    or dom_get_by_role in select_like_roles
                    or dom_tag == "select"
                    or aria_multiselectable == "true"
                ):
                    # Skip pure option-list artifacts (e.g., "Basic Standard Premium")
                    if _is_option_list_label(rec.get("label_text") or rec.get("text") or "", dom_tag, dom_role):
                        continue
                    rec["ocr_type"] = "select"
                elif not rec.get("ocr_type"):
                    if dom_role in {"button", "link", "checkbox", "textbox"}:
                        rec["ocr_type"] = dom_role
                    elif dom_tag in {"button", "a"}:
                        rec["ocr_type"] = "button"
                    elif dom_tag in {"input", "textarea", "select"}:
                        rec["ocr_type"] = "textbox" if dom_tag in {"input", "textarea"} else "select"

                # Skip giant container blobs to avoid one huge element covering the page
                label_text = rec.get("label_text") or rec.get("text") or ""
                if len(label_text) > 5000:
                    continue

                rec.setdefault("dom_matched", False)
                standardized_matches.append(
                    build_standard_metadata(
                        rec,
                        page_name=CURRENT_PAGE_NAME,
                        image_path="",
                        source_url=getattr(PAGE, "url", None),
                    )
                )
            # Drop container-like elements
            standardized_matches = [m for m in standardized_matches if not _is_container_stub(m)]
            standardized_matches = [m for m in standardized_matches if not _is_form_wrapper(m)]
            standardized_matches = _filter_secondary_labels(standardized_matches)
        else:
            standardized_matches = [
                _no_elements_record(CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
            ]

    # write per-page JSON (merge with existing if present)
    out_path = _output_path_for_page(CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
    try:
        existing_payload = json.loads(out_path.read_text(encoding="utf-8") or "[]") if out_path.exists() else []
    except Exception:
        existing_payload = []
    combined_payload = _merge_enrichment_records(existing_payload, standardized_matches)
    combined_payload = [_normalize_metadata_json(_strip_dom_flags(r)) for r in combined_payload]
    _write_project_file(out_path, json.dumps(combined_payload, indent=2), encoding="utf-8")

    # write per-page OCR-only JSON using just the page name
    ocr_only_payload = [r for r in combined_payload if (r.get("ocr_type") or "").strip()]
    ocr_only_path = _ocr_only_path_for_page(CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
    _write_project_file(ocr_only_path, json.dumps(ocr_only_payload, indent=2), encoding="utf-8")

    # refresh global snapshot by aggregating per-page after_enrichment_* files,
    # keeping only records with a meaningful ocr_type
    meta_dir = paths["meta"]
    _write_filtered_aggregate(meta_dir, ocr_only_payload)

    _safe_log(f"[enrich] wrote: {out_path} ({len(standardized_matches)} records)")

    # Pause to allow modal content to be captured, then close any modals so navigation can proceed cleanly
    # Allow a brief dwell for modal capture, then force-close any open dialogs
    try:
        await asyncio.sleep(max(0.0, MODAL_CAPTURE_PAUSE_SEC))
    except Exception:
        pass
    await _close_open_modals(TARGET)
    # Final escape in case close buttons failed
    try:
        await TARGET.keyboard.press("Escape")
    except Exception:
        pass

    return {
        "status": "success",
        "message": f"Enriched {len(standardized_matches)} elements for page: {CURRENT_PAGE_NAME}",
        "matched_data": standardized_matches,
        "count": len(standardized_matches),
        "output_path": str(out_path),
    }
# -----------------------------------------------------------------------------
# Strategy engines (OCR, crawl, mixed)
# -----------------------------------------------------------------------------
async def _enrich_ocr_pages() -> Dict[str, Any]:
    pages = _available_pages_for_dropdown()
    results = []
    for pn in pages:
        try:
            _safe_log(f"[auto] OCR page -> {pn}")
            res = await _run_enrichment_for(pn)
            results.append({"page_name": pn, "count": res.get("count", 0), "file": res.get("output_path")})
        except Exception as e:
            _safe_log(f"[auto] OCR page '{pn}' failed: {e}")
            results.append({"page_name": pn, "error": str(e), "count": 0})
    # If no OCR pages exist, enrich the current page at least once
    if not pages:
        pn = await _derive_page_name(PAGE)
        _safe_log(f"[auto] No OCR pages; enriching current: {pn}")
        res = await _run_enrichment_for(pn)
        results.append({"page_name": pn, "count": res.get("count", 0), "file": res.get("output_path")})
    return {"strategy": "ocr", "results": results}

async def _crawl_and_enrich(start_url: Optional[str], max_pages: int, max_depth: int, delay_ms: int, same_origin_only: bool) -> Dict[str, Any]:
    if PAGE is None: raise HTTPException(status_code=500, detail="No active page")
    seed = start_url or getattr(PAGE, "url", None)
    if not seed: raise HTTPException(status_code=400, detail="No start URL available for crawl")

    visited: Set[str] = set()
    queue: List[Tuple[str, int]] = [(seed, 0)]
    results: List[Dict[str, Any]] = []
    count = 0

    while queue and count < max_pages:
        url, depth = queue.pop(0)
        if url in visited: continue
        visited.add(url)

        _safe_log(f"[crawl] visiting d={depth} url={url}")
        try:
            await _smart_navigate(PAGE, url, wait_until="auto", timeout_ms=60000)
            await __snapshot_if_blank(PAGE, "crawl-visit")

            global TARGET
            try: TARGET = await _select_extraction_target(PAGE)
            except Exception: TARGET = PAGE

            page_name = await _derive_page_name(PAGE)
            res = await _run_enrichment_for(page_name)
            results.append({"url": url, "page_name": page_name, "count": res.get("count", 0), "file": res.get("output_path")})
            count += 1
        except Exception as e:
            _safe_log(f"[crawl] failed {url}: {e}")
            results.append({"url": url, "error": str(e), "count": 0})

        if depth < max_depth and count < max_pages:
            try:
                links = await _enumerate_links(PAGE, same_origin_only=same_origin_only)
                for link in links:
                    if link not in visited and len(queue) + count < max_pages * 3:
                        queue.append((link, depth + 1))
            except Exception:
                pass

        if delay_ms > 0:
            await asyncio.sleep(max(0, delay_ms) / 1000.0)

    return {"strategy": "crawl", "results": results}

async def _auto_enrich(strategy: str, crawl_max_pages: int, crawl_max_depth: int, crawl_delay_ms: int, crawl_same_origin_only: bool) -> Dict[str, Any]:
    strat = (strategy or "mixed").lower().strip()
    if strat not in {"ocr", "crawl", "mixed"}: strat = "mixed"
    has_ocr_pages = bool(_available_pages_for_dropdown())

    if strat == "ocr":
        return await _enrich_ocr_pages()

    if strat == "crawl":
        return await _crawl_and_enrich(
            start_url=getattr(PAGE, "url", None),
            max_pages=crawl_max_pages, max_depth=crawl_max_depth,
            delay_ms=crawl_delay_ms, same_origin_only=crawl_same_origin_only
        )
    # mixed
    if has_ocr_pages:
        return await _enrich_ocr_pages()
    return await _crawl_and_enrich(
        start_url=getattr(PAGE, "url", None),
        max_pages=crawl_max_pages, max_depth=crawl_max_depth,
        delay_ms=crawl_delay_ms, same_origin_only=crawl_same_origin_only
    )
# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@router.post("/url/launch-browser")
async def launch_browser(
    req: LaunchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    global PLAYWRIGHT, BROWSER, PAGE, TARGET, CURRENT_PAGE_NAME, ENRICH_UI_ENABLED, AUTOSCROLL_ENABLED
    project = _get_active_project(db, org_id=current_user.organization_id)
    project_paths = _ensure_project_structure(project)
    project_root = Path(project_paths["project_root"])
    os.environ["SMARTAI_PROJECT_DIR"] = project_paths["project_root"]
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)
    try:
        await _clean_restart()
        PLAYWRIGHT = await async_playwright().start()

        launch_args = []
        if req.disable_pinch_zoom:
            launch_args += ["--disable-pinch", "--force-device-scale-factor=1", "--high-dpi-support=1", "--overscroll-history-navigation=0"]
        if req.disable_gpu:
            launch_args += ["--disable-gpu", "--disable-accelerated-2d-canvas", "--disable-features=IsolateOrigins,site-per-process"]

        headless = req.headless
        if not auth_storage_path(project_root).exists():
            headless = False
        BROWSER = await PLAYWRIGHT.chromium.launch(headless=headless, slow_mo=req.slow_mo, args=launch_args)

        context_kwargs: Dict[str, Any] = {
            "ignore_https_errors": req.ignore_https_errors,
            "viewport": {"width": req.viewport_width, "height": req.viewport_height},
            "bypass_csp": True,
            "has_touch": False,
            "device_scale_factor": 1,
            "reduced_motion": "reduce",
            "color_scheme": "light",
        }
        if req.user_agent: context_kwargs["user_agent"] = req.user_agent
        if req.extra_http_headers: context_kwargs["extra_http_headers"] = req.extra_http_headers
        if req.http_username and req.http_password:
            context_kwargs["http_credentials"] = {"username": req.http_username, "password": req.http_password}

        ENRICH_UI_ENABLED = bool(req.enable_enrichment_ui)
        AUTOSCROLL_ENABLED = bool(req.enable_autoscroll)

        # Resolve optional storage file (cookies / localStorage) and apply to context kwargs if present
        storage_file = auth_storage_path(project_root)
        if not storage_file.exists():
            storage_file = _resolve_storage_file()
        try:
            if storage_file and storage_file.exists():
                context_kwargs["storage_state"] = str(storage_file)
                _safe_log(f"[enrichment] Using storage_state from {storage_file}")
            else:
                _safe_log(f"[enrichment] No storage_state file at {storage_file}")
        except Exception:
            _safe_log(f"[enrichment] Failed to apply storage_state from {storage_file}")

        # Create browser context and page now that context_kwargs is finalized
        context = await BROWSER.new_context(**context_kwargs)
        PAGE = await context.new_page()
        __log_page_events(PAGE)

        PAGE.on("framenavigated", lambda frame: asyncio.create_task(_refresh_target("framenavigated")))
        PAGE.on("framedetached",  lambda frame: asyncio.create_task(_refresh_target("framedetached")))
        PAGE.on("crash",          lambda:       asyncio.create_task(_refresh_target("page crash")))

        async def _binding_enrich(source, page_name: Optional[str] = None):
            try:
                target_page = "page"
                globals()["CURRENT_PAGE_NAME"] = target_page
                with _activate_project_storage_from_scope(current_user.organization_id):
                    res = await _run_enrichment_for(target_page)
                return json.dumps(res)
            except HTTPException as he:
                return json.dumps({"status": "fail", "error": he.detail})
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e)})

        await PAGE.expose_binding("smartAI_enrich", _binding_enrich)

        if req.apply_visual_patches: await PAGE.add_init_script(STABILITY_VIEWPORT_CSS_JS)
        if req.enable_watchdog_reload: await PAGE.add_init_script(WATCHDOG_RELOAD_JS)
        if ENRICH_UI_ENABLED:
            await PAGE.add_init_script(UI_KEYBRIDGE_JS)
            await PAGE.add_init_script(UI_MODAL_TOP_JS)

        try:
            PAGE.set_default_timeout(req.nav_timeout_ms)
            PAGE.set_default_navigation_timeout(req.nav_timeout_ms)
        except Exception:
            pass

        await _smart_navigate(PAGE, req.url, wait_until=req.wait_until if req.wait_until else "auto", timeout_ms=req.nav_timeout_ms)
        if should_start_auth_watch(auth_storage_path(project_root), getattr(PAGE, "url", "")):
            global _AUTH_WATCH_TASK
            if _AUTH_WATCH_TASK and not _AUTH_WATCH_TASK.done():
                _AUTH_WATCH_TASK.cancel()
            _AUTH_WATCH_TASK = asyncio.create_task(wait_for_login_and_save(PAGE, project_root))
        await __snapshot_if_blank(PAGE, "after-nav")
        try:
            TARGET = await _select_extraction_target(PAGE)
        except Exception:
            TARGET = PAGE

        # Use a static page identifier for manual capture
        global CURRENT_PAGE_NAME
        CURRENT_PAGE_NAME = "page"

        auto_result = None
        if req.auto_enrich:
            _safe_log(f"[auto] Starting auto-enrichment strategy='{req.enrich_strategy}'")
            auto_result = await _auto_enrich(
                strategy=req.enrich_strategy,
                crawl_max_pages=req.crawl_max_pages,
                crawl_max_depth=req.crawl_max_depth,
                crawl_delay_ms=req.crawl_delay_ms,
                crawl_same_origin_only=req.crawl_same_origin_only,
            )
            _safe_log(f"[auto] Completed auto-enrichment with {len(auto_result.get('results', []))} item(s)")

        msg = f"✅ Browser launched and navigated to {req.url}."
        if ENRICH_UI_ENABLED: msg += " Modal available (Alt+Q)."
        if req.auto_enrich and auto_result: msg += f" Auto-enrichment finished using '{auto_result.get('strategy')}'."

        # AUTO-CLOSE when requested
        if req.auto_enrich and req.close_after_enrich:
            try:
                await _clean_restart()
                msg += " Browser closed after auto-enrichment."
            except Exception:
                pass

        return {"status": "success", "message": msg, "auto_enrich_result": auto_result}

    except Exception as e:
        import traceback; traceback.print_exc()
        await _clean_restart()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)


@router.post("/url/storage-state/save")
async def save_url_storage_state(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_active_project(db, org_id=current_user.organization_id)
    project_paths = _ensure_project_structure(project)
    if PAGE is None or (hasattr(PAGE, "is_closed") and PAGE.is_closed()):
        raise HTTPException(status_code=409, detail="No active page to capture storage state.")
    path = auth_storage_path(Path(project_paths["project_root"]))
    try:
        await PAGE.context.storage_state(path=str(path))
        try:
            current_url = PAGE.url or ""
        except Exception:
            current_url = ""
        landing_path = auth_landing_path(Path(project_paths["project_root"]))
        if current_url:
            try:
                landing_path.write_text(
                    current_url,
                    encoding="utf-8",
                )
            except Exception:
                pass
        try:
            storage = DatabaseBackedProjectStorage(project, Path(project_paths["project_root"]), db)
            if path.exists():
                storage.write_file("auth/storage.json", path.read_text(encoding="utf-8"), "utf-8")
            if landing_path.exists():
                storage.write_file("auth/landing_url.txt", landing_path.read_text(encoding="utf-8"), "utf-8")
        except Exception:
            pass
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save storage state: {exc}") from exc
    return {"status": "success", "path": str(path)}

@router.post("/enrich-from-url")
async def enrich_from_url(
    req: EnrichFromUrlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    One-shot enrichment: navigate to a URL, extract DOM metadata, and store JSON.
    This skips the image upload step and runs enrichment as the first action.
    """
    global PLAYWRIGHT, BROWSER, PAGE, TARGET, AUTOSCROLL_ENABLED, ENRICH_UI_ENABLED
    project = _get_active_project(db, org_id=current_user.organization_id)
    project_paths = _ensure_project_structure(project)
    project_root = Path(project_paths["project_root"])
    os.environ["SMARTAI_PROJECT_DIR"] = project_paths["project_root"]
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)

    try:
        await _clean_restart()
        PLAYWRIGHT = await async_playwright().start()

        headless = req.headless
        if not auth_storage_path(project_root).exists():
            headless = False
        BROWSER = await PLAYWRIGHT.chromium.launch(headless=headless, slow_mo=req.slow_mo)

        context_kwargs: Dict[str, Any] = {
            "ignore_https_errors": req.ignore_https_errors,
            "viewport": {"width": 1400, "height": 900},
            "bypass_csp": True,
            "has_touch": False,
            "device_scale_factor": 1,
            "reduced_motion": "reduce",
            "color_scheme": "light",
        }

        storage_file = auth_storage_path(project_root)
        if not storage_file.exists():
            storage_file = _resolve_storage_file()
        try:
            if storage_file and storage_file.exists():
                context_kwargs["storage_state"] = str(storage_file)
                _safe_log(f"[enrichment] Using storage_state from {storage_file}")
        except Exception:
            _safe_log(f"[enrichment] Failed to apply storage_state from {storage_file}")

        context = await BROWSER.new_context(**context_kwargs)
        PAGE = await context.new_page()
        __log_page_events(PAGE)

        try:
            PAGE.set_default_timeout(req.nav_timeout_ms)
            PAGE.set_default_navigation_timeout(req.nav_timeout_ms)
        except Exception:
            pass

        ENRICH_UI_ENABLED = False
        AUTOSCROLL_ENABLED = bool(req.enable_autoscroll)

        await _smart_navigate(PAGE, req.url, wait_until=req.wait_until if req.wait_until else "auto", timeout_ms=req.nav_timeout_ms)
        if should_start_auth_watch(auth_storage_path(project_root), getattr(PAGE, "url", "")):
            global _AUTH_WATCH_TASK
            if _AUTH_WATCH_TASK and not _AUTH_WATCH_TASK.done():
                _AUTH_WATCH_TASK.cancel()
            _AUTH_WATCH_TASK = asyncio.create_task(wait_for_login_and_save(PAGE, project_root))
        await __snapshot_if_blank(PAGE, "enrich-from-url")
        try:
            TARGET = await _select_extraction_target(PAGE)
        except Exception:
            TARGET = PAGE

        page_name = _canonical(req.page_name) if req.page_name else await _derive_page_name(PAGE)
        result = await _run_enrichment_for(page_name)

        if req.close_after_enrich:
            try:
                await _clean_restart()
            except Exception:
                pass

        return {"status": "success", "message": result.get("message"), **result}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        await _clean_restart()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)

@router.post("/auto-enrich")
async def auto_enrich_endpoint(
    req: AutoEnrichRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_active_project(db, org_id=current_user.organization_id)
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)
    try:
        res = await _auto_enrich(
            strategy=req.enrich_strategy,
            crawl_max_pages=req.crawl_max_pages,
            crawl_max_depth=req.crawl_max_depth,
            crawl_delay_ms=req.crawl_delay_ms,
            crawl_same_origin_only=req.crawl_same_origin_only,
        )
        if req.close_after_enrich:
            try: await _clean_restart()
            except Exception: pass
        return {"status": "success", "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)

@router.post("/crawl-and-enrich")
async def crawl_and_enrich_endpoint(
    req: CrawlRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _get_active_project(db, org_id=current_user.organization_id)
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)
    try:
        res = await _crawl_and_enrich(
            start_url=req.start_url or getattr(PAGE, "url", None),
            max_pages=req.max_pages,
            max_depth=req.max_depth,
            delay_ms=req.delay_ms,
            same_origin_only=req.same_origin_only,
        )
        if req.close_after_enrich:
            try: await _clean_restart()
            except Exception: pass
        return {"status": "success", "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)

@router.post("/set-current-page-name")
async def set_page_name(
    req: PageNameSetRequest,
    current_user: User = Depends(get_current_user),
):
    global CURRENT_PAGE_NAME
    CURRENT_PAGE_NAME = _canonical(req.page_name)
    _safe_log(f"[INFO] ✅ Page name set to: {CURRENT_PAGE_NAME}")
    return {"status": "success", "page_name": CURRENT_PAGE_NAME}

@router.post("/execution-mode")
async def toggle_execution_mode(
    req: ExecutionModeRequest,
    current_user: User = Depends(get_current_user),
):
    global EXECUTION_MODE, ENRICH_UI_ENABLED, AUTOSCROLL_ENABLED, TARGET
    EXECUTION_MODE = bool(req.enabled)
    AUTOSCROLL_ENABLED = False if EXECUTION_MODE else AUTOSCROLL_ENABLED
    if EXECUTION_MODE:
        try:
            await PAGE.evaluate("window._smartaiDisabled = true; if (window.smartAI_disableUI) window.smartAI_disableUI();")
        except Exception: pass
        ENRICH_UI_ENABLED = False
    try:
        TARGET = await _select_extraction_target(PAGE)
    except Exception:
        TARGET = PAGE
    return {"status": "success", "execution_mode": EXECUTION_MODE}

@router.post("/ui/disable")
async def disable_ui(current_user: User = Depends(get_current_user)):
    global ENRICH_UI_ENABLED
    ENRICH_UI_ENABLED = False
    try:
        await PAGE.evaluate("window._smartaiDisabled = true; if (window.smartAI_disableUI) window.smartAI_disableUI();")
    except Exception: pass
    return {"status": "success", "ui_enabled": ENRICH_UI_ENABLED}

@router.post("/capture-dom-from-client")
async def capture_from_keyboard(
    _: CaptureRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Manual trigger kept; does NOT auto-close.
    global PAGE, TARGET, CURRENT_PAGE_NAME, AUTOSCROLL_ENABLED
    try:
        if PAGE is None: raise HTTPException(status_code=500, detail="❌ Cannot extract. No active page handle.")
        if hasattr(PAGE, "is_closed") and PAGE.is_closed(): raise HTTPException(status_code=500, detail="❌ Cannot extract. Page is already closed.")
        if not CURRENT_PAGE_NAME: CURRENT_PAGE_NAME = "page"
        # Ensure chroma path is available via project activation
    
        page_name = CURRENT_PAGE_NAME
        _safe_log(f"[INFO] Enrichment triggered for: {page_name}")

        await _refresh_target("capture-start")
        await __snapshot_if_blank(PAGE, "before-capture")

        with _activate_project_storage(db, org_id=current_user.organization_id):
            result = await _run_enrichment_for(page_name)
        await __snapshot_if_blank(PAGE, "after-capture")
        return {"status": "success", "message": f"[Keyboard Trigger] {result['message']}", **result}

    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"❌ Capture failed: {e.__class__.__name__}: {e}")

@router.get("/available-pages")
async def list_page_names(current_user: User = Depends(get_current_user)):
    try:
        return {"status": "success", "pages": _available_pages_for_dropdown()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/current-url")
async def current_url(current_user: User = Depends(get_current_user)):
    try:
        target_url = None
        if TARGET is not None:
            try: target_url = TARGET.url
            except Exception: target_url = None
        return {"page_url": getattr(PAGE, "url", None), "target_url": target_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.on_event("shutdown")
async def shutdown_browser():
    await _clean_restart()

@router.get("/latest-match-result")
async def get_latest_match_result(current_user: User = Depends(get_current_user)):
    try:
        records = _get_chroma_collection().get()
        matched = [r for r in records.get("metadatas", []) if r.get("dom_matched") is True]
        return {"status": "success", "matched_elements": matched, "count": len(matched)}
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/reset-enrichment/{page_name}")
async def reset_enrichment_api(
    page_name: str,
    current_user: User = Depends(get_current_user),
):
    reset_enriched(page_name)
    return {"success": True, "message": f"Enrichment reset for {page_name}"}

__all__ = ["router"]
