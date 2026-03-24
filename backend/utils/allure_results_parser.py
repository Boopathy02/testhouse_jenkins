from __future__ import annotations

import json
from pathlib import Path

ALLURE_RESULT_SUFFIX = "-result.json"


def parse_allure_results(results_dir: Path, report_base_url: str = "/reports/view") -> dict:
    results_dir = Path(results_dir)
    if not results_dir.exists():
        raise FileNotFoundError(f"Allure results directory not found: {results_dir}")

    result_files = sorted(results_dir.glob(f"*{ALLURE_RESULT_SUFFIX}"))
    status_counts = {"passed": 0, "failed": 0, "skipped": 0, "broken": 0, "unknown": 0}
    all_tests = []

    for result_file in result_files:
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        status = (payload.get("status") or "unknown").lower()
        if status not in status_counts:
            status_counts[status] = 0
        status_counts[status] += 1

        uuid = payload.get("uuid") or ""
        name = payload.get("name") or payload.get("fullName") or result_file.stem
        details = payload.get("statusDetails") or {}
        message = details.get("message") or ""
        trace = details.get("trace") or ""

        all_tests.append(
            {
                "name": name,
                "uuid": uuid,
                "status": status,
                "message": message,
                "trace": trace,
                "allure_testcase_url": f"{report_base_url}/#/testcase/{uuid}" if uuid else None,
            }
        )

    failed_count = status_counts.get("failed", 0) + status_counts.get("broken", 0)
    passed_count = status_counts.get("passed", 0)
    skipped_count = status_counts.get("skipped", 0)
    total_count = len(result_files)

    failed_tests = [test for test in all_tests if test["status"] in {"failed", "broken"}]

    return {
        "total": total_count,
        "passed": passed_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "status_breakdown": status_counts,
        "failed_tests": failed_tests,
        "charts": {
            "pie": {
                "labels": ["passed", "failed", "skipped"],
                "series": [passed_count, failed_count, skipped_count],
            },
            "bar": {
                "labels": ["passed", "failed", "skipped"],
                "series": [passed_count, failed_count, skipped_count],
            },
        },
    }
