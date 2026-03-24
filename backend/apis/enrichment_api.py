# enrichment_api.py
from __future__ import annotations

import os
import json
import re
import pprint
import asyncio
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from chromadb import PersistentClient
from .projects_api import _ensure_project_structure, get_current_user, get_user_project
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
from utils.project_context import (
    filter_metadata_by_project,
    set_current_project_id,
    reset_current_project_id,
)
from utils.file_utils import build_standard_metadata
from utils.smart_ai_utils import get_smartai_src_dir
from utils.session_manager import (
    auth_storage_path,
    auth_landing_path,
    should_start_auth_watch,
    wait_for_login_and_save,
)
from database.project_storage import DatabaseBackedProjectStorage
from database.session import get_db, session_scope
from database.models import Project, User
from sqlalchemy.orm import Session

# -----------------------------------------------------------------------------
# Router & DB
# -----------------------------------------------------------------------------
router = APIRouter()
_ACTIVE_STORAGE_VAR: ContextVar[Optional[DatabaseBackedProjectStorage]] = ContextVar(
    "active_storage", default=None
)


@dataclass
class ProjectBrowserSession:
    project_id: int
    playwright: Any = None
    browser: Optional[Browser] = None
    page: Optional[Page] = None
    target: Optional[Union[Page, Frame]] = None
    current_page_name: str = "unknown_page"
    execution_mode: bool = False
    enrich_ui_enabled: bool = False
    autoscroll_enabled: bool = True
    src_directory: Optional[Path] = None
    chroma_path: Optional[Path] = None
    auth_watch_task: Optional[asyncio.Task] = None


_PROJECT_SESSIONS: Dict[int, ProjectBrowserSession] = {}
_SESSIONS_LOCK = asyncio.Lock()


async def _get_session(project_id: int) -> ProjectBrowserSession:
    session = _PROJECT_SESSIONS.get(project_id)
    if not session:
        raise HTTPException(
            status_code=404,
            detail="No active browser session for this project. Launch the browser first.",
        )
    return session


async def _create_or_reset_session(
    project_id: int,
    src_directory: Path,
    chroma_path: Path,
) -> ProjectBrowserSession:
    async with _SESSIONS_LOCK:
        session = _PROJECT_SESSIONS.get(project_id)
        if session is None:
            session = ProjectBrowserSession(project_id=project_id)
            _PROJECT_SESSIONS[project_id] = session
        session.src_directory = src_directory
        session.chroma_path = chroma_path
        session.current_page_name = "unknown_page"
        session.execution_mode = False
        session.enrich_ui_enabled = False
        session.autoscroll_enabled = True
        return session


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


# Lazy chroma accessor to avoid creating repo-level data folder before a project is active
def _get_chroma_collection(projectChromaPath: Path):
    client = PersistentClient(projectChromaPath)
    return client.get_or_create_collection(
        name=os.environ.get("SMARTAI_CHROMA_COLLECTION", "element_metadata")
    )


# -----------------------------------------------------------------------------
# Runtime state
# -----------------------------------------------------------------------------
# PLAYWRIGHT = None
# BROWSER: Optional[Browser] = None
# PAGE: Optional[Page] = None
# TARGET: Optional[Union[Page, Frame]] = None
# CURRENT_PAGE_NAME: str = "unknown_page"

# execution/enrichment toggles
# EXECUTION_MODE: bool = False
# ENRICH_UI_ENABLED: bool = False           # modal disabled by default
# AUTOSCROLL_ENABLED: bool = True           # on by default for better capture


# -----------------------------------------------------------------------------
# Config (paths resolved lazily so project activation can update them)
# -----------------------------------------------------------------------------
def _src_dir() -> Path:
    env_src = os.environ.get("SMARTAI_SRC_DIR")
    path = Path(env_src) if env_src else get_smartai_src_dir()
    path.mkdir(parents=True, exist_ok=True)
    if not getattr(_src_dir, "_logged", False):
        print(
            f"[DEBUG] Using SRC_DIR={path} (SMARTAI_SRC_DIR={'set' if env_src else 'unset'})"
        )
        _src_dir._logged = True
    return path


def _pages_dir() -> Path:
    env_pages = os.environ.get("SMARTAI_PAGES_DIR")
    path = Path(env_pages) if env_pages else _src_dir() / "pages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _meta_dir(src_directory) -> Path:
    env_meta = os.environ.get("SMARTAI_META_DIR")
    path = src_directory / "metadata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _debug_dir(src_directory: Path) -> Path:
    path = src_directory / "ocr-dom-metadata"
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
    enrich_strategy: str = Field(
        "mixed", description="one of: 'ocr' | 'crawl' | 'mixed'"
    )
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
  function isEditable(el){ if(!el) return false; const t=(el.tagName||'').toLowerCase(); return t==='input'||t==='textarea'||t==='select'||el.isContentEditable; }
  function ensureModal(){
    if(document.getElementById('ocrModal')) return;
    const wrap=document.createElement('div');
    wrap.id='ocrModalWrapper';
    wrap.innerHTML=`<div id="ocrModal" style="position:fixed;top:40%;left:50%;transform:translate(-50%,-50%);background:#fff;padding:16px;border:2px solid #000;z-index:2147483647;display:none;min-width:360px;max-width:90vw;max-height:80vh;overflow:auto;border-radius:10px;font-family:Arial,sans-serif;">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <label style="min-width:70px;">Page:</label>
        <select id="pageDropdown" style="padding:6px;min-width:220px;max-width:60vw;"></select>
        <button id="smartAI_refresh_pages">Refresh</button>
      </div>
      <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">
        <button id="smartAI_enrich_btn">Enrich</button>
        <button id="smartAI_close_btn">Close</button>
      </div>
      <div id="enrichmentMessageBox" style="margin-top:10px;font-weight:bold;color:green;"></div>
      <div style="margin-top:8px;color:#666;font-size:12px;">Tip: press <b>Alt+Q</b> to open/close</div>
    </div>`;
    document.body.appendChild(wrap);
    async function trigger(){ if(window._smartaiDisabled) return; const dd=document.getElementById('pageDropdown'); const msg=document.getElementById('enrichmentMessageBox'); const chosen=(dd&&dd.value||'').trim(); if(!chosen){ msg.textContent='❌ No page found for this image.'; msg.style.color='red'; return; } msg.textContent='⏳ Enrichment in progress…'; msg.style.color='blue'; try{ const res=JSON.parse(await window.smartAI_enrich(chosen)||'{}'); if(res.status!=='success'){ msg.textContent='❌ '+(res.error||'Enrichment failed'); msg.style.color='red'; } else if(!res.count){ msg.textContent='❌ Enrichment succeeded but no elements matched.'; msg.style.color='red'; } else { msg.textContent=`✅ Enriched ${res.count} elements`; msg.style.color='green'; } } catch(e){ msg.textContent='❌ Error: '+(e&&e.message||e); msg.style.color='red'; } }
    document.getElementById('smartAI_enrich_btn').onclick = trigger;
    document.getElementById('smartAI_close_btn').onclick = () => { const m=document.getElementById('ocrModal'); if(m) m.style.display='none'; };
    document.getElementById('smartAI_refresh_pages').onclick = async () => {
      const dd=document.getElementById('pageDropdown'); const msg=document.getElementById('enrichmentMessageBox'); if(!dd) return; dd.innerHTML=''; try{ const data=JSON.parse(await window.smartAI_availablePages()||'{}'); const pages=(data&&data.status==='success'&&Array.isArray(data.pages))?data.pages:[]; if(!pages.length){ msg.textContent='⚠️ No extracted image pages found.'; msg.style.color='orange'; } pages.forEach(p=>{ const opt=document.createElement('option'); opt.value=p; opt.innerText=p; dd.appendChild(opt); }); }catch(_){ msg.textContent='❌ Failed to load pages.'; msg.style.color='red'; }
    };
  }
  function toggle(){ if(window._smartaiDisabled) return; if(!document.getElementById('ocrModal')) ensureModal(); const m=document.getElementById('ocrModal'); if(!m) return; m.style.display = m.style.display==='none' ? 'block' : 'none'; if(m.style.display==='block'){ document.getElementById('smartAI_refresh_pages').click(); } }
  window.addEventListener('keydown', e=>{ if(window._smartaiDisabled) return; if(!(e.altKey && (e.key==='q'||e.key==='Q'))) return; if(e.ctrlKey||e.metaKey) return; if(isEditable(document.activeElement)) return; try{e.preventDefault();e.stopPropagation();}catch(_){ } toggle(); }, true);
  window.addEventListener('message', ev => { if(window._smartaiDisabled) return; if((ev&&ev.data||{}).__smartai==='TOGGLE_MODAL') toggle(); });
  window.smartAI_disableUI = () => { try{ window._smartaiDisabled=true; const w=document.getElementById('ocrModalWrapper'); if(w) w.remove(); }catch(_){ } };
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
    try:
        print(*a)
    except Exception:
        pass


