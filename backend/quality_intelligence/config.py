"""
Configuration for the Quality Intelligence Engine.
Centralizes tunable policies so non-developers can adjust thresholds safely.
"""

from dataclasses import dataclass, field
from typing import Dict

# ---------------------------------------------------------------------
# Core thresholds and toggles (safe, immutable primitives)
# ---------------------------------------------------------------------

DEFAULT_SELECTION_MODE: str = "balanced"
MIN_QUALITY_SCORE: float = 0.85
MAX_FLAKY_RATE: float = 0.1
CRITICAL_TESTS_MUST_PASS: bool = True

# ---------------------------------------------------------------------
# Default scoring weights (used via factory ONLY)
# ---------------------------------------------------------------------

DEFAULT_QUALITY_SCORE_WEIGHTS: Dict[str, float] = {
    "coverage": 0.4,
    "healing": 0.3,
    "pass_rate": 0.2,
    "flakiness_penalty": 0.1,
}

# ---------------------------------------------------------------------
# Scorer-specific configuration
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class QualityScorerConfig:
    quality_score_weights: Dict[str, float] = field(
        default_factory=lambda: DEFAULT_QUALITY_SCORE_WEIGHTS.copy()
    )

# ---------------------------------------------------------------------
# Policy configuration (what gates releases)
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class QualityPolicy:
    """Container for quality-related thresholds and selection defaults."""

    selection_mode: str = DEFAULT_SELECTION_MODE
    min_quality_score: float = MIN_QUALITY_SCORE
    max_flaky_rate: float = MAX_FLAKY_RATE
    critical_tests_must_pass: bool = CRITICAL_TESTS_MUST_PASS
    quality_score_weights: Dict[str, float] = field(
        default_factory=lambda: DEFAULT_QUALITY_SCORE_WEIGHTS.copy()
    )

# ---------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------

__all__ = [
    "DEFAULT_SELECTION_MODE",
    "MIN_QUALITY_SCORE",
    "MAX_FLAKY_RATE",
    "CRITICAL_TESTS_MUST_PASS",
    "DEFAULT_QUALITY_SCORE_WEIGHTS",
    "QualityScorerConfig",
    "QualityPolicy",
]
