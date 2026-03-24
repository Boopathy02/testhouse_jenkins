import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

MAX_RUNS_TO_KEEP = 64
MAX_TEST_HISTORY = 30


def _default_history() -> Dict[str, Any]:
    return {
        "runs": [],
        "tests": {},
        "features": {},
        "errors": {},
        "healing_strategies": {},
        "feature_healing": {},
    }


def get_project_history_path() -> Path:
    """
    Return the path to history.json inside the active project's generated_runs
    directory, with a fallback to the old location if no project is active.
    """
    project_dir = os.environ.get("SMARTAI_PROJECT_DIR")
    if project_dir:
        return Path(project_dir) / "generated_runs" / "src" / "history.json"

    # Fallback for when no project is active
    return Path(__file__).resolve().parent / "history.json"


class MetricsStore:
    def __init__(self, history_path: Optional[Path] = None):
        self.path = history_path or get_project_history_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(_default_history())

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = _default_history()
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def read(self) -> Dict[str, Any]:
        return self._read()

    def save(self, data: Dict[str, Any]) -> None:
        self._write(data)

    def record_run(self, run_summary: Dict[str, Any]) -> None:
        data = _default_history()
        runs = data.setdefault("runs", [])
        runs.append(run_summary)
        self._update_tests(data, run_summary)
        self._update_features(data, run_summary)
        self._update_errors(data, run_summary)
        self._update_healing_strategies(data, run_summary)
        self._update_feature_healing(data, run_summary)
        data["last_updated"] = run_summary.get("timestamp")
        self._write(data)

    def _update_tests(self, data: Dict[str, Any], run_summary: Dict[str, Any]) -> None:
        tests_bucket = data.setdefault("tests", {})
        for test in run_summary.get("tests", []):
            name = test.get("name") or "unknown"
            entry = tests_bucket.setdefault(
                name,
                {
                    "name": name,
                    "runs": [],
                    "last_status": None,
                    "failures": 0,
                    "passes": 0,
                    "flaky_score": 0,
                    "healing_actions": 0,
                },
            )
            status = test.get("status", "unknown")
            entry["runs"].append({"timestamp": run_summary["timestamp"], "status": status})
            if len(entry["runs"]) > MAX_TEST_HISTORY:
                entry["runs"] = entry["runs"][-MAX_TEST_HISTORY:]
            if status != entry["last_status"] and entry["last_status"] is not None:
                entry["flaky_score"] += 1
            entry["last_status"] = status
            if status == "passed":
                entry["passes"] += 1
            elif status in {"failed", "broken"}:
                entry["failures"] += 1
            entry["healing_actions"] += test.get("self_healing_actions", 0)

    def _update_features(self, data: Dict[str, Any], run_summary: Dict[str, Any]) -> None:
        features_bucket = data.setdefault("features", {})
        for feature in run_summary.get("feature_summary", []):
            name = feature.get("name") or "General"
            entry = features_bucket.setdefault(
                name,
                {
                    "name": name,
                    "runs": 0,
                    "pass": 0,
                    "fail": 0,
                    "broken": 0,
                    "healing_actions": 0,
                },
            )
            entry["runs"] += feature.get("runs", 0)
            entry["pass"] += feature.get("pass", 0)
            entry["fail"] += feature.get("fail", 0)
            entry["broken"] += feature.get("broken", 0)
            entry["healing_actions"] += feature.get("healing_actions", 0)

    def _update_errors(self, data: Dict[str, Any], run_summary: Dict[str, Any]) -> None:
        errors_bucket = data.setdefault("errors", {})
        for message, count in run_summary.get("error_causes", {}).items():
            errors_bucket[message] = errors_bucket.get(message, 0) + count

    def _update_healing_strategies(self, data: Dict[str, Any], run_summary: Dict[str, Any]) -> None:
        strategies_bucket = data.setdefault("healing_strategies", {})
        for strategy, count in run_summary.get("healing_strategies", {}).items():
            strategies_bucket[strategy] = strategies_bucket.get(strategy, 0) + count

    def _update_feature_healing(self, data: Dict[str, Any], run_summary: Dict[str, Any]) -> None:
        feature_bucket = data.setdefault("feature_healing", {})
        for entry in run_summary.get("feature_healing_progress", []):
            name = entry.get("name")
            if not name:
                continue
            target = feature_bucket.setdefault(
                name,
                {"name": name, "healed_steps": 0, "total_steps": 0, "runs": 0},
            )
            target["healed_steps"] += entry.get("healed_steps", 0)
            target["total_steps"] += entry.get("total_steps", 0)
            target["runs"] += 1
