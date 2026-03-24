# app/api.py

from fastapi import FastAPI, Body, HTTPException
from fastapi.staticfiles import StaticFiles
from orchestrator.orchestrator import send_message

import subprocess
import sys
import shutil
from pathlib import Path

app = FastAPI()

# Directory where generated HTML reports will be placed and served from
reports_dir = Path(__file__).resolve().parent.parent / "static_reports"
reports_dir.mkdir(parents=True, exist_ok=True)
app.mount("/reports", StaticFiles(directory=str(reports_dir)), name="reports")


@app.post("/mcp/")
def mcp_endpoint(language: str = Body(...), action: str = Body(...), payload: dict = Body(...)):
    resp = send_message(language, action, payload)
    return resp.__dict__


@app.get("/tests/report")
def generate_test_report(test: str = "tests/test_1.py"):
    """Generate an Allure HTML report for a single test file and return a URL to view it.

    The function runs pytest for the requested `test` inside `generated_runs/src`,
    collects Allure results, generates HTML using the `allure` CLI and places the
    generated HTML under `backend/static_reports/{test_name}` which is served at
    `/reports/{test_name}/index.html`.
    """
    # Resolve paths
    repo_root = Path(__file__).resolve().parent.parent
    src_root = repo_root / "generated_runs" / "src"
    if not src_root.exists():
        raise HTTPException(status_code=404, detail=f"Source dir not found: {src_root}")

    results_dir = src_root / "allure-results-temp"
    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    test_path = test
    test_name = Path(test_path).stem
    html_out = reports_dir / test_name
    if html_out.exists():
        shutil.rmtree(html_out)
    html_out.mkdir(parents=True, exist_ok=True)

    pytest_cmd = [sys.executable, "-m", "pytest", test_path, f"--alluredir={results_dir}"]
    try:
        subprocess.run(pytest_cmd, cwd=src_root, check=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run pytest: {e}")

    # Require Allure CLI to be available
    allure_exe = shutil.which("allure")
    if not allure_exe:
        raise HTTPException(status_code=500, detail="Allure CLI not found on PATH. Install it and ensure `allure` is available.")

    gen_cmd = [allure_exe, "generate", str(results_dir), "-o", str(html_out), "--clean"]
    try:
        subprocess.run(gen_cmd, cwd=src_root, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Allure generate failed: {e}")

    report_url = f"/reports/{test_name}/index.html"
    return {"report_url": report_url}


@app.get("/tests/run")
def run_all_tests():
    """Run all tests under `generated_runs/src/tests` and return aggregated report URL."""
    repo_root = Path(__file__).resolve().parent.parent
    src_root = repo_root / "generated_runs" / "src"
    if not src_root.exists():
        raise HTTPException(status_code=404, detail=f"Source dir not found: {src_root}")

    results_dir = src_root / "allure-results"
    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    pytest_cmd = [sys.executable, "-m", "pytest", "tests", f"--alluredir={results_dir}"]
    try:
        subprocess.run(pytest_cmd, cwd=src_root, check=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to run pytest: {e}")

    allure_exe = shutil.which("allure")
    if not allure_exe:
        raise HTTPException(status_code=500, detail="Allure CLI not found on PATH. Install it and ensure `allure` is available.")

    html_out = reports_dir / "all"
    if html_out.exists():
        shutil.rmtree(html_out)
    html_out.mkdir(parents=True, exist_ok=True)

    gen_cmd = [allure_exe, "generate", str(results_dir), "-o", str(html_out), "--clean"]
    try:
        subprocess.run(gen_cmd, cwd=src_root, check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Allure generate failed: {e}")

    return {"report_url": "/reports/all/index.html"}
