import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

HEALING_KEYWORDS = {"heal", "healing", "self-heal", "healed"}
FEATURE_LABELS = {"feature", "story", "suite", "parentSuite", "epic", "package"}
STRATEGY_HINTS = {"strategy", "method", "approach", "heuristic"}


def _normalize_status(status: Any) -> str:
    if not status:
        return "unknown"
    value = str(status).lower()
    return value if value in {"passed", "failed", "broken", "skipped", "unknown"} else value


def _extract_strategy_from_text(text: str) -> Optional[str]:
    lower = text.lower()
    for hint in STRATEGY_HINTS:
        if hint in lower:
            try:
                candidate = lower.split(hint, 1)[1]
                if ":" in candidate:
                    strategy = candidate.split(":", 1)[1].strip()
                else:
                    strategy = candidate.strip()
                strategy = strategy.split()[0] if strategy else ""
                return strategy.title() if strategy else hint.title()
            except Exception:
                continue
    return None


def _collect_healing_info(entry: Dict[str, Any]) -> Dict[str, Any]:
    info = {
        "count": 0,
        "strategies": Counter(),
        "healed_steps": 0,
        "total_steps": 0,
    }

    def _inspect_steps(steps: Iterable[Dict[str, Any]]):
        for step in steps or []:
            info["total_steps"] += 1
            name = str(step.get("name") or "").lower()
            if any(keyword in name for keyword in HEALING_KEYWORDS):
                info["count"] += 1
                info["healed_steps"] += 1
                strategy = _extract_strategy_from_text(step.get("name") or "")
                if strategy:
                    info["strategies"][strategy] += 1
            _inspect_steps(step.get("steps", []))

    _inspect_steps(entry.get("steps", []))
    for attachment in entry.get("attachments", []):
        info["total_steps"] += 1
        attach_name = str(attachment.get("name") or "").lower()
        if any(keyword in attach_name for keyword in HEALING_KEYWORDS):
            info["count"] += 1
            strategy = _extract_strategy_from_text(attachment.get("name") or "")
            if strategy:
                info["strategies"][strategy] += 1
    return info


def _extract_feature(entry: Dict[str, Any]) -> str:
    for label in entry.get("labels", []) or []:
        key = str(label.get("name") or "").lower()
        if key in FEATURE_LABELS:
            return label.get("value") or label.get("name") or "General"

    name = entry.get("name") or entry.get("fullName") or "General"
    if isinstance(name, str):
        if " - " in name:
            return name.split(" - ", 1)[0].strip()
        if "::" in name:
            return name.split("::", 1)[0].strip()
        if "/" in name:
            return name.split("/", 1)[0].strip()
    return "General"


def _extract_error_message(entry: Dict[str, Any]) -> Optional[str]:
    details = entry.get("statusDetails") or {}
    message = details.get("message") or details.get("trace") or ""
    if not message:
        return None
    first_line = (message.splitlines()[0] or "").strip()
    return first_line if first_line else None


def _parse_duration(entry: Dict[str, Any]) -> float:
    start = entry.get("start")
    stop = entry.get("stop")
    duration_ms = entry.get("duration")
    if start is not None and stop is not None and stop >= start:
        return (stop - start) / 1000.0
    if isinstance(duration_ms, (int, float)) and duration_ms > 1:
        return duration_ms / 1000.0
    return 0.0


