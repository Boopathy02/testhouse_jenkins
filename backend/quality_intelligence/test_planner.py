"""
Plan which tests to run, skip, or generate based on impact analysis and mode.
Uses deterministic heuristics; replace filters with richer metadata when available.
"""

from typing import List

from . import config
from .schemas import ImpactAnalysisResult, TestPlan


class TestPlanner:
    """Decides test execution plans based on impact analysis and selection mode."""

    def plan(self, impact: ImpactAnalysisResult, mode: str) -> TestPlan:
        """
        Build a TestPlan using deterministic rules.

        Modes:
        - EXPRESS: prioritize unit/critical tests only.
        - BALANCED: skip E2E where possible.
        - SAFETY: run all impacted tests.
        """
        selection_mode = (mode or config.DEFAULT_SELECTION_MODE).upper()

        tests_to_run = list(impact.impacted_tests)
        tests_to_skip: List[str] = []

        if selection_mode == "EXPRESS":
            tests_to_run, tests_to_skip = self._filter_express(tests_to_run)
        elif selection_mode == "BALANCED":
            tests_to_run, tests_to_skip = self._filter_balanced(tests_to_run)
        else:  # SAFETY or any unrecognized mode defaults to safest behavior
            selection_mode = "SAFETY"

        # Deduplicate run/skip sets and ensure separation.
        tests_to_run = list(dict.fromkeys(tests_to_run))
        tests_to_skip = [t for t in dict.fromkeys(tests_to_skip) if t not in tests_to_run]

        tests_to_generate = self._plan_generation(impact, tests_to_run)

        if not impact.impacted_tests:
            placeholders = [
                f"generate_tests_for_{str(page).lower().replace(' ', '_')}"
                for page in impact.impacted_pages
            ] or ["generate_regression_smoke_pack"]
            tests_to_generate.extend(placeholders)

        # Ensure generation list is unique and does not overlap run/skip.
        tests_to_generate = [
            t for t in dict.fromkeys(tests_to_generate) if t not in tests_to_run and t not in tests_to_skip
        ]

        return TestPlan(
            tests_to_run=tests_to_run,
            tests_to_skip=tests_to_skip,
            tests_to_generate=tests_to_generate,
            mode=selection_mode.lower(),
        )

    def _filter_express(self, tests: List[str]) -> (List[str]):
        """
        Keep only unit or critical-labeled tests; skip the rest.
        Heuristic based on name tokens to keep logic deterministic and simple.
        """
        keep_keywords = ("unit", "critical", "smoke")
        tests_to_run: List[str] = []
        tests_to_skip: List[str] = []
        for test in tests:
            if any(key in test.lower() for key in keep_keywords):
                tests_to_run.append(test)
            else:
                tests_to_skip.append(test)
        return tests_to_run, tests_to_skip

    def _filter_balanced(self, tests: List[str]) -> (List[str]):
        """
        Skip E2E-style tests when possible; run the remainder.
        Heuristic checks common E2E indicators in test names.
        """
        skip_keywords = ("e2e", "end_to_end", "ui_flow")
        tests_to_run: List[str] = []
        tests_to_skip: List[str] = []
        for test in tests:
            if any(key in test.lower() for key in skip_keywords):
                tests_to_skip.append(test)
            else:
                tests_to_run.append(test)
        return tests_to_run, tests_to_skip

    def _plan_generation(
        self, impact: ImpactAnalysisResult, current_tests: List[str]
    ) -> List[str]:
        """
        Decide which tests need generation when no coverage exists for impacted pages.

        If no tests reference an impacted page, mark that page for test generation.
        """
        current_lower = [t.lower() for t in current_tests]
        tests_to_generate: List[str] = []

        for page in impact.impacted_pages:
            page_token = str(page).lower().replace(" ", "_")
            has_coverage = any(page_token in test for test in current_lower)
            if not has_coverage:
                tests_to_generate.append(f"generate_tests_for_{page_token}")

        return tests_to_generate
