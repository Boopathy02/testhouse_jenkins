
# manual_enrichment_api.py
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
from urllib.parse import urlparse
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
# Expose manual enrichment at root-level paths (no extra prefix).
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
MANUAL_BROWSER_CLOSED: bool = False

# execution/enrichment toggles
EXECUTION_MODE: bool = False
ENRICH_UI_ENABLED: bool = False           # modal disabled by default
# Auto-scroll disabled for manual enrichment; capture uses the current viewport only.
AUTOSCROLL_ENABLED: bool = True
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
    headless: bool = False
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
    enable_enrichment_ui: bool = True

class CaptureRequest(BaseModel):
    pass

class PageNameSetRequest(BaseModel):
    page_name: str

class ExecutionModeRequest(BaseModel):
    enabled: bool

class EnrichFromUrlRequest(BaseModel):
    url: str = Field(..., description="Target URL to enrich")
    page_name: Optional[str] = None
    headless: bool = False
    slow_mo: int = 80
    wait_until: str = "auto"
    nav_timeout_ms: int = 60000
    ignore_https_errors: bool = True
    enable_enrichment_ui: bool = True
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
  function onKey(e){
    if(window._smartaiDisabled) return;
    if(!(e.altKey && (e.key==='q'||e.key==='Q'))) return;
    if(e.ctrlKey||e.metaKey) return;
    if(isEditable(document.activeElement)) return;
    try{e.preventDefault();e.stopPropagation();}catch(_){}
    try{window.top.postMessage({__smartai:'TOGGLE_MODAL'},'*');}catch(_){}
  }
  window.addEventListener('keydown', onKey, true);
})();
"""

UI_MODAL_TOP_JS = r"""
(() => {
  if (window !== window.top) return;
  if (window._smartaiTopInstalled) return;
  window._smartaiTopInstalled = true;

  function killLegacyInputs() {
    try {
      // Remove known legacy elements
      ['ocrModal','ocrModalWrapper','pageDropdown','smartai_page_input']
        .forEach(id => document.getElementById(id)?.remove());

      // Remove ANY input inside smartai modal
      const modal = document.getElementById('smartaiModal');
      if (modal) {
        modal.querySelectorAll('input, select').forEach(el => el.remove());
      }

      // Remove floating legacy Page/URL inputs
      document.querySelectorAll('input').forEach(el => {
        const ph = (el.placeholder || '').toLowerCase();
        const val = (el.value || '').toLowerCase();
        if (
          ph.includes('page') ||
          ph.includes('url') ||
          ph.includes('customer') ||
          val.includes('page') ||
          val.includes('url')
        ) {
          el.remove();
        }
      });
    } catch (_) {}
  }

  function ensureModal() {
    killLegacyInputs();

    let modal = document.getElementById('smartaiModal');
    if (modal) return;

    modal = document.createElement('div');
    modal.id = 'smartaiModal';
    modal.style.cssText = `
      position: fixed;
      top: 40%;
      left: 50%;
      transform: translate(-50%, -50%);
      background: #ffffff;
      padding: 16px;
      border: 2px solid #000;
      z-index: 2147483647;
      display: none;
      min-width: 220px;
      border-radius: 10px;
      font-family: Arial, sans-serif;
    `;

    modal.innerHTML = `
      <div style="text-align:center;font-weight:bold;margin-bottom:10px;">
        Manual Enrich
      </div>

      <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap;">
        <button id="smartai_enrich_btn">Enrich</button>
        <button id="smartai_close_browser_btn">Close Browser</button>
        <button id="smartai_close_btn">Close</button>
      </div>

      <div id="smartai_msg"
           style="margin-top:10px;font-weight:bold;text-align:center;">
      </div>

      <div style="margin-top:8px;color:#666;font-size:12px;text-align:center;">
        Tip: press <b>Alt+Q</b> to open / close
      </div>
    `;

    document.body.appendChild(modal);

    const msg = modal.querySelector('#smartai_msg');

    document.getElementById('smartai_enrich_btn').onclick = async () => {
      killLegacyInputs();
      msg.style.color = 'blue';
      msg.textContent = 'Capturing metadata...';

      try {
        const res = JSON.parse(await window.smartAI_enrich() || '{}');
        if (res.status === 'success') {
          msg.style.color = 'green';
          msg.textContent = `Captured ${res.count || 0} elements`;
        } else {
          msg.style.color = 'red';
          msg.textContent = res.error || 'Enrichment failed';
        }
      } catch {
        msg.style.color = 'red';
        msg.textContent = 'Error during enrichment';
      }
    };

    document.getElementById('smartai_close_btn').onclick = () => {
      modal.style.display = 'none';
      killLegacyInputs();
    };

    document.getElementById('smartai_close_browser_btn').onclick = async () => {
      killLegacyInputs();
      msg.style.color = 'blue';
      msg.textContent = 'Closing browser...';
      try {
        const res = JSON.parse(await window.smartAI_close_browser() || '{}');
        if (res.status === 'success') {
          msg.style.color = 'green';
          msg.textContent = 'Browser closed';
        } else {
          msg.style.color = 'red';
          msg.textContent = res.error || 'Failed to close browser';
        }
      } catch {
        msg.style.color = 'red';
        msg.textContent = 'Error closing browser';
      }
    };

    window.smartaiToggleModal = () => {
      killLegacyInputs();
      modal.style.display = modal.style.display === 'none' ? 'block' : 'none';
    };
  }

  function toggleModal() {
    ensureModal();
    window.smartaiToggleModal();
  }

  // Alt+Q shortcut
  window.addEventListener('keydown', e => {
    if (!(e.altKey && (e.key === 'q' || e.key === 'Q'))) return;
    if (e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    toggleModal();
  }, true);

  // iframe bridge
  window.addEventListener('message', e => {
    if (e?.data?.__smartai === 'TOGGLE_MODAL') toggleModal();
  }, true);

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

async def _remove_legacy_modal(page: Page):
    """
    Ensure any legacy Page/URL modal is removed before showing the new minimal modal.
    """
    try:
        await page.add_init_script("""
            (() => {
              // Do not remove our SmartAI modal; only clear legacy overlays/inputs.
              const killIds = ['ocrModal','ocrModalWrapper','pageDropdown','smartai_page_input'];
              const nuke = () => {
                killIds.forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
                document.querySelectorAll('*').forEach(el => {
                  const txt = (el.innerText || '').toLowerCase();
                  if (txt.includes('page/url') && el.tagName === 'DIV' && el.id === 'smartaiModal') {
                    el.remove();
                  }
                });
              };
              nuke();
              document.addEventListener('DOMContentLoaded', nuke);
            })();
        """)
    except Exception:
        pass
    try:
        await page.evaluate("""
            (() => {
              // Do not remove our SmartAI modal; only clear legacy overlays/inputs.
              const killIds = ['ocrModal','ocrModalWrapper','pageDropdown','smartai_page_input'];
              killIds.forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
            })();
        """)
    except Exception:
        pass

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

def _looks_like_url(val: str) -> bool:
    if not val:
        return False
    val = val.strip()
    return val.startswith(("http://", "https://", "/"))

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

def _write_filtered_aggregate(meta_dir: Path, current_payload: List[Dict[str, Any]]) -> None:
    """
    Build after_enrichment.json containing only records that have an ocr_type.
    Aggregates across all per-page after_enrichment_*.json files.
    """
    def _with_ocr_type(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [r for r in (items or []) if (r.get("ocr_type") or "").strip()]

    aggregate: List[Dict[str, Any]] = []
    aggregate.extend(_with_ocr_type(current_payload))

    try:
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
        "ocr_present": False,
        "ts": _ts(),
    }

def _no_elements_record(page_name: str, source_url: Optional[str]) -> Dict[str, Any]:
    return {
        "page_name": page_name,
        "source_url": source_url or "",
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

async def _progressive_autoscroll(fr: Union[Page, Frame], steps: int = 6, pause_ms: int = 250):
    """
    Scroll the current page to surface more elements. This stays within the same page and does not navigate.
    """
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
    except Exception:
        pass

async def _pre_settle(fr: Union[Page, Frame], timeout_ms: int = 8000) -> None:
    try:
        try: await fr.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception: pass
        try: await fr.wait_for_selector("body", state="attached", timeout=timeout_ms)
        except Exception: pass
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


async def _open_action_modals(fr: Union[Page, Frame], wait_ms: int = 600):
    """
    Proactively click obvious action buttons (e.g., 'Create', 'Add', 'New', 'Edit')
    to surface modal content for enrichment. Limits the number of clicks to stay safe.
    """
    try:
        candidates = []
        try:
            candidates = await fr.query_selector_all("button, a[role='button'], [data-bs-toggle='modal'], [data-toggle='modal']")
        except Exception:
            candidates = []

        keywords = re.compile(r"(create|add|new|edit|open)", re.I)
        clicked = 0
        for el in candidates:
            if clicked >= 3:
                break
            try:
                label = (await el.inner_text() or "") + " " + (await el.get_attribute("aria-label") or "")
                if not keywords.search(label):
                    continue
                visible = await el.is_visible()
                disabled = await el.get_attribute("disabled")
                if not visible or disabled:
                    continue
                await el.click()
                clicked += 1
                await asyncio.sleep(max(0, wait_ms) / 1000)
            except Exception:
                continue
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
                        # Skip closing our own SmartAI modal buttons
                        try:
                            in_smartai_modal = await btn.evaluate("el => !!el.closest('#smartaiModal')")
                        except Exception:
                            in_smartai_modal = False
                        if in_smartai_modal:
                            continue
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
    # Guard against playwright returning a string instead of an object for failure
    def _req_failed(req):
        failure = getattr(req, "failure", None)
        err = getattr(failure, "error_text", None) if failure else None
        if err is None and isinstance(failure, str):
            err = failure
        _safe_log(f"[requestfailed] {getattr(req,'url',None)} -> {err}")
    page.on("requestfailed", _req_failed)
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

async def _freeze_navigation(page: Page):
    await page.add_init_script("""
        (() => {
            window.__smartai_nav_locked = true;

            const block = () => {
                if (window.__smartai_nav_locked) {
                    throw new Error("Navigation locked during enrichment");
                }
            };

            history.pushState = new Proxy(history.pushState, { apply: block });
            history.replaceState = new Proxy(history.replaceState, { apply: block });

            window.addEventListener('beforeunload', block);
        })();
    """)

async def _unfreeze_navigation(page: Page):
    await page.evaluate("""
        window.__smartai_nav_locked = false;
    """)

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
    """
    Single navigation helper: try https, then http once. No retries across multiple pages.
    """
    def _with_scheme(u: str, scheme: str) -> str:
        p = urlparse(u)
        return f"{scheme}://{u}" if not p.scheme else u

    targets = [_with_scheme(raw_url, "https"), _with_scheme(raw_url, "http")]
    for url in targets:
        try:
            resp = await page.goto(
                url,
                wait_until="domcontentloaded" if (wait_until or "").lower() == "auto" else wait_until,
                timeout=timeout_ms,
            )
            try:
                await page.wait_for_selector("body", state="attached", timeout=min(5000, timeout_ms))
            except Exception:
                pass
            try:
                await _dismiss_cookie_banner(page)
            except Exception:
                pass
            return resp
        except PWTimeoutError:
            continue
        except Exception:
            continue
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
        // Skip SmartAI modal elements to avoid self-capture
        if (el && el.closest && el.closest('#smartaiModal')) return;
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
        // Skip SmartAI modal elements to avoid self-capture
        if (el && el.closest && el.closest('#smartaiModal')) return;
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
    # 🔒 Snapshot the current page URL to prevent SPA hijack
    global PAGE, TARGET, CURRENT_PAGE_NAME, AUTOSCROLL_ENABLED
    stable_url = getattr(PAGE, "url", None)
    if PAGE is None: raise HTTPException(status_code=500, detail="❌ Cannot extract. No active page handle.")
    if hasattr(PAGE, "is_closed") and PAGE.is_closed(): raise HTTPException(status_code=500, detail="❌ Cannot extract. Page is already closed.")
    # Ensure chroma path is available via project activation

    CURRENT_PAGE_NAME = _canonical(page_name)

    # >>> NEW: ensure we're on the correct page BEFORE extraction
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
    if AUTOSCROLL_ENABLED:
        await _progressive_autoscroll(TARGET, steps=6, pause_ms=250)
    # Open obvious action modals (e.g., Create/Add/New) to capture their contents
    await _open_action_modals(TARGET, wait_ms=600)

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
    # 🔒 Re-assert page stability before DOM extraction
    if PAGE.url != stable_url:
        await PAGE.goto(stable_url, wait_until="domcontentloaded")
        try:
            await _dismiss_cookie_banner(PAGE)
        except Exception:
            pass

    dom_data = _dedupe_records(dom_data)

    # Normalize fields for deduping (include nearby_label as fallback)
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

    # debug dumps
    _write_project_file(
        paths["debug"] / f"dom_data_{_file_key(CURRENT_PAGE_NAME)}.txt",
        pprint.pformat(dom_data),
        encoding="utf-8",
    )

    # no matching: use captured DOM data directly
    updated_matches = dom_data or []
    _write_project_file(
        paths["debug"] / f"captured_dom_{_file_key(CURRENT_PAGE_NAME)}.txt",
        pprint.pformat(updated_matches),
        encoding="utf-8",
    )

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

    # fallback if nothing captured
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

    ocr_only_payload = [r for r in combined_payload if (r.get("ocr_type") or "").strip()]
    ocr_only_path = _ocr_only_path_for_page(CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
    _write_project_file(ocr_only_path, json.dumps(ocr_only_payload, indent=2), encoding="utf-8")

    # refresh global snapshot across all per-page files, keeping only records with ocr_type
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
        "message": f"Captured {len(standardized_matches)} elements for page: {CURRENT_PAGE_NAME}",
        "captured_data": standardized_matches,
        "count": len(standardized_matches),
        "output_path": str(out_path),
        "ocr_only_path": str(ocr_only_path),
    }
# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@router.post("/manual/launch-browser")
async def launch_browser(
    req: LaunchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    global PLAYWRIGHT, BROWSER, PAGE, TARGET, CURRENT_PAGE_NAME, ENRICH_UI_ENABLED, AUTOSCROLL_ENABLED, MANUAL_BROWSER_CLOSED
    MANUAL_BROWSER_CLOSED = False
    project = _get_active_project(db, org_id=current_user.organization_id)
    project_paths = _ensure_project_structure(project)
    project_root = Path(project_paths["project_root"])
    os.environ["SMARTAI_PROJECT_DIR"] = project_paths["project_root"]
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)
    try:
        await _clean_restart()
        PLAYWRIGHT = await async_playwright().start()

        launch_args: List[str] = []
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
        if req.user_agent:
            context_kwargs["user_agent"] = req.user_agent
        if req.extra_http_headers:
            context_kwargs["extra_http_headers"] = req.extra_http_headers
        if req.http_username and req.http_password:
            context_kwargs["http_credentials"] = {"username": req.http_username, "password": req.http_password}

        ENRICH_UI_ENABLED = bool(req.enable_enrichment_ui)
        AUTOSCROLL_ENABLED = True  # allow same-page scrolling while capturing

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

        context = await BROWSER.new_context(**context_kwargs)
        PAGE = await context.new_page()
        __log_page_events(PAGE)

        await _remove_legacy_modal(PAGE)

        try:
            PAGE.set_default_timeout(req.nav_timeout_ms)
            PAGE.set_default_navigation_timeout(req.nav_timeout_ms)
        except Exception:
            pass

        async def _binding_enrich(source, page_or_url: Optional[str] = None):
            try:
                # Ignore provided label; always capture current page
                target_page = await _derive_page_name(PAGE)
                globals()["CURRENT_PAGE_NAME"] = target_page

                await _freeze_navigation(PAGE)
                with _activate_project_storage_from_scope(current_user.organization_id):
                    result = await _run_enrichment_for(target_page)
                await _unfreeze_navigation(PAGE)

                return json.dumps(result)
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e)})

        await PAGE.expose_binding("smartAI_enrich", _binding_enrich)

        async def _binding_close_browser(source):
            try:
                globals()["MANUAL_BROWSER_CLOSED"] = True
                await _clean_restart()
                return json.dumps({"status": "success"})
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e)})

        await PAGE.expose_binding("smartAI_close_browser", _binding_close_browser)

        if ENRICH_UI_ENABLED:
            await _remove_legacy_modal(PAGE)
            await PAGE.add_init_script(UI_KEYBRIDGE_JS)
            await PAGE.add_init_script(UI_MODAL_TOP_JS)

        await _smart_navigate(PAGE, req.url, wait_until=req.wait_until if req.wait_until else "auto", timeout_ms=req.nav_timeout_ms)
        if should_start_auth_watch(auth_storage_path(project_root), getattr(PAGE, "url", "")):
            global _AUTH_WATCH_TASK
            if _AUTH_WATCH_TASK and not _AUTH_WATCH_TASK.done():
                _AUTH_WATCH_TASK.cancel()
            _AUTH_WATCH_TASK = asyncio.create_task(wait_for_login_and_save(PAGE, project_root))
        await _remove_legacy_modal(PAGE)

        # Re-enable and reattach modal/keybridge after navigation (some sites reset globals)
        if ENRICH_UI_ENABLED:
            try:
                await PAGE.evaluate("window._smartaiDisabled = false; window._smartaiTopInstalled = false; window._smartaiKeyBridgeInstalled = false;")
                await PAGE.add_init_script(UI_KEYBRIDGE_JS)
                await PAGE.add_init_script(UI_MODAL_TOP_JS)
                await PAGE.wait_for_timeout(300)
                await PAGE.evaluate("""
                    (() => {
                      if (typeof window.smartaiToggleModal === 'function') {
                        window.smartaiToggleModal();
                      }
                    })();
                """)
            except Exception:
                pass
        try:
            navigated_url = getattr(PAGE, "url", None) or req.url
            if navigated_url:
                os.environ["SITE_URL"] = navigated_url
        except Exception:
            pass
        await __snapshot_if_blank(PAGE, "launch-browser")
        try:
            TARGET = await _select_extraction_target(PAGE)
        except Exception:
            TARGET = PAGE

        CURRENT_PAGE_NAME = _canonical(await _derive_page_name(PAGE))
        msg = f"Browser launched and navigated to {req.url}. Modal available (Alt+Q)."
        return {"status": "success", "message": msg, "auto_enrich_result": None}

    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        await _clean_restart()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)


