import argparse
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


def _default_output_path() -> Path:
    project_root = os.getenv("SMARTAI_PROJECT_DIR", "").strip()
    if project_root:
        return Path(project_root) / "storage" / "cookies.json"
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "backend" / "storage" / "cookies.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record Playwright storage_state for login reuse.")
    parser.add_argument("--url", required=True, help="Login URL to open for manual sign-in.")
    parser.add_argument(
        "--out",
        default="",
        help="Output path for storage_state JSON (defaults to project storage or backend/storage).",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=300,
        help="Max seconds to wait before saving storage state.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    output_path = Path(args.out).expanduser() if args.out else _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[storage_state] Opening login URL: {args.url}")
    print(f"[storage_state] Saving to: {output_path}")
    print("[storage_state] Complete login in the opened browser window.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=args.timeout_sec * 1000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(args.timeout_sec * 1000)
        except Exception:
            pass
        context.storage_state(path=str(output_path))
        browser.close()

    print("[storage_state] Saved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
