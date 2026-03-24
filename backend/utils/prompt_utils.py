# utils/prompt_utils.py
import os
from pathlib import Path
from typing import List, Optional

def get_prompt(prompt_name: str, project_src_dir: Optional[Path]) -> str:
    """
    Gets a prompt, prioritizing the project-specific one if it exists,
    otherwise falling back to the global prompt.
    """
    # 1. Try to find the prompt in the active project's directory
    if project_src_dir:
        project_prompt_path = project_src_dir / "prompts" / prompt_name
        if project_prompt_path.exists():
            return project_prompt_path.read_text(encoding="utf-8")

    # 2. Fallback to the global prompts directory
    global_prompt_path = Path(__file__).resolve().parent.parent / "prompts" / prompt_name
    if not global_prompt_path.exists():
        raise FileNotFoundError(f"Prompt '{prompt_name}' not found in project or global directories.")
        
    return global_prompt_path.read_text(encoding="utf-8")

class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _safe_format(template: str, **kwargs: str) -> str:
    return template.format_map(_SafeFormatDict(**kwargs))


def build_prompt(
    story_block: str,
    method_map: dict,
    page_names: list[str],
    site_url: str,
    dynamic_steps: list[str],
    project_src_dir: Path,
) -> str:
    def _normalize_method(m: str) -> str:
        m_norm = m.strip()
        if m_norm.startswith("def "):
            m_norm = m_norm[4:].strip()
        return m_norm

    page_method_section = "\n".join(
        f"# {p}:\n" + "\n".join(f"- def {_normalize_method(m)}" for m in method_map.get(p, []))
        for p in page_names
    )
    dynamic_steps_joined = "\n".join(dynamic_steps)
    site_url_escaped = site_url.replace('"', '"')

    template = get_prompt("ui_test_generation.txt", project_src_dir)
    return _safe_format(
        template,
        story_block=story_block,
        site_url=site_url_escaped,
        page_method_section=page_method_section,
        dynamic_steps=dynamic_steps_joined,
    )

def build_security_prompt(
    story_block: str,
    method_map: dict,
    page_names: list[str],
    site_url: str,
    project_src_dir: Path,
    security_matrix: str = "",
) -> str:
    def _normalize_method(m: str) -> str:
        m_norm = m.strip()
        if m_norm.startswith("def "):
            m_norm = m_norm[4:].strip()
        return m_norm

    page_method_section = "\n".join(
        f"# {p}:\n" + "\n".join(f"- def {_normalize_method(m)}" for m in method_map.get(p, []))
        for p in page_names
    )
    input_methods = []
    prefixes = ("enter_", "fill_", "type_", "set_", "input_", "select_", "choose_", "add_", "pick_", "search_", "upload_", "set_value_", "type_in_")
    keywords = ("input", "field", "email", "password", "username", "name", "phone", "address", "zip", "code", "value", "text")
    for _page, methods in method_map.items():
        for method in methods:
            method_name = method.split("(", 1)[0].replace("def ", "").strip()
            if method_name.startswith(prefixes) or any(k in method_name for k in keywords):
                input_methods.append(method_name)
    payload_list = "\n".join(
        f"- {m}(page, <payload>)" for m in sorted(set(input_methods))
    )
    site_url_escaped = site_url.replace('"', '"')

    if not (security_matrix or "").strip():
        template = get_prompt("security_test_generation.txt", project_src_dir)
        return _safe_format(
            template,
            story_block=story_block,
            page_method_section=page_method_section,
            payload_list=payload_list,
            site_url=site_url_escaped,
        )
    else:
        template = get_prompt("security_test_generation_matrix.txt", project_src_dir)
        return _safe_format(
            template,
            story_block=story_block,
            security_matrix=security_matrix,
            site_url=site_url_escaped,
            page_method_section=page_method_section,
            payload_list=payload_list,
        )

def build_accessibility_prompt(
    story_block: str,
    method_map: dict,
    page_names: list[str],
    site_url: str,
    project_src_dir: Path,
) -> str:
    def _normalize_method(m: str) -> str:
        m_norm = m.strip()
        if m_norm.startswith("def "):
            m_norm = m_norm[4:].strip()
        return m_norm

    page_method_section = "\n".join(
        f"# {p}:\n" + "\n".join(f"- def {_normalize_method(m)}" for m in method_map.get(p, []))
        for p in page_names
    )

    template = get_prompt("accessibility_test_generation.txt", project_src_dir)
    return _safe_format(
        template,
        story_block=story_block,
        page_method_section=page_method_section,
    )


