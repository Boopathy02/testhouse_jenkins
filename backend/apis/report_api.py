#report_api.py 
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
import mimetypes

from pytest import Session

from .projects_api import _ensure_project_structure, get_current_user, get_project_by_projectId, get_user_project
from database.models import User
from database.session import get_db

router = APIRouter()

def _resolve_src_dir() -> Path:
    """
    Find generated_runs/src by:
    - SMARTAI_SRC_DIR if set and exists
    - backend/*/generated_runs/src
    - backend/generated_runs/src (legacy)
    """
    import os

    env_src = os.environ.get("SMARTAI_SRC_DIR")
    if env_src:
        p = Path(env_src)
        if p.exists():
            return p

    backend_root = Path(__file__).resolve().parents[1]  # backend/
    for child in backend_root.iterdir():
        cand = child / "generated_runs" / "src"
        if cand.exists():
            return cand

    orgs_root = backend_root / "organizations"
    if orgs_root.is_dir():
        for org_dir in orgs_root.iterdir():
            if not org_dir.is_dir():
                continue
            for project_dir in org_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                cand = project_dir / "generated_runs" / "src"
                if cand.exists():
                    return cand

    legacy = backend_root / "generated_runs" / "src"
    if legacy.exists():
        return legacy

    raise HTTPException(status_code=404, detail="No generated_runs/src found")

@router.get("/latest")
def get_latest_report():
    """
    Return the latest available report HTML.
    Prefers Allure HTML at generated_runs/src/allure-report/index.html,
    otherwise falls back to generated_runs/src/report.html.
    """
    src_dir = _resolve_src_dir()
    allure_index = src_dir / "allure-report" / "index.html"
    html_fallback = src_dir / "index.html"

    # Keep this endpoint for compatibility but prefer the HTML-serving endpoints below.
    if allure_index.exists():
        return Response(content=allure_index.read_text(encoding="utf-8"), media_type="text/html")
    if html_fallback.exists():
        return Response(content=html_fallback.read_text(encoding="utf-8"), media_type="text/html")

    return {"error": "No report found. Run tests first."}


def _get_report_dir() -> Path:
    """Return the directory that should be served for report assets.
    Prefers `allure-report` inside generated src; otherwise uses src root (for pytest-html fallback).
    """
    src_dir = _resolve_src_dir()
    allure_dir = src_dir / "allure-report"
    if allure_dir.exists():
        return allure_dir
    return src_dir

@router.get("/view/{project_id}/{path:path}")
def serve_allure_report(
    project_id: int,
    path: str = "",
    db: Session = Depends(get_db),
):
    project = get_project_by_projectId(db, project_id)
    project_paths = _ensure_project_structure(project)

    report_dir = Path(project_paths["src_dir"]) / "allure-report"

    if not path or path.endswith("/"):
        path = "index.html"

    file_path = report_dir / path

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Allure report file not found")

    return FileResponse(file_path)


@router.get("/{asset_path:path}")
def report_assets(asset_path: str):
    """Serve static assets referenced by the report (styles, js, images).
    This catches requests like `/reports/static/js/app.js` and resolves them
    relative to the selected report directory.
    """
    report_dir = _get_report_dir()
    # Prevent path traversal by resolving and ensuring under report_dir
    candidate = (report_dir / asset_path).resolve()
    try:
        report_dir_resolved = report_dir.resolve()
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid report directory")
    if not str(candidate).startswith(str(report_dir_resolved)):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="Asset not found")
    mime, _ = mimetypes.guess_type(str(candidate))
    return FileResponse(candidate, media_type=mime or "application/octet-stream")

@router.get("/all")
def list_reports():
    """
    List available report artifacts under generated_runs/src.
    """
    src_dir = _resolve_src_dir()
    allure_report = src_dir / "allure-report" / "index.html"
    html_fallback = src_dir / "index.html"

    reports = []
    if allure_report.exists():
        reports.append({"type": "allure", "path": str(allure_report)})
    if html_fallback.exists():
        reports.append({"type": "html", "path": str(html_fallback)})

    return {"reports": reports}
