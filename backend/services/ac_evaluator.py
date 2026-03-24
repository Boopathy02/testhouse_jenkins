import re
from typing import Any, Dict, List, Optional


def evaluate_acceptance_criteria(
    page,
    acceptance_criteria: List[str],
    executed_tests: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not acceptance_criteria:
        return {"overall_status": "NO_AC", "details": []}

    def _tokenize(text: str) -> List[str]:
        tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
        stopwords = {
            "the",
            "a",
            "an",
            "to",
            "and",
            "or",
            "of",
            "in",
            "on",
            "for",
            "with",
            "must",
            "be",
            "able",
            "open",
            "access",
            "navigate",
            "section",
            "page",
            "screen",
            "tab",
            "test",
            "case",
            "story",
        }
        return [token for token in tokens if len(token) >= 4 and token not in stopwords]

    def _extract_issue_keys(text: str) -> List[str]:
        return re.findall(r"\b[A-Z][A-Z0-9]+-\d+\b", text or "")

    def _is_ac_covered(ac_text: str, test_names: List[str]) -> bool:
        if not test_names:
            return True
        ac_tokens = _tokenize(ac_text)
        ac_keys = [key.lower() for key in _extract_issue_keys(ac_text)]
        for test_name in test_names:
            name = str(test_name or "")
            name_lower = name.lower()
            if ac_keys and any(key in name_lower for key in ac_keys):
                return True
            test_tokens = _tokenize(name)
            if ac_tokens and any(token in test_tokens for token in ac_tokens):
                return True
        return False

    details: List[Dict[str, Any]] = []
    test_scope = [name for name in (executed_tests or []) if str(name).strip()]
    for ac in acceptance_criteria:
        text = str(ac or "").strip()
        if not text:
            continue
        if not _is_ac_covered(text, test_scope):
            details.append(
                {
                    "ac": text,
                    "status": "NOT_EVALUATED",
                    "reason": "Not covered by executed test cases.",
                }
            )
            continue

        lowered = text.lower()
        if any(keyword in lowered for keyword in ("access", "navigate", "open section")):
            try:
                if page.is_closed():
                    details.append({"ac": text, "status": "FAILED", "reason": "Page was closed during navigation."})
                    continue

                url = (page.url or "").strip()
                if not url:
                    details.append({"ac": text, "status": "FAILED", "reason": "Navigation failed: page URL is empty."})
                    continue

                content = page.content()
                if not content or not content.strip():
                    details.append(
                        {"ac": text, "status": "FAILED", "reason": "Browser did not load any page content."}
                    )
                    continue

                details.append({"ac": text, "status": "PASSED", "reason": ""})
            except Exception as exc:
                details.append(
                    {"ac": text, "status": "FAILED", "reason": f"Navigation failed due to page load error: {exc}"}
                )
            continue

        if any(keyword in lowered for keyword in ("confirmation", "success", "submitted", "created")):
            success_re = re.compile(r"success|submitted|created|completed|saved|done", re.I)
            try:
                locator = page.get_by_text(success_re)
                if locator.count() > 0 and locator.first.is_visible():
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception as exc:
                details.append({"ac": text, "status": "FAILED", "reason": f"Success text check failed: {exc}"})
                continue

            try:
                url = (page.url or "").lower()
                if any(keyword in url for keyword in ("list", "summary", "success", "confirmation", "done")):
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception:
                pass

            try:
                content = (page.content() or "").lower()
                if success_re.search(content):
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception:
                pass

            try:
                rows = page.locator("table tbody tr")
                if rows.count() > 0 and rows.first.is_visible():
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception:
                pass

            details.append({"ac": text, "status": "FAILED", "reason": "No deterministic success signal found."})
            continue

        if any(keyword in lowered for keyword in ("open", "form", "application")):
            try:
                submit_button = page.get_by_role(
                    "button", name=re.compile(r"submit|save|create|add|apply|send", re.I)
                )
                if submit_button.count() > 0 and submit_button.first.is_visible():
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception as exc:
                details.append({"ac": text, "status": "FAILED", "reason": f"Form check failed: {exc}"})
                continue

            try:
                form = page.locator("form")
                if form.count() > 0 and form.first.is_visible():
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception:
                pass

            try:
                fields = page.locator("input, textarea, select")
                if fields.count() > 0 and fields.first.is_visible():
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception:
                pass

            try:
                heading = page.get_by_role("heading", name=re.compile(r"form|application|create|add|new", re.I))
                if heading.count() > 0 and heading.first.is_visible():
                    details.append({"ac": text, "status": "PASSED", "reason": ""})
                    continue
            except Exception:
                pass

            details.append({"ac": text, "status": "FAILED", "reason": "No deterministic form-open signal found."})
            continue

        if any(keyword in lowered for keyword in ("validate", "mandatory", "required")):
            details.append(
                {
                    "ac": text,
                    "status": "NOT_EVALUATED",
                    "reason": "Requires negative test execution (missing/invalid field submission).",
                }
            )
            continue

        details.append(
            {
                "ac": text,
                "status": "NOT_EVALUATED",
                "reason": "No deterministic evaluator rule matched.",
            }
        )

    if any(item["status"] == "FAILED" for item in details):
        overall_status = "REJECTED"
    elif any(item["status"] == "PASSED" for item in details):
        overall_status = "ACCEPTED"
    else:
        overall_status = "PARTIAL"

    return {"overall_status": overall_status, "details": details}