def _ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")


def _canonical(name: str) -> str:
    n = normalize_page_name(name or "")
    if not n:
        return n
    n = re.sub(r"(?i)^(page|screen|view)+", "", n)
    n = re.sub(r"(?i)(page|screen|view)+$", "", n)
    n = re.sub(r"[_\-\s]+", "_", n).strip("_")
    return n or "page"


def _ensure_dirs(src_directory) -> Dict[str, Path]:
    return {"debug": _debug_dir(src_directory), "meta": _meta_dir(src_directory)}


def _set_active_storage(storage: Optional[DatabaseBackedProjectStorage]) -> None:
    _ACTIVE_STORAGE_VAR.set(storage)


@contextmanager
def _activate_project_storage(db: Session):
    project = _get_active_project(db)
    storage = DatabaseBackedProjectStorage(project, _src_dir(), db)
    _set_active_storage(storage)
    try:
        yield project, storage
    finally:
        _set_active_storage(None)


@contextmanager
def _activate_project_storage_from_scope():
    with session_scope() as scoped_db:
        with _activate_project_storage(scoped_db) as ctx:
            yield ctx


def _persist_project_file(path: Path, content: str, encoding: str = "utf-8") -> None:
    if path.suffix.lower() == ".txt":
        return
    storage = _ACTIVE_STORAGE_VAR.get()
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