# ============================================================
# API BEHAVE STEP DEFINITIONS PROMPT
# ============================================================

def build_api_step_definitions_prompt(feature_text: str, page_methods: list[str]) -> str:
    methods_block = "\n".join(f"- {name}" for name in page_methods) or "- <no methods found>"
    return f"""
You are a senior Python test automation architect.

I have the following Gherkin feature for API testing:

{feature_text}

AVAILABLE API PAGE METHODS (REQRES):
{methods_block}

Your task is to generate a Python Behave step definitions file that follows THESE STRICT RULES:

CRITICAL RULES (DO NOT VIOLATE):
1. DO NOT hardcode any values such as status codes, tokens, emails, passwords, or saved keys.
2. DO NOT embed literal numbers like 200 inside decorators.
3. DO NOT repeat step text manually inside function bodies.
4. Step decorators MUST use regex or parameterized expressions.
5. Step functions MUST receive parameters directly from Behave.
6. DO NOT instantiate the API client class (no REQRES()).
7. DO NOT call response.json() directly; use response.body when available.
8. Decorator patterns MUST NOT include Gherkin keywords (Given/When/Then/And/But). Match only the step text.

IMPLEMENTATION REQUIREMENTS:
1. Use context.table dynamically for all table-driven inputs.
2. Use context.saved for storing and retrieving dynamic values.
3. Implement explicit step functions for each decorator (no string-matching dispatch).
4. Use shared helper functions only for reusable utilities (table parsing, response body, status checks).
5. Status codes must be parsed dynamically from the step text.
6. Field names (like "token") must be extracted dynamically using regex.
7. Token names (like "registered_token") must be dynamically resolved.
8. Validate:
   - Response status
   - Non-empty response fields
   - Equality with saved values
   - Presence of a non-empty users list (if missing, do not fail; use a safe fallback response)
9. Use the existing REQRES API client.
10. Support both offline and online execution using REQRES_BASE_URL; if missing, default to offline://stub and NEVER fail on missing base URL.
11. Use a table-to-dict helper that does not assume column names.
12. When invoking client methods, pass payloads as dictionaries with a "body" key if the method expects a body.
13. For protected requests, build Authorization headers dynamically from context.saved.
14. Include steps for negative assertions like: response should not contain "<field>".
15. Include steps for generic API method calls, including:
    - client calls api method "<method>" without field "<field>"
    - client calls api method "<method>" with invalid field "<field>"

STEP DECORATOR STYLE (MANDATORY EXAMPLES):
- @then(r'response status should be (\\d{{3}})')
- @step(r'response should contain a non-empty "([^"]+)"')
- @step(r'save response field "([^"]+)" as "([^"]+)"')
- @when(r'client fetches protected user data using saved token "([^"]+)"')

OUTPUT REQUIREMENTS:
- Produce a complete, runnable file: steps/register_flow_steps.py
- Clean, readable, production-quality Python
- No duplicated logic
- No hardcoded strings inside handlers
- Do NOT use "if <text> in step_text" style dispatch
- Import the client as: from pages.api_pages import REQRES (do NOT use reqres_client or other modules)

Generate ONLY the step definition code.
"""


# ============================================================
# API TESTING PAGE METHODS PROMPT
# ============================================================

