import logging
import os
import subprocess
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from metrics.collector import collect_run_summary
from metrics.store import MetricsStore
from utils.allure_results_parser import parse_allure_results
from .report_api import _resolve_src_dir
from .projects_api import _ensure_project_structure, get_current_user, get_user_project
from database.models import User
from database.session import get_db

router = APIRouter()
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
VISUALIZER_SCRIPT = REPO_ROOT / "allure_reports" / "allure_visualizer.py"
RUN_LOCK_NAME = ".allure_run.lock"


def _shorten_output(text: str, limit: int = 400) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}... (truncated)"


def _ensure_env(src_dir: Path, project_dir: Path, keep_browser: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "")
    parts = [str(src_dir)]
    if pythonpath:
        parts.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["SMARTAI_SRC_DIR"] = str(src_dir)
    env["SMARTAI_PROJECT_DIR"] = str(project_dir)
    os.environ["SMARTAI_SRC_DIR"] = str(src_dir)
    os.environ["SMARTAI_PROJECT_DIR"] = str(project_dir)
    hold_value = keep_browser or env.get("UI_KEEP_BROWSER_OPEN") or os.environ.get("UI_KEEP_BROWSER_OPEN")
    if not hold_value:
        hold_value = "30"  # give UI time to stay visible by default
    env["UI_KEEP_BROWSER_OPEN"] = hold_value
    env["SMARTAI_SKIP_PLAYWRIGHT_FIXTURES"] = "1"
    env["ALLURE_RESULTS_DIR"] = str(src_dir / "allure-results")

    return env


def _run_script(script_path: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        script_to_run = script_path.relative_to(cwd)
    except ValueError:
        script_to_run = script_path
    cmd = [sys.executable, str(script_to_run)]
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True)


def _run_behave_feature(
    feature_path: Path,
    cwd: Path,
    env: dict[str, str],
    results_dir: Path,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "behave",
        str(feature_path),
        "-f",
        "allure_behave.formatter:AllureFormatter",
        "-o",
        str(results_dir),
    ]
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True)


def _generate_report(results_dir: Path, report_dir: Path, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    allure_exe = shutil.which("allure")
    if not allure_exe:
        raise HTTPException(status_code=501, detail="Allure CLI is not installed. Please install it so the report can be generated.")
    cmd = [allure_exe, "generate", str(results_dir), "-o", str(report_dir), "--clean"]
    return subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True)


def _run_allure_visualizer(results_dir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str] | None:
    if not VISUALIZER_SCRIPT.exists():
        logger.warning("Allure visualizer script not found at %s; skipping chart generation.", VISUALIZER_SCRIPT)
        return None

    cmd = [
        sys.executable,
        str(VISUALIZER_SCRIPT),
        "--results-dir",
        str(results_dir),
        "--interactive",
        "--interactive-only",
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, text=True, capture_output=True)
    if result.returncode != 0:
        logger.warning(
            "Allure visualizer failed with exit code %s: %s",
            result.returncode,
            _shorten_output(result.stderr or result.stdout),
        )
    else:
        charts_dir = results_dir.parent / "allure_charts"
        logger.info("Interactive charts written to %s", charts_dir)
    return result


def _resolve_requested_script_path(tests_dir: Path, script_path: str) -> Path:
    requested = Path(script_path)

    if requested.is_absolute():
        raise HTTPException(status_code=400, detail="Absolute script paths are not allowed.")

    if requested.parts and requested.parts[0].lower() == "tests":
        requested = Path(*requested.parts[1:])

    candidate = (tests_dir / requested).resolve()
    tests_dir_resolved = tests_dir.resolve()

    if not str(candidate).startswith(str(tests_dir_resolved)):
        raise HTTPException(
            status_code=403,
            detail="Requested script path must stay within the tests directory.",
        )

    return candidate


def _discover_script_targets(tests_dir: Path) -> list[Path]:
    matches: list[Path] = []
    for pattern in ("*_script_*.py", "*_script.py"):
        matches.extend(sorted(tests_dir.rglob(pattern)))
    unique = []
    seen = set()
    for path in matches:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _execute_scripts(
    scripts: list[Path],
    src_dir: Path,
    env: dict[str, str],
) -> None:
    failures = []
    for script_path in scripts:
        result = _run_script(script_path, src_dir, env)
        if result.returncode != 0:
            failures.append((script_path, result))
    if failures:
        script_path, failure = failures[0]
        detail = _shorten_output(failure.stdout or failure.stderr)
        raise HTTPException(
            status_code=500,
            detail=f"Script run failed for {script_path}: {detail or 'see server logs for details'}",
        )


def _reset_dir(directory: Path) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)


def _acquire_run_lock(src_dir: Path) -> Path:
    lock_path = src_dir / RUN_LOCK_NAME
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="A test run is already in progress.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\n")
    return lock_path


def _release_run_lock(lock_path: Path) -> None:
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        logger.warning("Failed to remove run lock at %s", lock_path)


