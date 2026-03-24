"""
Pydantic v1 data contracts for the Quality Intelligence Engine.
These models describe inputs/outputs shared across change detection,
impact analysis, test planning, scoring, and release gating flows.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel


class ChangeSet(BaseModel):
    """Represents a detected change and its affected scope within the product."""

    change_id: str
    change_types: List[str]
    pages_affected: List[str]
    elements_affected: List[str]
    severity: str


class ImpactAnalysisResult(BaseModel):
    """Summarizes the impact of a change on tests and application pages."""

    impacted_tests: List[str]
    impacted_pages: List[str]
    reason: str


class TestPlan(BaseModel):
    """Defines which tests to run, skip, or generate for the current evaluation."""

    tests_to_run: List[str]
    tests_to_skip: List[str]
    tests_to_generate: List[str]
    mode: str


class QualityScore(BaseModel):
    """Quality assessment metrics for a build or change set."""

    score: float
    risk_level: str
    coverage_percent: float
    healing_confidence: float


class ReleaseDecision(BaseModel):
    """Release gating decision based on quality signals."""

    decision: Literal["GO", "BLOCK"]
    reason: Optional[str]
