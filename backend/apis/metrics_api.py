from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends

from metrics.collector import collect_run_summary
from metrics.store import MetricsStore

router = APIRouter()


def get_metrics_store() -> MetricsStore:
    return MetricsStore()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    cleaned = ts
    if ts.endswith("Z"):
        cleaned = ts[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _aggregate_period(runs: List[Dict], days: int) -> Dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered = []
    for run in runs:
        timestamp = _parse_iso(run.get("timestamp"))
        if timestamp and timestamp >= cutoff:
            filtered.append(run)
    period = {"passed": 0, "failed": 0, "broken": 0, "skipped": 0, "total": 0, "healing_actions": 0, "healing_saved": 0}
    for run in filtered:
        counts = run.get("status_counts", {})
        period["passed"] += counts.get("passed", 0)
        period["failed"] += counts.get("failed", 0)
        period["broken"] += counts.get("broken", 0)
        period["skipped"] += counts.get("skipped", 0)
        period["total"] += run.get("total", 0)
        period["healing_actions"] += run.get("self_healing_actions", 0)
        period["healing_saved"] += run.get("self_healing_saved", 0)
    return period


def _build_pass_rate_history(runs: List[Dict]) -> List[Dict]:
    history = []
    for run in runs[-20:]:
        history.append(
            {
                "timestamp": run.get("timestamp"),
                "pass_rate": run.get("pass_rate", 0.0),
                "total": run.get("total", 0),
            }
        )
    return history


def _build_healing_history(runs: List[Dict]) -> List[Dict]:
    history = []
    for run in runs[-20:]:
        history.append(
            {
                "timestamp": run.get("timestamp"),
                "healing_actions": run.get("self_healing_actions", 0),
                "healing_saved": run.get("self_healing_saved", 0),
            }
        )
    return history


def _build_flaky_champions(tests: Dict) -> List[Dict]:
    candidates = []
    for entry in tests.values():
        candidates.append(
            {
                "name": entry.get("name"),
                "flaky_score": entry.get("flaky_score", 0),
                "failures": entry.get("failures", 0),
                "runs": len(entry.get("runs", [])),
                "healing_actions": entry.get("healing_actions", 0),
            }
        )
    candidates.sort(key=lambda item: (item["flaky_score"], item["failures"]), reverse=True)
    return candidates[:10]


def _build_feature_insights(features: Dict) -> List[Dict]:
    bucket = []
    for name, entry in features.items():
        total = entry.get("pass", 0) + entry.get("fail", 0) + entry.get("broken", 0)
        fail_rate = (entry.get("fail", 0) + entry.get("broken", 0)) / total if total else 0.0
        pass_rate = entry.get("pass", 0) / total if total else 0.0
        bucket.append(
            {
                "name": name,
                "runs": entry.get("runs", 0),
                "fail_rate": round(fail_rate, 3),
                "pass_rate": round(pass_rate, 3),
                "healing_actions": entry.get("healing_actions", 0),
                "fragile_score": round(fail_rate * (entry.get("runs", 0) or 1), 2),
            }
        )
    bucket.sort(key=lambda item: item["fragile_score"], reverse=True)
    return bucket


def _build_duration_history(runs: List[Dict]) -> List[Dict]:
    history = []
    for run in runs[-20:]:
        durations = [test.get("duration", 0) for test in run.get("tests", []) if test.get("duration") is not None]
        avg = round((sum(durations) / len(durations)), 2) if durations else 0.0
        history.append(
            {
                "id": run.get("id"),
                "timestamp": run.get("timestamp"),
                "average_duration": avg,
                "test_count": len(durations),
            }
        )
    return history


def _build_error_root_causes(errors: Dict[str, int]) -> List[Dict]:
    bucket = [
        {"message": msg, "count": count}
        for msg, count in errors.items()
    ]
    bucket.sort(key=lambda item: item["count"], reverse=True)
    return bucket[:8]


def _build_healing_saved_breakdown(run: Optional[Dict]) -> List[Dict]:
    if not run:
        return []
    saved = run.get("self_healing_saved", 0)
    failed = run.get("healing_failed_tests", 0)
    return [
        {"label": "saved", "value": saved},
        {"label": "failed", "value": failed},
    ]


def _build_healing_strategy_usage(strategies: Dict[str, int]) -> List[Dict]:
    bucket = [
        {"strategy": name, "count": count}
        for name, count in strategies.items()
    ]
    bucket.sort(key=lambda item: item["count"], reverse=True)
    return bucket[:8]


def _build_feature_healing_stats(feature_healing: Dict[str, Dict[str, int]]) -> List[Dict]:
    bucket = []
    for name, entry in feature_healing.items():
        total = entry.get("total_steps", 0)
        healed = entry.get("healed_steps", 0)
        bucket.append(
            {
                "name": name,
                "healed_steps": healed,
                "total_steps": total,
                "runs": entry.get("runs", 0),
                "healing_rate": round((healed / total), 3) if total else 0.0,
            }
        )
    bucket.sort(key=lambda item: item["healed_steps"], reverse=True)
    return bucket


def _build_flaky_density(tests: Dict) -> List[Dict]:
    bucket = []
    for entry in tests.values():
        runs = len(entry.get("runs", []))
        failures = entry.get("failures", 0)
        fail_rate = failures / runs if runs else 0.0
        bucket.append(
            {
                "name": entry.get("name"),
                "fail_rate": round(fail_rate, 3),
                "runs": runs,
                "failures": failures,
            }
        )
    bucket.sort(key=lambda item: item["fail_rate"], reverse=True)
    return bucket[:10]


@router.get("/dashboard")
def dashboard(store: MetricsStore = Depends(get_metrics_store)):
    data = store.read()
    runs = data.get("runs", [])
    latest_run = runs[-1] if runs else None
    periods = {
        "last_run": latest_run,
        "7_days": _aggregate_period(runs, 7),
        "30_days": _aggregate_period(runs, 30),
    }
    pass_rate_history = _build_pass_rate_history(runs)
    healing_history = _build_healing_history(runs)
    flaky_champions = _build_flaky_champions(data.get("tests", {}))
    features = _build_feature_insights(data.get("features", {}))
    risk_heatmap = features
    top_healed_pages = sorted(features, key=lambda item: item["healing_actions"], reverse=True)[:5]
    duration_history = _build_duration_history(runs)
    error_causes = _build_error_root_causes(data.get("errors", {}))
    healing_saved_breakdown = _build_healing_saved_breakdown(latest_run)
    healing_strategy_usage = _build_healing_strategy_usage(data.get("healing_strategies", {}))
    feature_healing_stats = _build_feature_healing_stats(data.get("feature_healing", {}))
    flaky_density = _build_flaky_density(data.get("tests", {}))

    summary = {
        "actions": latest_run.get("self_healing_actions", 0) if latest_run else 0,
        "tests": latest_run.get("self_healing_tests", 0) if latest_run else 0,
        "saved": latest_run.get("self_healing_saved", 0) if latest_run else 0,
        "save_rate": latest_run.get("self_healing_save_rate", 0.0) if latest_run else 0.0,
        "power": round(
            (latest_run.get("pass_rate", 0.0) * latest_run.get("self_healing_save_rate", 0.0)) if latest_run else 0.0,
            3,
        ),
        "pass_rate": latest_run.get("pass_rate", 0.0) if latest_run else 0.0,
        "total": latest_run.get("total", 0) if latest_run else 0,
    }

    return {
        "latest_run": latest_run,
        "periods": periods,
        "pass_rate_history": pass_rate_history,
        "healing_history": healing_history,
        "flaky_champions": flaky_champions,
        "risk_heatmap": risk_heatmap,
        "top_healed_pages": top_healed_pages,
        "self_healing_summary": summary,
        "quality_reports": {
            "pass_fail_trend": pass_rate_history,
            "feature_scores": features,
            "duration_trend": duration_history,
            "flaky_index": flaky_champions,
            "error_root_causes": error_causes,
        },
        "self_healing_reports": {
            "saved_breakdown": healing_saved_breakdown,
            "strategy_usage": healing_strategy_usage,
            "healing_steps_per_feature": feature_healing_stats,
            "history": healing_history,
            "summary": summary,
        },
        "flakiness_insights": {
            "flaky_density": flaky_density,
            "page_quality": features,
            "locator_change_frequency": [
                {
                    "name": stat["name"],
                    "healing_rate": stat["healing_rate"],
                    "runs": stat["runs"],
                }
                for stat in feature_healing_stats
            ],
        },
    }
