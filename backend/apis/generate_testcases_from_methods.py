from fastapi import APIRouter, HTTPException
from pathlib import Path
import re
import os

router = APIRouter()

def next_test_index(tests_dir):
    """Returns the next test index for file naming."""
    test_files = list(tests_dir.glob("test_*.py"))
    if not test_files:
        return 1
    indices = [
        int(re.search(r"test_(\d+)\.py", f.name).group(1))
        for f in test_files if re.match(r"test_\d+\.py", f.name)
    ]
    return max(indices, default=0) + 1

def read_page_methods(page_method_path):
    """Reads the contents of a generated page methods file."""
    with open(page_method_path, "r", encoding="utf-8") as f:
        return f.read()

@router.post("/rag/generate-from-method")
def generate_test_from_methods():
    src_dir = os.environ.get("SMARTAI_SRC_DIR")
    if not src_dir:
        raise HTTPException(status_code=400, detail="No active project. Start a project to set SMARTAI_SRC_DIR.")
    run_folder = Path(src_dir)
    pages_dir = run_folder / "pages"
    tests_dir = run_folder / "tests"
    metadata_dir = run_folder / "metadata"

    # clean_old_files(tests_dir, age_seconds=3600)
    # clean_old_files(metadata_dir, age_seconds=3600)

    tests_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    # Find all *_page_methods.py in pages_dir
    method_files = list(pages_dir.glob("*_page_methods.py"))
    if not method_files:
        return {"error": "No page methods found. Please generate page methods first."}
    
    # Collect all imports and function defs
    all_imports = set()
    all_func_defs = []
    test_invocations = []

    for method_file in method_files:
        methods_code = read_page_methods(method_file)
        # Separate import lines and function defs
        for line in methods_code.splitlines():
            line = line.strip()
            if line.startswith("from ") or line.startswith("import "):
                all_imports.add(line)
            elif line.startswith("def "):
                all_func_defs.append(line)
            elif line:  # collect body as well
                all_func_defs.append(line)

        # Detect and queue function invocation
        func_defs = re.findall(r'def\s+([a-zA-Z_][a-zA-Z0-9_]*)\((.*?)\):', methods_code)
        for fn, params in func_defs:
            if "page, value" in params:
                # Fill/select function; provide sample value
                test_invocations.append(f"        {fn}(page, 'demo_value')")
            elif "page" in params:
                # Click/toggle/verify function
                test_invocations.append(f"        {fn}(page)")

    # Ensure all necessary imports exist
    all_imports.add("from playwright.sync_api import sync_playwright")
    for method_file in method_files:
        module_name = method_file.stem  # like login_page_methods
        all_imports.add(f"from pages.{module_name} import *")


    # Build the full test script content
    script_lines = []
    script_lines += sorted(all_imports)
    script_lines.append("")  # spacer
    # Append all function definitions (no duplicates)
    script_lines += all_func_defs
    script_lines.append("")  # spacer

    # Test runner boilerplate
    script_lines.append("def test_generated():")
    script_lines.append("    with sync_playwright() as p:")
    script_lines.append("        browser = p.chromium.launch(headless=False)")
    script_lines.append("        context = browser.new_context()")
    script_lines.append("        page = context.new_page()")
    script_lines += test_invocations
    script_lines.append("        browser.close()")

    script_content = "\n".join(script_lines)

    # Write to tests folder as test_{N}.py
    idx = next_test_index(tests_dir)
    out_file = tests_dir / f"test_{idx}.py"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(script_content)

    return {
        "filename": str(out_file),
        "status": "Test generated",
        "test_code": script_content
    }
