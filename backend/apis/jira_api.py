from __future__ import annotations

import base64
import json
import re
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter()


class JiraImportRequest(BaseModel):
    base_url: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    api_token: str = Field(..., min_length=1)
    project_key: str = Field(..., min_length=1)
    issue_key: Optional[str] = None
    issue_keys: Optional[List[str]] = None
    jql: Optional[str] = None
    max_results: int = Field(50, ge=1, le=200)

    @field_validator("issue_key")
    @classmethod
    def _validate_issue_key(cls, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip().upper()
        if not re.match(r"^[A-Z][A-Z0-9]+-\d+$", cleaned):
            raise ValueError("issue_key must match pattern ABC-123")
        return cleaned


def _normalize_base_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


def _extract_adf_text(node: Any) -> List[str]:
    if isinstance(node, dict):
        text = []
        if node.get("type") == "hardBreak":
            # Preserve explicit Jira line breaks inside a paragraph.
            return ["\n"]
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            text.append(node["text"])
        for child in node.get("content", []) or []:
            text.extend(_extract_adf_text(child))
        return text
    if isinstance(node, list):
        text = []
        for child in node:
            text.extend(_extract_adf_text(child))
        return text
    return []


def _extract_adf_paragraphs(description: Dict[str, Any]) -> List[str]:
    paragraphs: List[str] = []
    for node in description.get("content", []) or []:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "paragraph":
            text = "".join(_extract_adf_text(node)).strip()
            if text:
                paragraphs.append(text)
            continue
        nested = []
        for child in node.get("content", []) or []:
            nested.append("".join(_extract_adf_text(child)).strip())
        nested = [value for value in nested if value]
        if nested:
            paragraphs.extend(nested)
    return paragraphs


def _extract_description_text(description: Any) -> str:
    if isinstance(description, str):
        return description.strip()
    if isinstance(description, dict):
        # Preserve Jira paragraph structure by joining paragraph blocks with newlines.
        paragraphs = _extract_adf_paragraphs(description)
        return "\n".join(paragraphs).strip()
    return ""


def _is_gherkin_description(text: str) -> bool:
    if not text:
        return False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("Given ", "When ", "Then ", "And ", "But ")):
            return True
    return False


def _split_gherkin_steps(text: str) -> str:
    if not text:
        return text
    step_re = re.compile(r"(?=\b(?:Given|When|Then|And|But)\b)")
    lines: List[str] = []
    for raw_line in text.splitlines():
        parts = [p.strip() for p in step_re.split(raw_line) if p.strip()]
        lines.extend(parts or [raw_line.strip()])
    return "\n".join([line for line in lines if line])


def split_story_and_ac(description: str) -> tuple[str, list[str]]:
    if not description:
        return "", []
    match = re.search(r"acceptance criteria\s*:", description, flags=re.IGNORECASE)
    if not match:
        return description.strip(), []
    executable_text = description[: match.start()].strip()
    ac_block = description[match.end() :].strip()
    criteria: list[str] = []
    for line in ac_block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = re.sub(r"^\s*(?:[-•]|\d+[\.\)])\s*", "", stripped)
        if cleaned:
            criteria.append(cleaned)
    return executable_text, criteria


def _build_auth_header(email: str, api_token: str) -> str:
    token = (api_token or "").strip()
    if token.lower().startswith("bearer "):
        return token
    if token.lower().startswith("basic "):
        return token
    if email:
        encoded = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"
    return f"Bearer {token}"


def _jira_request(
    url: str,
    email: str,
    api_token: str,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    auth_header = _build_auth_header(email, api_token)
    headers = {
        "Authorization": auth_header,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "testify-automator-ai/1.0",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, headers=headers, method=method, data=data)

    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        raise HTTPException(status_code=exc.code, detail=detail or "Jira request failed.") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail="Unable to reach Jira.") from exc

    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Invalid Jira response.") from exc


