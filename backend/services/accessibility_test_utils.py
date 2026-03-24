# backend/services/accessibility_test_utils.py
import json
from pathlib import Path

from playwright.sync_api import Page
from axe_playwright_python.sync_playwright import Axe


def _unwrap_page(page: Page):
    """
    Returns the underlying Playwright page implementation if the page was patched.
    """
    return getattr(page, "_locator", page)


def run_accessibility_scan(
    page: Page,
    file_path: Path | str | None = None,
):
    """
    Runs an Axe accessibility scan on the current page and saves the results to a file.
    """
    axe = Axe()
    actual_page = _unwrap_page(page)

    target_path = Path(file_path) if file_path else Path.cwd() / "accessibility_results.json"
    target_path.parent.mkdir(parents=True, exist_ok=True)

    results = axe.run(actual_page)

    # Convert the AxeResults object to a dictionary for JSON serialization
    results_dict = {
        "violations": results.violations,
        "passes": results.passes,
        "incomplete": results.incomplete,
        "inapplicable": results.inapplicable,
        "timestamp": results.timestamp,
        "url": results.url,
    }

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(results_dict, f, indent=2)

    if results.violations:
        print(
            f"Accessibility violations found on page {actual_page.url} "
            f"and saved to {target_path}"
        )
    else:
        print(f"No accessibility violations found on page {actual_page.url}")

    return results
