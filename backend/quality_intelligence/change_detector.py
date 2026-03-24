"""
Lightweight change detection between two metadata snapshots.
Uses deterministic rules to classify UI changes without external systems.
"""

from typing import Dict, List

from .schemas import ChangeSet


class ChangeDetector:
    """Detects UI changes between previous and current metadata snapshots."""

    def detect(self, previous_snapshot: Dict, current_snapshot: Dict) -> ChangeSet:
        """
        Compare two snapshots and return a structured ChangeSet.

        The comparison focuses on page name, label text, and DOM selector.
        Severity is derived from the type of change encountered.
        """

        previous = previous_snapshot or {}
        current = current_snapshot or {}

        page_previous = previous.get("page_name")
        page_current = current.get("page_name")
        label_previous = previous.get("label_text")
        label_current = current.get("label_text")
        selector_previous = previous.get("dom_selector")
        selector_current = current.get("dom_selector")

        change_types: List[str] = []
        pages_affected: List[str] = []
        elements_affected: List[str] = []

        removed = bool(selector_previous) and not selector_current
        added = bool(selector_current) and not selector_previous
        label_changed = (
            label_previous is not None
            and label_current is not None
            and label_previous != label_current
        )
        selector_changed = (
            selector_previous is not None
            and selector_current is not None
            and selector_previous != selector_current
        )
        page_changed = (
            page_previous is not None
            and page_current is not None
            and page_previous != page_current
        )

        any_change = removed or added or label_changed or selector_changed or page_changed
        if any_change:
            change_types = ["UI"]

        # Severity follows explicit rules; selector/page changes default to LOW when present.
        if removed:
            severity = "HIGH"
        elif label_changed:
            severity = "MEDIUM"
        elif added:
            severity = "LOW"
        else:
            severity = "LOW" if any_change else "NONE"

        if page_previous:
            pages_affected.append(str(page_previous))
        if page_current and page_current not in pages_affected:
            pages_affected.append(str(page_current))

        if selector_previous:
            elements_affected.append(str(selector_previous))
        if selector_current and selector_current not in elements_affected:
            elements_affected.append(str(selector_current))

        change_id_basis = selector_current or selector_previous or page_current or page_previous or "unknown"
        change_id = f"change-{change_id_basis}"

        return ChangeSet(
            change_id=change_id,
            change_types=change_types,
            pages_affected=pages_affected,
            elements_affected=elements_affected,
            severity=severity,
        )