def build_api_page_methods_prompt(
    service_name: str,
    endpoints: list[tuple[str, str]],
    auth_mechanism: str,
    class_name: str,
) -> str:
    endpoints_section = "\n".join(
        f"- {verb.upper()} {path}" for verb, path in endpoints
    ) or "- <HTTP_VERB> <PATH>"

    return f"""
You are SmartAI, an expert QA automation assistant. Produce a Python module that acts as the page-layer for the {service_name} API so test cases can call stable helper methods instead of crafting requests manually.

CONTEXT
- Target endpoints:
{endpoints_section}
- Authentication summary: {auth_mechanism}
- Generated class name: {class_name}

MODULE REQUIREMENTS
1. Emit a complete, ready-to-use module (no markdown) with:
   • lightweight ApiResponse wrapper (status/body helpers)
   • ServiceClient abstraction that issues HTTP requests and applies auth data
   • optional offline/stub client that returns deterministic responses when base_url is missing or set to offline:// …
2. Provide class {class_name} that exposes classmethods for configuring the client and invoking each endpoint.
   • configure(base_url, *, credentials=None, default_security=None, timeout=30, offline_fallback=True)
   • _client_or_fail(), _require(), _optional_dict(), helper to extract request bodies
   • each endpoint gets a classmethod named after the OpenAPI operation (snake_case) accepting a dict with keys like "body", "query", "headers"
   • validate required path parameters via _require and substitute them into the URL template
   • return ApiResponse objects produced by ServiceClient
3. If the spec exposes friendlier aliases (descriptions/operationIds), add thin wrapper aliases that forward to the canonical method.
4. Keep logic deterministic: do not invent additional endpoints or parameters; mirror the schema exactly and reuse provided values when story tables mention them.
5. Include concise docstrings describing what each method does and the expected status code or important assertions.

USAGE NOTES
- Close the file with a short TODO/assumption comment (one or two sentences) highlighting anything that needs manual follow-up (e.g., real auth secrets).
- No markdown, no explanatory prose outside of docstrings/comments.
- Keep formatting PEP8-friendly so it can be dropped into the repository without edits.
"""


def build_api_positive_negative_prompt(
    story_block: str,
    service_name: str,
    endpoints: list[tuple[str, str]],
    class_name: str,
    positive_test_name: str = "test_positive_flow",
    negative_test_name: str = "test_negative_flow",
) -> str:
    endpoints_section = "\n".join(
        f"- {verb.upper()} {path}" for verb, path in endpoints
    ) or "- <HTTP_VERB> <PATH>"

    return f"""
You are SmartAI, an expert API test engineer. Based on the user story below, generate two pytest-style test functions that exercise the {service_name} API client {class_name} using only existing page-method wrappers.

USER STORY CONTEXT:
{story_block}

AVAILABLE ENDPOINTS TO LEVERAGE:
{endpoints_section}

OUTPUT REQUIREMENTS
- Produce EXACTLY two test functions named {positive_test_name}(service_client) and {negative_test_name}(service_client).
- Assume service_client exposes the methods defined in {class_name}; call those helpers directly instead of raw requests.
- Start each test by arranging required data, then invoke the appropriate client methods, and finish with clear assertions on status codes and critical fields.
- Positive test: follow the happy path from the story, validate a 2xx status, and assert key response attributes.
- Negative test: Generate all possible negative testcases for the given userstory, expect the documented error status, and assert the error message/body.
- Assertion guidance: avoid hard-coded response values unless the story/spec provides them; prefer asserting types, presence of required keys, and relationships between responses (for example, returned tokens should be non-empty strings and consistent across steps).
- If pagination fields (page/page_number) are present, assert they match the request; if they are absent, do not fail the test.
- If response includes token/registered_token fields, assert they match the login/register token captured earlier.
- For list-style endpoints, assert the list exists, is non-empty, and the first record includes an id-like field when present.
- Highlight TODO comments when the story lacks required data (for example, authentication secrets or fixture payloads).
- Do not add imports, helper functions, or explanatory prose—return only executable Python test code.
"""


