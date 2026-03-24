"""
Compute an overall quality confidence score using deterministic weighting.
"""

from typing import Dict

from . import config
from .schemas import QualityScore


class QualityScorer:
    """Calculates quality confidence and risk level from execution metrics."""

    def calculate(self, execution_results: Dict) -> QualityScore:
        """
        Compute a weighted score and derive a risk level.

        Expected keys in execution_results:
        - pass_rate (0–1)
        - coverage_percent (0–100)
        - healing_success_rate (0–1)
        - flaky_rate (0–1)
        """
        pass_rate = float(execution_results.get("pass_rate", 0.0))
        coverage_percent = float(execution_results.get("coverage_percent", 0.0))
        healing_success_rate = float(execution_results.get("healing_success_rate", 0.0))
        flaky_rate = float(execution_results.get("flaky_rate", 0.0))

        # Normalize coverage to 0–1 for scoring.
        coverage_norm = max(0.0, min(coverage_percent / 100.0, 1.0))
        flakiness_penalty = max(0.0, min(flaky_rate, 1.0))

        weights = config.QUALITY_SCORE_WEIGHTS

        score_raw = (
            (coverage_norm * weights.get("coverage", 0.0))
            + (healing_success_rate * weights.get("healing", 0.0))
            + (pass_rate * weights.get("pass_rate", 0.0))
            + ((1.0 - flakiness_penalty) * weights.get("flakiness_penalty", 0.0))
        )

        score = max(0.0, min(score_raw, 1.0))

        if score >= 0.9:
            risk_level = "LOW"
        elif score >= 0.75:
            risk_level = "MEDIUM"
        else:
            risk_level = "HIGH"

        return QualityScore(
            score=round(score, 4),
            risk_level=risk_level,
            coverage_percent=coverage_percent,
            healing_confidence=healing_success_rate,
        )