def _get_active_project(db: Session) -> Project:
    project_id_value = os.environ.get("SMARTAI_PROJECT_ID")
    if project_id_value:
        try:
            project = (
                db.query(Project).filter(Project.id == int(project_id_value)).first()
            )
            if project:
                return project
        except ValueError:
            pass

    project_dir = os.environ.get("SMARTAI_PROJECT_DIR")
    if project_dir:
        segment = Path(project_dir).name
        match = re.match(r"(?P<id>\d+)-", segment)
        if match:
            candidate_id = int(match.group("id"))
            project = db.query(Project).filter(Project.id == candidate_id).first()
            if project:
                os.environ["SMARTAI_PROJECT_ID"] = str(project.id)
                return project

        normalized_slug = Project.normalized_key(
            segment.replace("-", " ").replace("_", " ")
        )
        project = (
            db.query(Project)
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
    return (pa.netloc or "").lower() != "" and (pa.netloc or "").lower() == (
        pb.netloc or ""
    ).lower()


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
        raw = (
            os.getenv("UI_STORAGE_FILE", "").strip()
            or os.getenv("SMARTAI_STORAGE_FILE", "").strip()
        )
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


def _ocr_name_counts(projectChromaPath: Path) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    try:
        recs = _get_chroma_collection(projectChromaPath).get() or {}
        for m in filter_metadata_by_project(recs.get("metadatas") or []):
            pn = (m or {}).get("page_name")
            if not pn:
                continue
            c = _canonical(pn)
            if not c:
                continue
            counts[c] = counts.get(c, 0) + 1
    except Exception:
        pass
    return counts


def _available_pages_for_dropdown(projectChromaPath: Path) -> List[str]:
    counts = _ocr_name_counts(projectChromaPath)
    noise = {"unknown", "unknown_page", "basepage"}
    for k in list(counts.keys()):
        if k in noise:
            counts.pop(k, None)
    names = list(counts.keys())
    names.sort(key=lambda n: (-counts[n], n))
    return names


def _short_hash(s: str) -> str:
    try:
        return hashlib.md5((s or "").encode("utf-8")).hexdigest()[:8]
    except Exception:
        return "00000000"


def _output_path_for_page(
    src_directory: Path, page_name: str, source_url: Optional[str]
) -> Path:
    meta_dir = _ensure_dirs(src_directory)["meta"]
    return meta_dir / f"after_enrichment_{page_name}.json"


def _merge_enrichment_records(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    index_map: Dict[tuple, int] = {}

    def _key(item: Dict[str, Any]) -> tuple:
        identifier = (
            (item or {}).get("unique_name")
            or (item or {}).get("id")
            or (item or {}).get("ocr_id")
            or (item or {}).get("label_text")
            or ""
        )
        intent = (item or {}).get("intent") or ""
        return (identifier.strip().lower(), intent.strip().lower())

    for entry in existing or []:
        key = _key(entry)
        index_map[key] = len(merged)
        merged.append(entry)

    for entry in incoming or []:
        key = _key(entry)
        if key in index_map:
            merged[index_map[key]] = entry
        else:
            index_map[key] = len(merged)
            merged.append(entry)

    return merged


def _norm_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return " ".join(
        (s or "").replace("\n", " ").replace("\r", " ").strip().strip(":").split()
    ).lower()


def _standardize_dom_only(
    rec: Dict[str, Any], page_name: str, source_url: Optional[str]
) -> Dict[str, Any]:
    bbox = rec.get("bbox") or {}
    label = (
        rec.get("label_text")
        or rec.get("aria_label")
        or rec.get("placeholder")
        or rec.get("text")
        or ""
    )
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
        "bbox": {
            "x": bbox.get("x", 0),
            "y": bbox.get("y", 0),
            "width": bbox.get("width", 0),
            "height": bbox.get("height", 0),
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
    try:
        return await fr.evaluate("!!document && !!document.body")
    except Exception:
        return False


async def _pre_settle(fr: Union[Page, Frame], timeout_ms: int = 8000) -> None:
    try:
        try:
            await fr.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            await fr.wait_for_selector("body", state="attached", timeout=timeout_ms)
        except Exception:
            pass
    except Exception:
        pass


async def _progressive_autoscroll(
    AUTOSCROLL_ENABLED: bool,
    EXECUTION_MODE: bool,
    fr: Union[Page, Frame],
    steps: int = 6,
    pause_ms: int = 250,
):
    if EXECUTION_MODE or not AUTOSCROLL_ENABLED:
        return
    try:
        await fr.evaluate(
            f"""
        (async () => {{
          const sleep = t=>new Promise(r=>setTimeout(r,t));
          const doc=document; const se=doc.scrollingElement||doc.documentElement||doc.body;
          const H = se ? (se.scrollHeight||0) : (doc.documentElement.scrollHeight||doc.body.scrollHeight||0);
          const step = Math.max(1, Math.floor(H/{max(1, steps)}));
          let y=0; for(let i=0;i<{max(1, steps)};i++){{ y+=step; window.scrollTo(0,y); await sleep({max(0, pause_ms)}); }}
          await sleep({max(0, pause_ms)}); window.scrollTo(0,0);
        }})()"""
        )
    except Exception:
        pass


async def _open_potential_modals(fr: Union[Page, Frame]):
    """
    Attempt to open modals by clicking elements that might trigger them.
    This is a heuristic approach to capture modal DOM during enrichment.
    """
    try:
        # Select elements that likely open modals: buttons/links with onclick or common modal triggers
        selectors = [
            "button[onclick]",
            'input[type="button"][onclick]',
            "a[onclick]",
            'button[data-toggle="modal"]',
            "button[data-target]",
            '[role="button"][onclick]',
            'button:has-text("Open"), button:has-text("Show"), button:has-text("Modal")',  # Playwright pseudo-selectors
        ]
        for selector in selectors:
            try:
                elements = await fr.query_selector_all(selector)
                for el in elements[:5]:  # Limit to avoid excessive clicks
                    try:
                        # Check if visible and not disabled
                        is_visible = await el.is_visible()
                        is_disabled = await el.get_attribute("disabled")
                        if is_visible and not is_disabled:
                            await el.click()
                            await asyncio.sleep(0.5)  # Wait for modal to appear
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass


def __log_page_events(page: Page):
    page.on("console", lambda m: _safe_log(f"[console:{m.type}] {m.text}"))
    page.on("pageerror", lambda e: _safe_log(f"[pageerror] {e}"))
    page.on(
        "requestfailed",
        lambda req: _safe_log(f"[requestfailed] {req.url} -> {req.failure}"),
    )
    page.on(
        "response",
        lambda resp: (
            _safe_log(f"[http {resp.status}] {resp.url}")
            if resp.status >= 400
            else None
        ),
    )


async def __snapshot_if_blank(page: Page, tag: str):
    try:
        is_blank = await page.evaluate(
            """() => {
          const b=document.body; if(!b) return false;
          const rect=b.getBoundingClientRect(); const len=(b.innerText||"").trim().length;
          return rect && rect.width>0 && rect.height>0 && len===0;
        }"""
        )
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


async def _smart_navigate(
    page: Page, raw_url: str, wait_until: str = "auto", timeout_ms: int = 60000
):
    def _with_scheme(u: str, scheme: str) -> str:
        p = urlparse(u)
        return f"{scheme}://{u}" if not p.scheme else u

    strategies = (
        ["domcontentloaded", "load", "commit", "networkidle"]
        if (wait_until or "").lower() == "auto"
        else [wait_until]
    )

    for scheme in ("https", "http"):
        url = _with_scheme(raw_url, scheme)
        for wu in strategies:
            try:
                resp = await page.goto(url, wait_until=wu, timeout=timeout_ms)
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=min(10000, timeout_ms)
                    )
                except Exception:
                    pass
                try:
                    await page.wait_for_selector("body", state="attached", timeout=5000)
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
                break
    try:
        await page.goto(_with_scheme(raw_url, "https"), timeout=timeout_ms)
    except Exception:
        pass
    try:
        await _dismiss_cookie_banner(page)
    except Exception:
        pass
    return None


async def _clean_restart(PAGE, BROWSER, PLAYWRIGHT):
    try:
        if PAGE and hasattr(PAGE, "is_closed") and not PAGE.is_closed():
            await PAGE.close()
    except Exception:
        pass
    try:
        if BROWSER:
            await BROWSER.close()
    except Exception:
        pass
    try:
        if PLAYWRIGHT:
            await PLAYWRIGHT.stop()
    except Exception:
        pass


async def _select_extraction_target(page: Page) -> Union[Page, Frame]:
    try:
        await page.wait_for_selector("iframe", timeout=3000)
    except Exception:
        pass

    candidates: List[Union[Page, Frame]] = []
    if page.main_frame:
        candidates.append(page.main_frame)
    candidates.extend([f for f in page.frames if f is not page.main_frame])

    top_url = getattr(page, "url", "") or ""
    ad_like = re.compile(
        r"(onetag|rubicon|adnxs|doubleclick|googlesyndication|taboola|adsystem|bidswitch|pubmatic|criteo|cloudflare|googletagmanager|trustarc|consent|cookie|privacy-center)",
        re.I,
    )

    async def _score_frame(fr: Union[Page, Frame]) -> Tuple[int, bool, str]:
        try:
            ok = await fr.evaluate("Boolean(document && document.body)")
            if not ok:
                return (-10_000, False, "")
            area = await fr.evaluate(
                """() => { try { const w=window.innerWidth||0, h=window.innerHeight||0; return Math.max(1,w)*Math.max(1,h); } catch { return 1; } }"""
            )
            interactive = int(
                await fr.evaluate(
                    "document.querySelectorAll('input,select,textarea,button,a,[role],[contenteditable=\"true\"]').length"
                )
            )
            url = ""
            try:
                url = fr.url or ""
            except Exception:
                url = ""
            same = _same_origin(top_url, url)
            score = (
                int(area / 10)
                + interactive * 20
                + (3000 if same else 0)
                - (8000 if ad_like.search(url) else 0)
            )
            return (score, same, url)
        except Exception:
            return (-10_000, False, "")

    best: Union[Page, Frame] = page
    best_score = -10_000
    for fr in candidates:
        score, _, _ = await _score_frame(fr)
        if score > best_score:
            best_score = score
            best = fr
    return best or page


# -----------------------------------------------------------------------------
# Derive page name & links
# -----------------------------------------------------------------------------
async def _derive_page_name(p: Page) -> str:
    try:
        title = (await p.title()) or ""
    except Exception:
        title = ""
    url = getattr(p, "url", "") or ""
    parsed = urlparse(url)
    path = (parsed.path or "/").strip("/").replace("/", "_") or "home"
    host = (parsed.netloc or "site").split(":")[0]
    pieces = [normalize_page_name(title or ""), normalize_page_name(f"{host}_{path}")]
    candidate = next((c for c in pieces if c), "unknown_page")
    return _canonical(candidate)


async def _enumerate_links(p: Page, same_origin_only: bool = True) -> List[str]:
    top = getattr(p, "url", "") or ""
    try:
        hrefs = await p.evaluate(
            """() => Array.from(document.querySelectorAll('a[href]')).map(a=>a.getAttribute('href')).filter(Boolean)"""
        )
    except Exception:
        return []
    out = []
    for h in hrefs:
        full = urljoin(top, h)
        if same_origin_only and not _same_origin(top, full):
            continue
        if full.startswith("mailto:") or full.startswith("tel:"):
            continue
        if urlparse(full).fragment:
            full = full.split("#", 1)[0]
        if full not in out:
            out.append(full)
    return out


# -----------------------------------------------------------------------------
# NEW: URL harvesting & navigation-to-page logic
# -----------------------------------------------------------------------------
def _candidate_urls_for_page(page_name: str, projectChromaPath: Path) -> List[str]:
    """
    Collect possible URLs for a given canonical page name from ChromaDB metadatas.
    We look for common fields and rank by frequency.
    """
    can = _canonical(page_name)
    url_fields = ("source_url", "url", "page_url", "origin_url")
    freq: Dict[str, int] = {}
    try:
        recs = _get_chroma_collection(projectChromaPath).get() or {}
        for m in recs.get("metadatas") or []:
            if _canonical((m or {}).get("page_name", "")) != can:
                continue
            for f in url_fields:
                u = (m or {}).get(f)
                if not u:
                    continue
                u = str(u).strip()
                if not u:
                    continue
                # ignore data URLs / mailto / tel etc.
                if (
                    u.startswith("data:")
                    or u.startswith("mailto:")
                    or u.startswith("tel:")
                ):
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


async def _click_nav_element_for_tokens(
    p: Page, token_words: List[str], timeout_ms: int = 8000
) -> bool:
    """
    Try clicking a link/button/menu whose visible text contains most of the token words.
    """
    words = [w for w in token_words if w]
    if not words:
        return False
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
        loc = p.locator(
            f"a:has-text(/{'|'.join(map(re.escape, words))}/i), button:has-text(/{'|'.join(map(re.escape, words))}/i)"
        )
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


async def _ensure_on_page(
    PAGE: Page,
    page_name: str,
    projectChromaPath: Path,
    same_origin_only: bool = True,
    nav_timeout_ms: int = 60000,
) -> None:
    """
    Make best effort to navigate the browser to the page corresponding to `page_name`.
    Strategy:
      1) If current page already matches canonical name -> return.
      2) Navigate to best candidate URL(s) harvested from Chroma metadata.
      3) Try clicking a nav link/button based on page-name tokens.
      4) Probe discovered links (limited BFS) looking for a page-name match.
    """

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
    candidates = _candidate_urls_for_page(page_name, projectChromaPath)
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
        clicked = await _click_nav_element_for_tokens(
            PAGE, tokens, timeout_ms=min(8000, nav_timeout_ms)
        )
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
        if u in visited:
            continue
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

    _safe_log(
        f"[nav] ⚠️ Could not confidently navigate to page '{desired_can}'. Continuing with current page."
    )


# -----------------------------------------------------------------------------
# Extraction & enrichment core
# -----------------------------------------------------------------------------
def _dedupe_records(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        key = (
            (r.get("tag") or ""),
            (r.get("id") or ""),
            (r.get("name") or ""),
            (r.get("type") or ""),
            _norm_text(
                r.get("label_text")
                or r.get("aria_label")
                or r.get("placeholder")
                or r.get("text")
                or ""
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


async def _rich_extract_dom_metadata(fr: Union[Page, Frame]) -> List[Dict[str, Any]]:
    js = r"""
    (() => {
      const out = [], seen = new Set();
      const norm = s => (s || "").replace(/\s+/g, " ").trim();
 
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
 
      const isInteractable = el => !el.disabled && !el.closest('nav,[role="navigation"],header,footer,[aria-hidden="true"],[hidden]');
      const textOf = el => norm(el ? (el.innerText || el.textContent || "") : "");
 
      const idText = (doc, id) => {
        if (!id) return "";
        const n = doc.getElementById(id);
        return n ? textOf(n) : "";
      };
 
      const labelForMap = doc => {
        const m = new Map();
        doc.querySelectorAll("label[for]").forEach(l => {
          const f = l.getAttribute("for");
          if (f) m.set(f, (m.get(f) || "") + " " + textOf(l));
        });
        return m;
      };
 
      const wrapLabelText = input => {
        const lab = input.closest("label");
        return lab ? textOf(lab) : "";
      };
 
      const accessibleName = (el, doc, _lmap) => {
        const aria = el.getAttribute && el.getAttribute("aria-label");
        if (aria) return norm(aria);
 
        const lb = el.getAttribute && el.getAttribute("aria-labelledby");
        if (lb) {
          const txt = lb.split(/\s+/).map(id => idText(doc, id)).join(" ").trim();
          if (txt) return norm(txt);
        }
 
        const id = el.id || "";
        if (id && _lmap.has(id)) return norm(_lmap.get(id));
 
        const wrap = wrapLabelText(el);
        if (wrap) return norm(wrap);
 
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
        if (seen.has(key)) return;
        seen.add(key);
        out.push(r);
      };
 
      const pick = (el, doc, _lmap, framePrefix="") => {
        if (!isVisible(el) || !isInteractable(el)) return;
        const tag = (el.tagName || "").toLowerCase();
        const role = el.getAttribute && (el.getAttribute("role") || "");
        if (role && /^(none|presentation)$/i.test(role)) return;
        const rect = el.getBoundingClientRect() || { x: 0, y: 0, width: 0, height: 0 };
 
        push({
          tag,
          role,
          id: el.id || "",
          name: el.getAttribute && el.getAttribute("name") || "",
          type: el.getAttribute && el.getAttribute("type") || "",
          text: textOf(el),
          aria_label: el.getAttribute && el.getAttribute("aria-label") || "",
          placeholder: el.getAttribute && el.getAttribute("placeholder") || "",
          label_text: accessibleName(el, doc, _lmap),
          title: el.getAttribute && el.getAttribute("title") || "",
          data_testid: el.getAttribute && el.getAttribute("data-testid") || "",
          data_id: el.getAttribute && el.getAttribute("data-id") || "",
          data_name: el.getAttribute && el.getAttribute("data-name") || "",
          data_field: el.getAttribute && el.getAttribute("data-field") || "",
          visible: true,
          bbox: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          frame: framePrefix
        });
      };
 
      const pickText = (el, doc, _lmap, framePrefix="") => {
        if (!isVisible(el) || el.closest('nav,header,footer,[aria-hidden="true"],[hidden]')) return;
        const tag = (el.tagName || "").toLowerCase();
        if (!/^(h1|h2|h3|h4|h5|h6|label|legend|span|strong|em|p|div)$/.test(tag)) return;
        const txt = textOf(el);
        if (!txt || txt.length < 2) return;
        const rect = el.getBoundingClientRect();
        if (!rect || rect.width < 1 || rect.height < 1) return;
 
        push({
          tag,
          role: el.getAttribute && (el.getAttribute("role") || ""),
          id: el.id || "",
          name: el.getAttribute && el.getAttribute("name") || "",
          type: "",
          text: txt,
          aria_label: el.getAttribute && el.getAttribute("aria-label") || "",
          placeholder: "",
          label_text: txt,
          title: el.getAttribute && el.getAttribute("title") || "",
          data_testid: el.getAttribute && el.getAttribute("data-testid") || "",
          data_id: el.getAttribute && el.getAttribute("data-id") || "",
          data_name: el.getAttribute && el.getAttribute("data-name") || "",
          data_field: el.getAttribute && el.getAttribute("data-field") || "",
          visible: true,
          bbox: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          frame: framePrefix
        });
      };
 
      const visit = (root, framePrefix="") => {
        if (!root) return;
        const doc = root;
        const _lmap = labelForMap(doc);
        const walker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
 
        while (walker.nextNode()) {
          const el = walker.currentNode;
          const tag = (el.tagName || "").toLowerCase();
          if (tag === "input" || tag === "button" || tag === "select" || tag === "textarea" || tag === "a" || el.hasAttribute("role"))
            pick(el, doc, _lmap, framePrefix);
          pickText(el, doc, _lmap, framePrefix);
 
          // shadow DOM traversal
          if (el.shadowRoot) {
            const w = doc.createTreeWalker(el.shadowRoot, NodeFilter.SHOW_ELEMENT);
            while (w.nextNode()) {
              const s = w.currentNode;
              const st = (s.tagName || "").toLowerCase();
              if (st === "input" || st === "button" || st === "select" || st === "textarea" || st === "a" || s.hasAttribute("role"))
                pick(s, el.shadowRoot, _lmap, framePrefix);
              pickText(s, el.shadowRoot, _lmap, framePrefix);
            }
          }
        }
 
        // ✅ NEW: recurse into same-origin iframes
        const iframes = doc.querySelectorAll("iframe");
        for (const f of iframes) {
          try {
            const id = f.id || f.name || "";
            const prefix = framePrefix + (id ? id : "iframe") + "/";
            if (f.contentDocument) visit(f.contentDocument, prefix);
          } catch (e) {
            // skip cross-origin frames
          }
        }
      };
 
      visit(document);
      return out;
    })();
    """

    try:
        elements = await fr.evaluate(js)
        print(f"[DEBUG] Extracted {len(elements)} DOM elements (including iframes).")
        # Optional: debug grouping by frame
        const_by_frame = {}
        for e in elements:
            f = e.get("frame", "")
            const_by_frame[f] = const_by_frame.get(f, 0) + 1
        print("[DEBUG] Elements grouped by frame:", const_by_frame)
        return elements
    except Exception as e:
        print("[ERROR] _rich_extract_dom_metadata failed:", e)
        return []


# -----------------------------------------------------------------------------
# Enrichment
# -----------------------------------------------------------------------------
async def _refresh_target(PAGE: Page, reason: str = ""):
    try:
        if PAGE is None:
            return
        TARGET = await _select_extraction_target(PAGE)
        _safe_log(
            f"[stability] TARGET refreshed ({reason}) → {getattr(TARGET,'url',None)}"
        )
    except Exception as e:
        _safe_log(f"[stability] TARGET refresh failed ({reason}): {e}")


def _get_ocr_data_by_canonical(
    canonical_page_name: str, projectChromaPath: Path
) -> List[Dict[str, Any]]:
    try:
        recs = _get_chroma_collection(projectChromaPath).get() or {}
        metas = filter_metadata_by_project(recs.get("metadatas", []) or [])
        return [
            m
            for m in metas
            if _canonical((m or {}).get("page_name", "")) == canonical_page_name
        ]
    except Exception:
        return []


def _assess_dom_quality(recs: List[Dict[str, Any]]) -> bool:
    if not recs:
        return True
    n = len(recs)
    labeled = 0
    with_bbox = 0
    for r in recs:
        if (
            r.get("aria_label")
            or r.get("placeholder")
            or r.get("label")
            or r.get("text")
            or r.get("label_text")
        ):
            labeled += 1
        bb = r.get("bbox") or {}
        if (bb.get("width", 0) or 0) > 0 and (bb.get("height", 0) or 0) > 0:
            with_bbox += 1
    return n < 5 or (labeled / max(1, n) < 0.30) or (with_bbox / max(1, n) < 0.30)


async def _run_enrichment_for(
    PAGE: Page,
    TARGET: Page | Frame | None,
    AUTOSCROLL_ENABLED: bool,
    EXECUTION_MODE: bool,
    src_directory: Path,
    projectChromaPath: Path,
    page_name: str,
) -> Dict[str, Any]:
    if PAGE is None:
        raise HTTPException(
            status_code=500, detail="❌ Cannot extract. No active page handle."
        )
    if hasattr(PAGE, "is_closed") and PAGE.is_closed():
        raise HTTPException(
            status_code=500, detail="❌ Cannot extract. Page is already closed."
        )
    # Ensure chroma path is available via project activation

    CURRENT_PAGE_NAME = _canonical(page_name)

    # >>> NEW: ensure we're on the correct page BEFORE extraction
    await _ensure_on_page(PAGE, CURRENT_PAGE_NAME, projectChromaPath)

    await _refresh_target(PAGE, "enrich-start")
    paths = _ensure_dirs(src_directory)

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
        await _progressive_autoscroll(
            AUTOSCROLL_ENABLED, EXECUTION_MODE, TARGET, steps=6, pause_ms=250
        )
    finally:
        AUTOSCROLL_ENABLED = prev_scroll

    # Attempt to open potential modals before extraction
    await _open_potential_modals(TARGET)

    # extract DOM
    # --- Diagnostic probe: record readyState, node count, and accessibility ---
    try:
        probe = {"url": getattr(TARGET, "url", None)}
        try:
            probe["readyState"] = await TARGET.evaluate("() => document.readyState")
        except Exception as _e:
            probe["readyState"] = f"eval-error: {_e}"
        try:
            probe["body_node_count"] = await TARGET.evaluate(
                "() => document.querySelectorAll('body *').length"
            )
        except Exception as _e:
            probe["body_node_count"] = f"eval-error: {_e}"
        try:
            probe["is_js_accessible"] = bool(await _is_js_accessible(TARGET))
        except Exception:
            probe["is_js_accessible"] = False
        debug_path = paths["debug"] / f"dom_eval_debug_{CURRENT_PAGE_NAME}.json"
        _write_project_file(
            debug_path,
            json.dumps(probe, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        try:
            error_path = (
                paths["debug"] / f"dom_eval_debug_{CURRENT_PAGE_NAME}_error.txt"
            )
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
            label = (
                rec.get("label_text")
                or rec.get("nearby_label")
                or rec.get("label")
                or rec.get("aria_label")
                or rec.get("text")
            )
            placeholder = rec.get("placeholder")
            role = rec.get("role")
            tag = rec.get("tag")
            rec["_norm"] = {
                "label": _norm_text(label),
                "nearby": _norm_text(rec.get("nearby_label")),
                "placeholder": _norm_text(placeholder),
                "role": (role or "").lower(),
                "tag": (tag or "").lower(),
            }
        except Exception:
            pass

    ocr_data = _get_ocr_data_by_canonical(CURRENT_PAGE_NAME, projectChromaPath)

    # debug dumps
    _write_project_file(
        paths["debug"] / f"dom_data_{CURRENT_PAGE_NAME}.txt",
        pprint.pformat(dom_data),
        encoding="utf-8",
    )
    _write_project_file(
        paths["debug"] / f"ocr_data_{CURRENT_PAGE_NAME}.txt",
        pprint.pformat(ocr_data),
        encoding="utf-8",
    )

    # matching
    updated_matches = match_and_update(
        ocr_data, dom_data, _get_chroma_collection(projectChromaPath)
    )
    _write_project_file(
        paths["debug"] / f"after_match_and_update_{CURRENT_PAGE_NAME}.txt",
        pprint.pformat(updated_matches),
        encoding="utf-8",
    )

    standardized_matches = [
        build_standard_metadata(
            m, CURRENT_PAGE_NAME, image_path="", source_url=getattr(PAGE, "url", None)
        )
        for m in (updated_matches or [])
    ]
    set_last_match_result(standardized_matches)

    # fallback if nothing matched
    if not standardized_matches:
        if dom_data:
            standardized_matches = [
                _standardize_dom_only(r, CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
                for r in dom_data
            ]
        else:
            standardized_matches = [
                _no_elements_record(CURRENT_PAGE_NAME, getattr(PAGE, "url", None))
            ]

    # write per-page JSON (merge with existing if present)
    out_path = _output_path_for_page(
        src_directory, CURRENT_PAGE_NAME, getattr(PAGE, "url", None)
    )
    try:
        existing_payload = (
            json.loads(out_path.read_text(encoding="utf-8") or "[]")
            if out_path.exists()
            else []
        )
    except Exception:
        existing_payload = []
    combined_payload = _merge_enrichment_records(existing_payload, standardized_matches)
    _write_project_file(
        out_path, json.dumps(combined_payload, indent=2), encoding="utf-8"
    )

    # refresh global snapshot
    chroma_all = _get_chroma_collection(projectChromaPath).get() or {}
    chroma_all_metadatas = filter_metadata_by_project(
        chroma_all.get("metadatas", []) or []
    )
    _write_project_file(
        paths["meta"] / "after_enrichment.json",
        json.dumps(chroma_all_metadatas, indent=2),
        encoding="utf-8",
    )

    _safe_log(f"[enrich] wrote: {out_path} ({len(standardized_matches)} records)")
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
async def _enrich_ocr_pages(
    PAGE: Page,
    TARGET: Page | Frame | None,
    AUTOSCROLL_ENABLED: bool,
    EXECUTION_MODE: bool,
    src_directory: Path,
    projectChromaPath: Path,
) -> Dict[str, Any]:
    pages = _available_pages_for_dropdown(projectChromaPath)
    results = []
    for pn in pages:
        try:
            _safe_log(f"[auto] OCR page -> {pn}")
            res = await _run_enrichment_for(
                PAGE,
                TARGET,
                AUTOSCROLL_ENABLED,
                EXECUTION_MODE,
                src_directory,
                projectChromaPath,
                pn,
            )
            results.append(
                {
                    "page_name": pn,
                    "count": res.get("count", 0),
                    "file": res.get("output_path"),
                }
            )
        except Exception as e:
            _safe_log(f"[auto] OCR page '{pn}' failed: {e}")
            results.append({"page_name": pn, "error": str(e), "count": 0})
    # If no OCR pages exist, enrich the current page at least once
    if not pages:
        pn = await _derive_page_name(PAGE)
        _safe_log(f"[auto] No OCR pages; enriching current: {pn}")
        res = await _run_enrichment_for(
            PAGE,
            TARGET,
            AUTOSCROLL_ENABLED,
            EXECUTION_MODE,
            src_directory,
            projectChromaPath,
            pn,
        )
        results.append(
            {
                "page_name": pn,
                "count": res.get("count", 0),
                "file": res.get("output_path"),
            }
        )
    return {"strategy": "ocr", "results": results}


async def _crawl_and_enrich(
    PAGE: Page,
    TARGET: Page | Frame | None,
    AUTOSCROLL_ENABLED: bool,
    EXECUTION_MODE: bool,
    src_directory: Path,
    projectChromaPath: Path,
    start_url: Optional[str],
    max_pages: int,
    max_depth: int,
    delay_ms: int,
    same_origin_only: bool,
) -> Dict[str, Any]:
    if PAGE is None:
        raise HTTPException(status_code=500, detail="No active page")
    seed = start_url or getattr(PAGE, "url", None)
    if not seed:
        raise HTTPException(status_code=400, detail="No start URL available for crawl")

    visited: Set[str] = set()
    queue: List[Tuple[str, int]] = [(seed, 0)]
    results: List[Dict[str, Any]] = []
    count = 0

    while queue and count < max_pages:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        _safe_log(f"[crawl] visiting d={depth} url={url}")
        try:
            await _smart_navigate(PAGE, url, wait_until="auto", timeout_ms=60000)
            await __snapshot_if_blank(PAGE, "crawl-visit")

            try:
                TARGET = await _select_extraction_target(PAGE)
            except Exception:
                TARGET = PAGE

            page_name = await _derive_page_name(PAGE)
            res = await _run_enrichment_for(
                PAGE,
                TARGET,
                AUTOSCROLL_ENABLED,
                EXECUTION_MODE,
                src_directory,
                projectChromaPath,
                page_name,
            )
            results.append(
                {
                    "url": url,
                    "page_name": page_name,
                    "count": res.get("count", 0),
                    "file": res.get("output_path"),
                }
            )
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


async def _auto_enrich(
    PAGE: Page,
    TARGET: Page | Frame | None,
    AUTOSCROLL_ENABLED: bool,
    EXECUTION_MODE: bool,
    src_directory: Path,
    projectChromaPath: Path,
    strategy: str,
    crawl_max_pages: int,
    crawl_max_depth: int,
    crawl_delay_ms: int,
    crawl_same_origin_only: bool,
) -> Dict[str, Any]:
    strat = (strategy or "mixed").lower().strip()
    if strat not in {"ocr", "crawl", "mixed"}:
        strat = "mixed"
    has_ocr_pages = bool(_available_pages_for_dropdown(projectChromaPath))

    if strat == "ocr":
        return await _enrich_ocr_pages(
            PAGE,
            TARGET,
            AUTOSCROLL_ENABLED,
            EXECUTION_MODE,
            src_directory,
            projectChromaPath,
        )

    if strat == "crawl":
        return await _crawl_and_enrich(
            PAGE,
            TARGET,
            AUTOSCROLL_ENABLED,
            EXECUTION_MODE,
            src_directory=src_directory,
            projectChromaPath=projectChromaPath,
            start_url=getattr(PAGE, "url", None),
            max_pages=crawl_max_pages,
            max_depth=crawl_max_depth,
            delay_ms=crawl_delay_ms,
            same_origin_only=crawl_same_origin_only,
        )

    # mixed
    if has_ocr_pages:
        return await _enrich_ocr_pages(
            PAGE,
            TARGET,
            AUTOSCROLL_ENABLED,
            EXECUTION_MODE,
            src_directory,
            projectChromaPath,
        )
    return await _crawl_and_enrich(
        PAGE,
        TARGET,
        AUTOSCROLL_ENABLED,
        EXECUTION_MODE,
        src_directory=src_directory,
        projectChromaPath=projectChromaPath,
        start_url=getattr(PAGE, "url", None),
        max_pages=crawl_max_pages,
        max_depth=crawl_max_depth,
        delay_ms=crawl_delay_ms,
        same_origin_only=crawl_same_origin_only,
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@router.post("/{project_id}/launch-browser")
async def launch_browser(
    project_id: int,
    req: LaunchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    PLAYWRIGHT = None
    BROWSER: Optional[Browser] = None
    PAGE: Optional[Page] = None
    TARGET: Optional[Union[Page, Frame]] = None
    CURRENT_PAGE_NAME: str = "unknown_page"
    EXECUTION_MODE: bool = False
    ENRICH_UI_ENABLED: bool = False
    AUTOSCROLL_ENABLED: bool = True

    project = get_user_project(db, project_id, current_user)
    project_paths = _ensure_project_structure(project)
    src_directory = Path(project_paths["src_dir"])
    projectChromaPath = project_paths["chroma_path"]
    storage = DatabaseBackedProjectStorage(project, src_directory, db)
    session = await _create_or_reset_session(project.id, src_directory, projectChromaPath)
    if session.page or session.browser or session.playwright:
        await _clean_restart(session.page, session.browser, session.playwright)
    _set_active_storage(storage)
    try:
        await _clean_restart(PAGE, BROWSER, PLAYWRIGHT)
        PLAYWRIGHT = await async_playwright().start()

        launch_args = []
        if req.disable_pinch_zoom:
            launch_args += [
                "--disable-pinch",
                "--force-device-scale-factor=1",
                "--high-dpi-support=1",
                "--overscroll-history-navigation=0",
            ]
        if req.disable_gpu:
            launch_args += [
                "--disable-gpu",
                "--disable-accelerated-2d-canvas",
                "--disable-features=IsolateOrigins,site-per-process",
            ]

        headless = req.headless
        if not auth_storage_path(Path(project_paths["project_root"])).exists():
            headless = False
        BROWSER = await PLAYWRIGHT.chromium.launch(
            headless=headless, slow_mo=req.slow_mo, args=launch_args
        )

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
            context_kwargs["http_credentials"] = {
                "username": req.http_username,
                "password": req.http_password,
            }

        ENRICH_UI_ENABLED = bool(req.enable_enrichment_ui)
        AUTOSCROLL_ENABLED = bool(req.enable_autoscroll)
        session.enrich_ui_enabled = ENRICH_UI_ENABLED
        session.autoscroll_enabled = AUTOSCROLL_ENABLED

        # Resolve optional storage file (cookies / localStorage) and apply to context kwargs if present
        storage_file = auth_storage_path(Path(project_paths["project_root"]))
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
        session.playwright = PLAYWRIGHT
        session.browser = BROWSER
        session.page = PAGE

        PAGE.on(
            "framenavigated",
            lambda frame: asyncio.create_task(_refresh_target(PAGE, "framenavigated")),
        )
        PAGE.on(
            "framedetached",
            lambda frame: asyncio.create_task(_refresh_target(PAGE, "framedetached")),
        )
        PAGE.on(
            "crash", lambda: asyncio.create_task(_refresh_target(PAGE, "page crash"))
        )

        async def _binding_enrich(source, page_name: str):
            try:
                with session_scope() as scoped_db:
                    scoped_project = (
                        scoped_db.query(Project)
                        .filter(Project.id == project.id)
                        .first()
                    )
                    if not scoped_project:
                        raise HTTPException(status_code=404, detail="Project not found.")
                    storage = DatabaseBackedProjectStorage(scoped_project, src_directory, scoped_db)
                    _set_active_storage(storage)
                    with _temporary_project_env(project_paths, scoped_project.id):
                        res = await _run_enrichment_for(
                            PAGE,
                            TARGET,
                            AUTOSCROLL_ENABLED,
                            EXECUTION_MODE,
                            src_directory,
                            projectChromaPath,
                            page_name,
                        )
                return json.dumps(res)
            except HTTPException as he:
                return json.dumps({"status": "fail", "error": he.detail})
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e)})
            finally:
                _set_active_storage(None)

        async def _binding_available_pages(source):
            try:
                with _temporary_project_env(project_paths, project.id):
                    pages = _available_pages_for_dropdown(projectChromaPath)
                return json.dumps({"status": "success", "pages": pages})
            except Exception as e:
                return json.dumps({"status": "fail", "error": str(e), "pages": []})

        await PAGE.expose_binding("smartAI_enrich", _binding_enrich)
        await PAGE.expose_binding("smartAI_availablePages", _binding_available_pages)

        if req.apply_visual_patches:
            await PAGE.add_init_script(STABILITY_VIEWPORT_CSS_JS)
        if req.enable_watchdog_reload:
            await PAGE.add_init_script(WATCHDOG_RELOAD_JS)
        if ENRICH_UI_ENABLED:
            await PAGE.add_init_script(UI_KEYBRIDGE_JS)
            await PAGE.add_init_script(UI_MODAL_TOP_JS)

        try:
            PAGE.set_default_timeout(req.nav_timeout_ms)
            PAGE.set_default_navigation_timeout(req.nav_timeout_ms)
        except Exception:
            pass

        await _smart_navigate(
            PAGE,
            req.url,
            wait_until=req.wait_until if req.wait_until else "auto",
            timeout_ms=req.nav_timeout_ms,
        )
        if should_start_auth_watch(auth_storage_path(Path(project_paths["project_root"])), getattr(PAGE, "url", "")):
            if session.auth_watch_task and not session.auth_watch_task.done():
                session.auth_watch_task.cancel()
            session.auth_watch_task = asyncio.create_task(
                wait_for_login_and_save(PAGE, Path(project_paths["project_root"]))
            )
        await __snapshot_if_blank(PAGE, "after-nav")
        try:
            TARGET = await _select_extraction_target(PAGE)
        except Exception:
            TARGET = PAGE
        session.target = TARGET

        auto_result = None
        if req.auto_enrich:
            _safe_log(
                f"[auto] Starting auto-enrichment strategy='{req.enrich_strategy}'"
            )
            with _temporary_project_env(project_paths, project.id):
                auto_result = await _auto_enrich(
                    PAGE,
                    TARGET,
                    AUTOSCROLL_ENABLED,
                    EXECUTION_MODE,
                    src_directory=src_directory,
                    projectChromaPath=projectChromaPath,
                    strategy=req.enrich_strategy,
                    crawl_max_pages=req.crawl_max_pages,
                    crawl_max_depth=req.crawl_max_depth,
                    crawl_delay_ms=req.crawl_delay_ms,
                    crawl_same_origin_only=req.crawl_same_origin_only,
                )
            session.target = TARGET
            _safe_log(
                f"[auto] Completed auto-enrichment with {len(auto_result.get('results', []))} item(s)"
            )

        msg = f"✅ Browser launched and navigated to {req.url}."
        if ENRICH_UI_ENABLED:
            msg += " Modal available (Alt+Q)."
        if req.auto_enrich and auto_result:
            msg += f" Auto-enrichment finished using '{auto_result.get('strategy')}'."

        # AUTO-CLOSE when requested
        if req.auto_enrich and req.close_after_enrich:
            try:
                await _clean_restart(PAGE, BROWSER, PLAYWRIGHT)
                session.playwright = None
                session.browser = None
                session.page = None
                session.target = None
                msg += " Browser closed after auto-enrichment."
            except Exception:
                pass

        return {"status": "success", "message": msg, "auto_enrich_result": auto_result}

    except Exception as e:
        import traceback

        traceback.print_exc()
        await _clean_restart(PAGE, BROWSER, PLAYWRIGHT)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)


@router.post("/{project_id}/auth/session/save")
async def save_project_auth_session(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = get_user_project(db, project_id, current_user)
    project_paths = _ensure_project_structure(project)
    session = await _get_session(project.id)
    if session.page is None or (hasattr(session.page, "is_closed") and session.page.is_closed()):
        raise HTTPException(status_code=409, detail="No active page to capture storage state.")
    path = auth_storage_path(Path(project_paths["project_root"]))
    try:
        await session.page.context.storage_state(path=str(path))
        try:
            current_url = session.page.url or ""
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


@router.post("/auto-enrich")
async def auto_enrich_endpoint(
    req: AutoEnrichRequest,
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    project = (
        db.query(Project).filter(Project.id == project_id).first()
        if project_id
        else _get_active_project(db)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    project_paths = _ensure_project_structure(project)
    session = await _get_session(project.id)
    src_directory = session.src_directory or Path(project_paths["src_dir"])
    project_chroma = session.chroma_path or Path(project_paths["chroma_path"])
    storage = DatabaseBackedProjectStorage(project, src_directory, db)
    _set_active_storage(storage)
    try:
        with _temporary_project_env(project_paths, project.id):
            res = await _auto_enrich(
                session.page,
                session.target,
                session.autoscroll_enabled,
                session.execution_mode,
                src_directory=src_directory,
                projectChromaPath=project_chroma,
                strategy=req.enrich_strategy,
                crawl_max_pages=req.crawl_max_pages,
                crawl_max_depth=req.crawl_max_depth,
                crawl_delay_ms=req.crawl_delay_ms,
                crawl_same_origin_only=req.crawl_same_origin_only,
            )
        if req.close_after_enrich:
            try:
                await _clean_restart(session.page, session.browser, session.playwright)
                session.playwright = None
                session.browser = None
                session.page = None
                session.target = None
            except Exception:
                pass
        return {"status": "success", "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)


@router.post("/crawl-and-enrich")
async def crawl_and_enrich_endpoint(
    req: CrawlRequest,
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    project = (
        db.query(Project).filter(Project.id == project_id).first()
        if project_id
        else _get_active_project(db)
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    project_paths = _ensure_project_structure(project)
    session = await _get_session(project.id)
    src_directory = session.src_directory or Path(project_paths["src_dir"])
    project_chroma = session.chroma_path or Path(project_paths["chroma_path"])
    storage = DatabaseBackedProjectStorage(project, src_directory, db)
    _set_active_storage(storage)
    try:
        with _temporary_project_env(project_paths, project.id):
            res = await _crawl_and_enrich(
                session.page,
                session.target,
                session.autoscroll_enabled,
                session.execution_mode,
                src_directory=src_directory,
                projectChromaPath=project_chroma,
                start_url=req.start_url or getattr(session.page, "url", None),
                max_pages=req.max_pages,
                max_depth=req.max_depth,
                delay_ms=req.delay_ms,
                same_origin_only=req.same_origin_only,
            )
        if req.close_after_enrich:
            try:
                await _clean_restart(session.page, session.browser, session.playwright)
                session.playwright = None
                session.browser = None
                session.page = None
                session.target = None
            except Exception:
                pass
        return {"status": "success", "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _set_active_storage(None)


@router.post("/set-current-page-name")
async def set_page_name(req: PageNameSetRequest, project_id: Optional[int] = None):
    pid = project_id
    if pid is None:
        pid_val = os.environ.get("SMARTAI_PROJECT_ID")
        pid = int(pid_val) if pid_val and pid_val.isdigit() else None
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    session = await _get_session(pid)
    session.current_page_name = _canonical(req.page_name)
    _safe_log(f"[INFO] ✅ Page name set to: {session.current_page_name}")
    return {"status": "success", "page_name": session.current_page_name}


@router.post("/execution-mode")
async def toggle_execution_mode(req: ExecutionModeRequest, project_id: Optional[int] = None):
    pid = project_id
    if pid is None:
        pid_val = os.environ.get("SMARTAI_PROJECT_ID")
        pid = int(pid_val) if pid_val and pid_val.isdigit() else None
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    session = await _get_session(pid)
    session.execution_mode = bool(req.enabled)
    session.autoscroll_enabled = False if session.execution_mode else session.autoscroll_enabled
    if session.execution_mode and session.page:
        try:
            await session.page.evaluate(
                "window._smartaiDisabled = true; if (window.smartAI_disableUI) window.smartAI_disableUI();"
            )
        except Exception:
            pass
        session.enrich_ui_enabled = False
    if session.page:
        try:
            session.target = await _select_extraction_target(session.page)
        except Exception:
            session.target = session.page
    return {"status": "success", "execution_mode": session.execution_mode}


@router.post("/ui/disable")
async def disable_ui(project_id: Optional[int] = None):
    pid = project_id
    if pid is None:
        pid_val = os.environ.get("SMARTAI_PROJECT_ID")
        pid = int(pid_val) if pid_val and pid_val.isdigit() else None
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    session = await _get_session(pid)
    session.enrich_ui_enabled = False
    if session.page:
        try:
            await session.page.evaluate(
                "window._smartaiDisabled = true; if (window.smartAI_disableUI) window.smartAI_disableUI();"
            )
        except Exception:
            pass
    return {"status": "success", "ui_enabled": session.enrich_ui_enabled}


@router.post("/capture-dom-from-client")
async def capture_from_keyboard(
    _: CaptureRequest,
    project_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    # Manual trigger kept; does NOT auto-close.
    pid = project_id
    if pid is None:
        pid_val = os.environ.get("SMARTAI_PROJECT_ID")
        pid = int(pid_val) if pid_val and pid_val.isdigit() else None
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    session = await _get_session(pid)
    try:
        if session.page is None:
            raise HTTPException(
                status_code=500, detail="❌ Cannot extract. No active page handle."
            )
        if hasattr(session.page, "is_closed") and session.page.is_closed():
            raise HTTPException(
                status_code=500, detail="❌ Cannot extract. Page is already closed."
            )
        if not session.current_page_name:
            session.current_page_name = await _derive_page_name(session.page)

        page_name = session.current_page_name
        _safe_log(f"[INFO] Enrichment triggered for: {page_name}")

        await _refresh_target(session.page, "capture-start")
        await __snapshot_if_blank(session.page, "before-capture")

        prev_scroll = session.autoscroll_enabled
        try:
            session.autoscroll_enabled = True
            await _progressive_autoscroll(session.target, steps=6, pause_ms=250)
        finally:
            session.autoscroll_enabled = prev_scroll

        project = db.query(Project).filter(Project.id == pid).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")
        project_paths = _ensure_project_structure(project)
        storage = DatabaseBackedProjectStorage(project, session.src_directory, db)
        _set_active_storage(storage)
        with _temporary_project_env(project_paths, project.id):
            result = await _run_enrichment_for(
                session.page,
                session.target,
                session.autoscroll_enabled,
                session.execution_mode,
                session.src_directory,
                session.chroma_path,
                page_name,
            )
        await __snapshot_if_blank(session.page, "after-capture")
        return {
            "status": "success",
            "message": f"[Keyboard Trigger] {result['message']}",
            **result,
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500, detail=f"❌ Capture failed: {e.__class__.__name__}: {e}"
        )
    finally:
        _set_active_storage(None)


@router.get("/available-pages")
async def list_page_names(project_id: Optional[int] = None, db: Session = Depends(get_db)):
    pid = project_id
    if pid is None:
        pid_val = os.environ.get("SMARTAI_PROJECT_ID")
        pid = int(pid_val) if pid_val and pid_val.isdigit() else None
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    project = db.query(Project).filter(Project.id == pid).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    project_paths = _ensure_project_structure(project)
    try:
        with _temporary_project_env(project_paths, project.id):
            return {
                "status": "success",
                "pages": _available_pages_for_dropdown(project_paths["chroma_path"]),
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/current-url")
async def current_url(project_id: Optional[int] = None):
    pid = project_id
    if pid is None:
        pid_val = os.environ.get("SMARTAI_PROJECT_ID")
        pid = int(pid_val) if pid_val and pid_val.isdigit() else None
    if pid is None:
        raise HTTPException(status_code=400, detail="project_id is required")
    session = await _get_session(pid)
    try:
        target_url = None
        if session.target is not None:
            try:
                target_url = session.target.url
            except Exception:
                target_url = None
        return {
            "page_url": getattr(session.page, "url", None),
            "target_url": target_url,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.on_event("shutdown")
async def shutdown_browser():
    page = globals().get("PAGE")
    browser = globals().get("BROWSER")
    playwright = globals().get("PLAYWRIGHT")
    if page or browser or playwright:
        await _clean_restart(page, browser, playwright)


@router.get("/latest-match-result")
async def get_latest_match_result():
    try:
        records = _get_chroma_collection().get()
        matched = [
            r for r in records.get("metadatas", []) if r.get("dom_matched") is True
        ]
        return {"status": "success", "matched_elements": matched, "count": len(matched)}
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reset-enrichment/{page_name}")
async def reset_enrichment_api(page_name: str):
    reset_enriched(page_name)
    return {"success": True, "message": f"Enrichment reset for {page_name}"}


__all__ = ["router"]