@router.get("/run")
def run_tests(
    test: str | None = Query(
        None,
        description="Relative path (under tests/) to a script file to execute. Omit to run all script files.",
    ),
    keep_browser: str | None = None,
):
    src_dir = _resolve_src_dir()
    tests_dir = src_dir / "tests"
    if not tests_dir.is_dir():
        raise HTTPException(status_code=404, detail="Tests directory not found.")

    allure_results_dir = src_dir / "allure-results"
    allure_report_dir = src_dir / "allure-report"
    project_dir = src_dir.parents[1] if len(src_dir.parents) > 1 else src_dir.parent
    env = _ensure_env(src_dir, project_dir, keep_browser)
    lock_path = _acquire_run_lock(src_dir)
    try:
        _reset_dir(allure_results_dir)
        _reset_dir(allure_report_dir)
        if test:
            script_target = _resolve_requested_script_path(tests_dir, test)
            if not script_target.exists():
                raise HTTPException(status_code=404, detail=f"Script file not found at {script_target}")
            scripts_to_run = [script_target]
        else:
            scripts_to_run = _discover_script_targets(tests_dir)
            if not scripts_to_run:
                raise HTTPException(status_code=404, detail="No script files found under tests/")

        _execute_scripts(scripts_to_run, src_dir, env)

        report_result = _generate_report(allure_results_dir, allure_report_dir, src_dir, env)
        if report_result.returncode != 0:
            detail = _shorten_output(report_result.stderr or report_result.stdout)
            raise HTTPException(
                status_code=500,
                detail=f"Allure report generation failed: {detail or 'see server logs for details'}",
            )

        _run_allure_visualizer(allure_results_dir, env)

        try:
            summary = collect_run_summary(allure_results_dir)
            if summary:
                history_path = src_dir / "history.json"
                store = MetricsStore(history_path=history_path)
                store.record_run(summary)
        except Exception as exc:
            logger.warning("Failed to update metrics history: %s", exc)
    finally:
        _release_run_lock(lock_path)

    return {
        "status": "ok",
        "allure_results": str(allure_results_dir),
        "allure_report": str(allure_report_dir),
        "report_url": "/reports/view",
        "allure_charts": str(allure_results_dir.parent / "allure_charts"),
    }

@router.get("/run-all")
def run_all_tests(keep_browser: str | None = None):
    return run_tests(test=None, keep_browser=keep_browser)


@router.post("/api/run-feature")
def run_api_feature(
    project_id: int | None = Query(None),
    keep_browser: str | None = None,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if project_id is not None:
        project = get_user_project(db, project_id, current_user)
        project_paths = _ensure_project_structure(project)
        src_dir = Path(project_paths["src_dir"])
    else:
        src_dir = _resolve_src_dir()
    tests_dir = src_dir / "tests"
    feature_path = tests_dir / "api_test" / "user_story.feature"
    if not feature_path.exists():
        raise HTTPException(status_code=404, detail="API feature file not found.")

    project_dir = src_dir.parents[1] if len(src_dir.parents) > 1 else src_dir.parent
    env = _ensure_env(src_dir, project_dir, keep_browser)
    allure_results_dir = src_dir / "allure-results"
    allure_report_dir = src_dir / "allure-report"
    lock_path = _acquire_run_lock(src_dir)
    try:
        _reset_dir(allure_results_dir)
        _reset_dir(allure_report_dir)
        result = _run_behave_feature(feature_path, src_dir, env, allure_results_dir)

        if result.returncode != 0:
            detail = _shorten_output(result.stderr or result.stdout)
            raise HTTPException(
                status_code=500,
                detail=f"Behave run failed: {detail or 'see server logs for details'}",
            )

        report_result = _generate_report(allure_results_dir, allure_report_dir, src_dir, env)
        if report_result.returncode != 0:
            detail = _shorten_output(report_result.stderr or report_result.stdout)
            raise HTTPException(
                status_code=500,
                detail=f"Allure report generation failed: {detail or 'see server logs for details'}",
            )

        _run_allure_visualizer(allure_results_dir, env)
    finally:
        _release_run_lock(lock_path)

    return {
        "status": "ok",
        "feature": str(feature_path),
        "stdout": _shorten_output(result.stdout),
        "stderr": _shorten_output(result.stderr),
        "allure_results": str(allure_results_dir),
        "allure_report": str(allure_report_dir),
        "report_url": "/reports/view",
        "allure_charts": str(allure_results_dir.parent / "allure_charts"),
    }


@router.get("/report")
def report():
    return {"report_url": "/reports/view"}


@router.get("/open")
def open_report():
    return {"report_url": "/reports/view"}


@router.get("/visualize")
def visualize_results():
    src_dir = _resolve_src_dir()
    results_dir = src_dir / "allure-results"
    try:
        return parse_allure_results(results_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Allure results not found. Run tests first.")