@router.post("/manual/storage-state/save")
async def save_manual_storage_state(
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
    global PLAYWRIGHT, BROWSER, PAGE, TARGET, AUTOSCROLL_ENABLED, ENRICH_UI_ENABLED, MANUAL_BROWSER_CLOSED
    MANUAL_BROWSER_CLOSED = False
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

        ENRICH_UI_ENABLED = bool(req.enable_enrichment_ui)
        AUTOSCROLL_ENABLED = False

        async def _binding_enrich(source, page_name: Optional[str] = None):
            try:
                target_page = _canonical(page_name or CURRENT_PAGE_NAME or "page")
                globals()["CURRENT_PAGE_NAME"] = target_page
                with _activate_project_storage_from_scope():
                    res = await _run_enrichment_for(target_page)
                return json.dumps(res)
            except HTTPException as he:
                return json.dumps({"status": "fail", "error": he.detail})
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e)})

        await PAGE.expose_binding("smartAI_enrich", _binding_enrich)

        async def _binding_close_browser(source):
            try:
                globals()["MANUAL_BROWSER_CLOSED"] = True
                await _clean_restart()
                return json.dumps({"status": "success"})
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e)})

        await PAGE.expose_binding("smartAI_close_browser", _binding_close_browser)

        if ENRICH_UI_ENABLED:
            await _remove_legacy_modal(PAGE)
            await PAGE.add_init_script(UI_KEYBRIDGE_JS)
            await PAGE.add_init_script(UI_MODAL_TOP_JS)


        await _smart_navigate(PAGE, req.url, wait_until=req.wait_until if req.wait_until else "auto", timeout_ms=req.nav_timeout_ms)
        if should_start_auth_watch(auth_storage_path(project_root), getattr(PAGE, "url", "")):
            global _AUTH_WATCH_TASK
            if _AUTH_WATCH_TASK and not _AUTH_WATCH_TASK.done():
                _AUTH_WATCH_TASK.cancel()
            _AUTH_WATCH_TASK = asyncio.create_task(wait_for_login_and_save(PAGE, project_root))
        try:
            navigated_url = getattr(PAGE, "url", None) or req.url
            if navigated_url:
                os.environ["SITE_URL"] = navigated_url
        except Exception:
            pass
        await __snapshot_if_blank(PAGE, "enrich-from-url")
        try:
            TARGET = await _select_extraction_target(PAGE)
        except Exception:
            TARGET = PAGE

        # Set the current page name for modal-triggered enrichment, but do not auto-enrich.
        global CURRENT_PAGE_NAME
        CURRENT_PAGE_NAME = "page"

        msg = f"Ready for manual enrichment on {req.url}. Open modal (Alt+Q) and click Enrich."
        return {"status": "success", "message": msg, "page_name": CURRENT_PAGE_NAME}
    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        await _clean_restart()
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

@router.get("/manual/browser-status")
async def manual_browser_status(current_user: User = Depends(get_current_user)):
    return {"closed": MANUAL_BROWSER_CLOSED}

@router.on_event("shutdown")
async def shutdown_browser():
    await _clean_restart()

@router.post("/reset-enrichment/{page_name}")
async def reset_enrichment_api(
    page_name: str,
    current_user: User = Depends(get_current_user),
):
    reset_enriched(page_name)
    return {"success": True, "message": f"Enrichment reset for {page_name}"}

__all__ = ["router"]  