def build_flow_negative_cases_prompt(
    story_block: str,
) -> str:
    return f"""
You are a Principal Test Architect specializing in Enterprise API Automation,
Negative Testing, and Risk-Based API Validation.

Your task is to generate a SMALL, STABLE, EXECUTABLE set of NEGATIVE, EDGE,
and RISK-BASED TEST CASES derived STRICTLY from the given POSITIVE USER STORY.

This is a CONTROLLED, GATED prompt.
You MUST decide which categories are SAFE to generate based ONLY on
observable evidence in the POSITIVE USER STORY and real backend behavior.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GLOBAL NON-NEGOTIABLE RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Generate ONLY tests that can run against a REAL backend.
- NEVER assume authentication, authorization, roles, permissions,
  rate limiting, security headers, or concurrency unless EXPLICITLY
  demonstrated in the POSITIVE USER STORY.
- NEVER invent failures based on theory, best practices, or security ideals.
- Prefer validation failures (400 / 422) over auth failures (401 / 403)
  unless auth enforcement is PROVEN by the story.
- Do NOT assert exact error messages unless reliably returned by the API.
- Do NOT fabricate users, tokens, IDs, permissions, or backend state.
- Fail-fast: once an API call fails, the test MUST stop immediately.
- Each test must break ONLY ONE contract.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CATEGORIES TO CONSIDER (AUTO-GATED)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
You MUST evaluate EACH category below and decide ALLOW or SKIP
based on evidence in the story and observable backend behavior.

1. Missing / Invalid Authentication
2. Malformed Requests
3. Edge Parameters (SAFE boundaries only)
4. State / Flow Logic Errors
5. Concurrency / Race Conditions
6. Rate Limiting / Abuse
7. IDOR / BOLA
8. Security Headers
9. Backend Failures (observable only)
10. Fuzz / Mutation (controlled, single-field only)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CATEGORY ELIGIBILITY RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Authentication tests:
  ALLOW ONLY if the positive story clearly proves auth/token enforcement.
- Malformed requests:
  ALLOW when request schema or required fields are visible.
- Edge parameters:
  ALLOW only realistic boundaries (empty, null, max length).
  NO stress, NO abuse, NO large payloads.
- State/flow logic:
  ALLOW when steps have clear dependencies.
- Backend failures:
  ALLOW only if the API naturally returns 5xx or partial responses.
- Fuzz/mutation:
  ALLOW only ONE controlled mutation per request.
- Concurrency, rate limiting, IDOR/BOLA, security headers:
  SKIP unless explicitly demonstrated and observable in the story.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1: FLOW & CAPABILITY ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Identify API calls in execution order.
- Identify required request fields per step.
- Identify mandatory response fields used by later steps.
- Identify state/data dependencies between steps.
- Identify observable backend behavior (status codes, body presence).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2: CATEGORY DECISION (INTERNAL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For EACH category (1–10), decide internally:
- allowed: true / false
- reason: based strictly on evidence

DO NOT expose internal reasoning.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 3: TEST CASE GENERATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Generate tests ONLY from ALLOWED categories.
- Generate at most 1 test per category.
- Generate NO MORE THAN 6 total tests.
- Each test must:
  • break ONLY ONE contract
  • call the real API
  • fail via a REAL API response
  • stop immediately after failure

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT (STRICT — JSON ONLY)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{{
  "generated_testcases": [
    {{
      "id": "NEG-01",
      "category": "input_break | edge | state | backend_failure | auth | fuzz",
      "scenario_description": "...",
      "broken_contract": "...",
      "api_expected_result": {{
        "status_code": [400, 422],
        "behavior": "Observable API rejection or failure"
      }}
    }}
  ],
  "skipped_categories": [
    {{
      "category": "rate_limit",
      "reason": "No observable evidence in positive story"
    }}
  ]
}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABSOLUTE CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- JSON output ONLY.
- Do NOT generate skipped categories.
- Do NOT exceed 6 tests.
- Do NOT include markdown, explanations, or commentary.
- Every test must be executable against a real backend.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
POSITIVE USER STORY (REFERENCE FLOW)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{story_block}  add this promt
"""


def build_flow_negative_gherkin_prompt(
    story_block: str,
    api_methods: list[str],
) -> str:
    methods_block = "\n".join(f"- {name}" for name in (api_methods or [])) or "- <no methods found>"
    return f"""
You are a senior API test engineer. Generate NEGATIVE Gherkin scenarios ONLY.

INPUT STORY (POSITIVE FLOW):
{story_block}

AVAILABLE API METHODS (use these names exactly):
{methods_block}

RULES
- Output ONLY Gherkin scenario blocks (no Feature line, no markdown).
- Use these step patterns only:
  Given the API service is available
  When client calls api method "<method>" with:
    | field | value |
  When client calls api method "<method>" without field "<field>"
  When client calls api method "<method>" with invalid field "<field>"
  Then response status should be 400
  Then response status should be 401
  And response should not contain "<field>"
- At most 3 scenarios total.
- Each scenario must break only one contract.
- Use method names from the available list.
- Field names must come from the story or request tables; do NOT invent new fields.
- If the negative case removes or corrupts an auth token (Authorization header or token field),
  use status 401. Otherwise use 400.
- For invalid credentials such as wrong password, use status 400.

OUTPUT ONLY Gherkin scenarios now.
"""


# ============================================================
# ETL VALIDATION PROMPT
# ============================================================

def build_etl_validation_prompt(user_request: str, project_src_dir: Optional[Path]) -> str:
    template = get_prompt("etl_prompt.txt", project_src_dir)
    return f"{template}\n\nUSER REQUEST:\n{user_request}"

