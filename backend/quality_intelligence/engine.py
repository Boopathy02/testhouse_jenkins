"""
Orchestrates the Quality Intelligence workflow for a build.
Performs change detection, impact analysis, test planning, scoring, and release gating.
"""

from typing import Dict, List

from .change_detector import ChangeDetector
from .impact_analyzer import ImpactAnalyzer
from .quality_scorer import QualityScorer
from .release_gate import ReleaseGate
from .schemas import ChangeSet, ImpactAnalysisResult, QualityScore, ReleaseDecision, TestPlan
from .test_planner import TestPlanner
from .git_change_detector import detect_changes


class QualityIntelligenceEngine:
    """High-level orchestrator for quality intelligence decisions."""

    def __init__(self) -> None:
        self.change_detector = ChangeDetector()
        self.impact_analyzer = ImpactAnalyzer()
        self.test_planner = TestPlanner()
        self.quality_scorer = QualityScorer()
        self.release_gate = ReleaseGate()

    def run(
        self,
        project_id: int,
        build_id: str,
        mode: str,
        snapshots: Dict[str, Dict],
        execution_results: Dict,
    ) -> Dict:
        """
        Execute the Quality Intelligence workflow and return a structured result.

        This method is orchestration-only; execution and generation are placeholders.
        """
        previous_snapshot = (snapshots or {}).get("previous", {})
        current_snapshot = (snapshots or {}).get("current", {})

        change_set: ChangeSet = self.change_detector.detect(
            previous_snapshot=previous_snapshot,
            current_snapshot=current_snapshot,
        )

        impact: ImpactAnalysisResult = self.impact_analyzer.analyze(change_set)
        plan: TestPlan = self.test_planner.plan(impact=impact, mode=mode)

        generated_tests: List[str] = plan.tests_to_generate
        if generated_tests:
            # TODO: trigger generation workflow for generated_tests
            pass

        # TODO: execute tests in plan.tests_to_run and collect execution_results
        # execution_results passed in are assumed to reflect latest run externally.

        quality_score: QualityScore = self.quality_scorer.calculate(execution_results)
        release_decision: ReleaseDecision = self.release_gate.evaluate(quality_score)

        return {
            "project_id": project_id,
            "build_id": build_id,
            "mode": plan.mode,
            "impacted_tests": plan.tests_to_run,
            "generated_tests": generated_tests,
            "quality_score": quality_score.dict(),
            "release_decision": release_decision.dict(),
            "change_set": change_set.dict(),
            "impact": impact.dict(),
        }


def get_test_plan(repo_path: str, base_commit: str, head_commit: str, mode: str) -> TestPlan:
    """Orchestrate git change detection, impact analysis, and planning."""
    changes = detect_changes(repo_path, base_commit, head_commit)
    if not changes:
        return TestPlan(tests_to_run=[], tests_to_skip=[], tests_to_generate=[], mode=mode)
    impact = ImpactAnalyzer().analyze(changes)
    return TestPlanner().plan(impact=impact, mode=mode)
