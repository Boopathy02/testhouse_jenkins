"""
Release gating decisions based on quality score and risk assessment.
"""

from . import config
from .schemas import QualityScore, ReleaseDecision


class ReleaseGate:
    """Determines whether a release is allowed based on quality signals."""

    def evaluate(self, score: QualityScore) -> ReleaseDecision:
        """
        Evaluate a QualityScore and return a release decision.

        - BLOCK if score below configured minimum.
        - BLOCK if risk level is HIGH.
        - Otherwise GO.
        """
        if score.score < config.MIN_QUALITY_SCORE:
            return ReleaseDecision(
                decision="BLOCK",
                reason=(
                    f"Quality score {score.score} below minimum "
                    f"{config.MIN_QUALITY_SCORE}"
                ),
            )

        if score.risk_level.upper() == "HIGH":
            return ReleaseDecision(
                decision="BLOCK",
                reason="Risk level HIGH",
            )

        return ReleaseDecision(
            decision="GO",
            reason="Quality thresholds met",
        )