@router.post("/jira/import")
def import_from_jira(payload: JiraImportRequest):
    base_url = _normalize_base_url(payload.base_url)
    if not base_url:
        raise HTTPException(status_code=400, detail="Invalid Jira base URL.")

    if payload.issue_key:
        # Single-issue import: ignore project/JQL filters and fetch exactly one.
        jql = f'issueKey = "{payload.issue_key}"'
        max_results = 1
        issue_key_mode = True
    else:
        jql = (payload.jql or "").strip()
        max_results = payload.max_results
        issue_key_mode = False

    issue_keys = [k.strip() for k in (payload.issue_keys or []) if k and str(k).strip()]
    issue_key_clause = ""
    if issue_keys:
        quoted_keys = ", ".join(f'"{k}"' for k in issue_keys)
        issue_key_clause = f"issuekey in ({quoted_keys})"

    default_story_jql = f'project = "{payload.project_key}" AND issuetype = "Story" ORDER BY created DESC'
    default_all_jql = f'project = "{payload.project_key}" ORDER BY created DESC'
    if not jql:
        jql = default_story_jql
    if issue_key_clause and not issue_key_mode:
        jql = f"{jql} AND {issue_key_clause}"

    query = {
        "jql": jql,
        "fields": ["summary", "description", "issuetype"],
        "maxResults": max_results,
    }
    query_string = urlencode(
        {
            "jql": jql,
            "fields": "summary,description,issuetype",
            "maxResults": str(max_results),
        }
    )
    endpoints = [
        ("POST", f"{base_url}/rest/api/3/search/jql", query),
        ("GET", f"{base_url}/rest/api/3/search?{query_string}", None),
        ("GET", f"{base_url}/rest/api/2/search?{query_string}", None),
        ("GET", f"{base_url}/rest/api/latest/search?{query_string}", None),
    ]

    last_error: Optional[HTTPException] = None
    data: Dict[str, Any] = {}

    for method, url, body in endpoints:
        try:
            data = _jira_request(url, payload.email, payload.api_token, method=method, body=body)
            last_error = None
            break
        except HTTPException as exc:
            last_error = exc
            if exc.status_code not in {404, 410}:
                raise

    if last_error is not None:
        raise last_error
    issues = data.get("issues", []) or []
    if not issues and not issue_key_mode and not (payload.jql or "").strip():
        query["jql"] = default_all_jql
        query_string = urlencode(
            {
                "jql": default_all_jql,
                "fields": "summary,description,issuetype",
                "maxResults": str(max_results),
            }
        )
        endpoints = [
            ("POST", f"{base_url}/rest/api/3/search/jql", query),
            ("GET", f"{base_url}/rest/api/3/search?{query_string}", None),
            ("GET", f"{base_url}/rest/api/2/search?{query_string}", None),
            ("GET", f"{base_url}/rest/api/latest/search?{query_string}", None),
        ]
        last_error = None
        for method, url, body in endpoints:
            try:
                data = _jira_request(url, payload.email, payload.api_token, method=method, body=body)
                last_error = None
                break
            except HTTPException as exc:
                last_error = exc
                if exc.status_code not in {404, 410}:
                    raise
        if last_error is not None:
            raise last_error
        issues = data.get("issues", []) or []

    if issue_key_mode and not issues:
        raise HTTPException(status_code=404, detail="Jira issue not found or access denied.")

    stories: List[str] = []
    story_objects: List[Dict[str, Any]] = []
    for issue in issues:
        fields = issue.get("fields", {}) or {}
        summary = (fields.get("summary") or "").strip()
        description = _extract_description_text(fields.get("description"))
        jira_key = (issue.get("key") or "").strip()
        if description and _is_gherkin_description(description):
            # Keep Jira step formatting intact; do not prepend summary.
            description = _split_gherkin_steps(description)
            stories.append(description)
        elif summary and description:
            # Keep summary and ADF description in separate blocks for Jira parity.
            stories.append(f"{summary}\n\n{description}")
        elif summary:
            stories.append(summary)
        elif description:
            stories.append(description)

        executable_text, acceptance_criteria = split_story_and_ac(description)
        if not executable_text and summary:
            executable_text = summary
        story_objects.append(
            {
                "jira_key": jira_key or None,
                "summary": summary or None,
                "executable_text": executable_text,
                "acceptance_criteria": acceptance_criteria,
                "raw_description": description,
            }
        )

    return {"stories": stories, "story_objects": story_objects}