def _timestamp_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def collect_run_summary(allure_results: Path) -> Dict[str, Any] | None:
    files = sorted(allure_results.glob("*-result.json"))
    if not files:
        return None

    status_counts = Counter()
    tests: List[Dict[str, Any]] = []
    feature_accum: Dict[str, Dict[str, Any]] = {}
    feature_duration_map: Dict[str, List[float]] = defaultdict(list)
    feature_healing_steps: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"healed_steps": 0, "total_steps": 0, "tests": 0}
    )
    total_healing_actions = 0
    healing_saved = 0
    healing_tests = 0
    healing_failed_tests = 0
    earliest_start = None
    latest_stop = None
    strategy_counter = Counter()
    error_causes = Counter()

    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception:
            continue
        name = payload.get("name") or payload.get("fullName") or path.stem
        status = _normalize_status(payload.get("status"))
        duration = _parse_duration(payload)
        start = payload.get("start")
        stop = payload.get("stop")
        if start is not None and (earliest_start is None or start < earliest_start):
            earliest_start = start
        if stop is not None and (latest_stop is None or stop > latest_stop):
            latest_stop = stop

        healing_info = _collect_healing_info(payload)
        healing_actions = healing_info["count"]
        if healing_actions > 0:
            healing_tests += 1
        if healing_actions and status == "passed":
            healing_saved += 1
        if healing_actions and status != "passed":
            healing_failed_tests += 1
        for strat, strat_count in healing_info["strategies"].items():
            strategy_counter[strat] += strat_count
        total_healing_actions += healing_actions

        feature_name = _extract_feature(payload)
        feature_duration_map[feature_name].append(duration)
        feature_healing_steps[feature_name]["healed_steps"] += healing_info["healed_steps"]
        feature_healing_steps[feature_name]["total_steps"] += healing_info["total_steps"]
        feature_healing_steps[feature_name]["tests"] += 1
        feature_entry = feature_accum.setdefault(
            feature_name,
            {"name": feature_name, "runs": 0, "pass": 0, "fail": 0, "broken": 0, "healing_actions": 0},
        )
        feature_entry["runs"] += 1
        feature_entry["healing_actions"] += healing_actions
        if status == "passed":
            feature_entry["pass"] += 1
        elif status == "broken":
            feature_entry["broken"] += 1
        elif status == "failed":
            feature_entry["fail"] += 1

        status_counts[status] += 1
        error_message = _extract_error_message(payload)
        if error_message:
            error_causes[error_message] += 1
        tests.append(
            {
                "name": name,
                "status": status,
                "duration": round(duration, 2),
                "self_healing_actions": healing_actions,
                "feature": feature_name,
                "error_message": error_message,
            }
        )

    total_tests = sum(status_counts.values())
    pass_count = status_counts.get("passed", 0)
    run_duration = 0.0
    if earliest_start is not None and latest_stop is not None and latest_stop > earliest_start:
        run_duration = (latest_stop - earliest_start) / 1000.0
    elif tests:
        run_duration = sum(test["duration"] for test in tests)

    pass_rate = pass_count / total_tests if total_tests else 0.0
    healing_save_rate = healing_saved / healing_tests if healing_tests else 0.0

    run_summary = {
        "id": uuid.uuid4().hex,
        "timestamp": _timestamp_now(),
        "total": total_tests,
        "duration": round(run_duration, 2),
        "status_counts": dict(status_counts),
        "tests": tests,
        "self_healing_actions": total_healing_actions,
        "self_healing_saved": healing_saved,
        "self_healing_tests": healing_tests,
        "pass_rate": round(pass_rate, 3),
        "self_healing_save_rate": round(healing_save_rate, 3),
        "feature_summary": [
            {
                "name": feature["name"],
                "runs": feature["runs"],
                "pass": feature["pass"],
                "fail": feature["fail"],
                "broken": feature["broken"],
                "healing_actions": feature["healing_actions"],
            }
            for feature in feature_accum.values()
        ],
        "feature_duration": {
            feature: round((sum(durations) / len(durations)), 2) if durations else 0.0
            for feature, durations in feature_duration_map.items()
        },
        "feature_healing_progress": [
            {
                "name": name,
                "healed_steps": info["healed_steps"],
                "total_steps": info["total_steps"],
                "tests": info["tests"],
            }
            for name, info in feature_healing_steps.items()
        ],
        "healing_strategies": dict(strategy_counter),
        "healing_failed_tests": healing_failed_tests,
        "error_causes": dict(error_causes),
    }
    return run_summary
