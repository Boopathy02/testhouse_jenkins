from __future__ import annotations

import copy
import json
import keyword
import os
import re
import textwrap
from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apis.api_specs_api import (
    ApiTestsRequest,
    _build_snippet_payload,
    _ensure_active_project,
    _load_project_spec_context,
    _normalize_identifier,
    _partition_story_steps,
    _extract_story_actor_details,
    _select_specs,
    _tokenize,
)
from apis.projects_api import _project_root, get_current_user
from database.models import User
from database.project_storage import DatabaseBackedProjectStorage
from database.session import get_db
from utils.prompt_utils import build_api_page_methods_prompt, build_flow_negative_cases_prompt
from services.test_generation_utils import openai_client


router = APIRouter()

_FILE_NAME = "api_pages.py"


def _compact_token(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _service_env_prefix(name: Optional[str]) -> str:
    token = re.sub(r"[^0-9a-zA-Z]+", "_", (name or "").strip().upper())
    token = re.sub(r"_+", "_", token)
    return token.strip("_") or "SERVICE"


def _build_base_url_credentials(services: List[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    seen: Set[str] = set()
    for service in services:
        service_name = service.get("service") or service.get("class_name") or "Service"
        env_prefix = _service_env_prefix(service_name)
        base_url = (
            service.get("base_url")
            or service.get("baseUrl")
            or service.get("BASE_URL")
            or service.get("Base_URL")
        )
        if not isinstance(base_url, str):
            continue
        value = base_url.strip()
        if not value:
            continue
        line = f"{env_prefix}_BASE_URL={value}"
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


def _describe_auth_mechanism(service: Dict[str, Any]) -> str:
    schemes = service.get("auth_schemes") or {}
    if not schemes:
        service_auth = service.get("service_auth") or {}
        if isinstance(service_auth, dict) and service_auth:
            return "custom authentication metadata provided in service_auth"
        return "no authentication (public endpoints)"

    descriptions: List[str] = []
    for scheme_name in sorted(schemes):
        scheme = schemes.get(scheme_name) or {}
        scheme_type = (scheme.get("type") or "custom").lower()

        if scheme_type == "apikey":
            key_name = scheme.get("name") or scheme_name
            location = scheme.get("in") or scheme.get("location") or "header"
            descriptions.append(f"API key '{key_name}' via {location}")
            continue

        if scheme_type == "http":
            http_scheme = (scheme.get("scheme") or "").lower()
            if http_scheme == "basic":
                descriptions.append("HTTP Basic authentication")
            elif http_scheme == "bearer":
                descriptions.append("HTTP Bearer token")
            else:
                descriptions.append(f"HTTP auth scheme '{http_scheme or 'custom'}'")
            continue

        if scheme_type in {"oauth2", "openidconnect"}:
            flows = scheme.get("flows") if isinstance(scheme.get("flows"), dict) else None
            flow_names = sorted(flows.keys()) if flows else []
            base = "OAuth2" if scheme_type == "oauth2" else "OpenID Connect"
            if flow_names:
                descriptions.append(f"{base} ({', '.join(flow_names)})")
            else:
                descriptions.append(base)
            continue

        descriptions.append(f"{scheme_type} authentication")

    return "; ".join(descriptions)


def _to_snake_case(value: Optional[str]) -> str:
    if not value:
        return ""
    candidate = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value.strip())
    candidate = re.sub(r"[^0-9a-zA-Z]+", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate)
    return candidate.strip("_").lower()


def _extract_path_params(path: Optional[str]) -> List[str]:
    if not path:
        return []
    return [match for match in re.findall(r"{([^}]+)}", path) if match]


def _looks_like_example_id(segment: Optional[str]) -> bool:
    if not segment:
        return False
    token = segment.strip()
    if not token:
        return False
    if token.isdigit():
        return True
    if re.fullmatch(r"[0-9a-fA-F-]{8,}", token):
        return True
    return False


def _is_form_encoded_case(case: Dict[str, Any]) -> bool:
    headers = case.get("default_headers") or case.get("headers") or {}
    content_type = ""
    for key in ("Content-Type", "content-type"):
        if key in headers:
            content_type = headers[key]
            break
    lowered = (content_type or "").lower()
    return "application/x-www-form-urlencoded" in lowered or "multipart/form-data" in lowered


def _line_mentions_create(text: Optional[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(token in lowered for token in ("create", "add", "post", "new"))


def _select_best_create_case(cases: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_score = -1
    for case in cases:
        score = 0
        method = (case.get("http_method") or "").upper()
        operation = (case.get("operation") or case.get("method_name") or "").lower()
        if method == "POST":
            score += 5
        if case.get("requires_payload"):
            score += 2
        if any(token in operation for token in ("create", "add", "post")):
            score += 3
        if _is_form_encoded_case(case):
            score -= 3
        if score > best_score:
            best = case
            best_score = score
    return best


def _resolve_case_example(case: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not case:
        return None
    primary = case.get("primary_example")
    if isinstance(primary, dict):
        return copy.deepcopy(primary)
    examples = case.get("examples") or {}
    alias = case.get("example_alias")
    if alias and alias in examples and isinstance(examples[alias], dict):
        return copy.deepcopy(examples[alias])
    for payload in examples.values():
        if isinstance(payload, dict):
            return copy.deepcopy(payload)
    return None


def _extract_case_request_schema(case: Dict[str, Any]) -> Dict[str, Any]:
    schema = case.get("request_schema") or {}
    if isinstance(schema, dict) and "schema" in schema and isinstance(schema["schema"], dict):
        schema = schema["schema"]
    return schema if isinstance(schema, dict) else {}


def _method_identifier(case: Dict[str, Any]) -> str:
    method = case.get("method_name") or case.get("operation")
    return _to_snake_case(method) or "operation"


def _case_role_score(case: Dict[str, Any], role: str, preferred_class: Optional[str]) -> int:
    method = (case.get("http_method") or "").upper()
    operation_name = (case.get("operation") or case.get("method_name") or "").lower()
    path_literal = (case.get("path") or "").lower()
    class_name = case.get("class_name")

    score = 0
    if preferred_class and class_name == preferred_class:
        score += 3

    if role == "register":
        if method == "POST":
            score += 3
        if any(token in operation_name for token in ("register", "sign_up", "signup", "enroll")):
            score += 4
        if "/register" in path_literal:
            score += 4
        if any(token in operation_name for token in ("create", "add", "post")):
            score += 2
        if case.get("requires_payload"):
            score += 1
    elif role == "login":
        if method in {"POST", "PUT"}:
            score += 3
        if any(token in operation_name for token in ("login", "log_in", "signin", "sign_in", "authenticate")):
            score += 5
        if "/login" in path_literal or "auth" in path_literal:
            score += 3
        if case.get("requires_payload"):
            score += 1
    elif role == "fetch":
        if method == "GET":
            score += 4
        if any(token in operation_name for token in ("fetch", "get", "list", "retrieve", "records", "users")):
            score += 3
        if any(token in path_literal for token in ("/user", "/record", "/data")):
            score += 2
        if case.get("requires_payload"):
            score -= 2
    return score


def _identify_story_flow_cases(cases: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    if not cases:
        return {}

    class_counts = Counter(case.get("class_name") for case in cases if case.get("class_name"))
    preferred_class = class_counts.most_common(1)[0][0] if class_counts else None

    ranked: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for role in ("register", "login", "fetch"):
        scored: List[Tuple[int, Dict[str, Any]]] = []
        for case in cases:
            score = _case_role_score(case, role, preferred_class)
            if score > 0:
                scored.append((score, case))
        scored.sort(key=lambda item: (item[0], _method_identifier(item[1])), reverse=True)
        ranked[role] = scored

    selected: Dict[str, Dict[str, Any]] = {}
    used_ids: Set[int] = set()

    for role in ("register", "login", "fetch"):
        candidates = ranked.get(role) or []
        chosen_case: Optional[Dict[str, Any]] = None
        for score, case in candidates:
            case_id = id(case)
            if case_id in used_ids:
                continue
            chosen_case = case
            used_ids.add(case_id)
            break
        if chosen_case is None and candidates:
            _, fallback_case = candidates[0]
            chosen_case = fallback_case
            used_ids.add(id(fallback_case))
        if chosen_case is not None:
            selected[role] = chosen_case

    if selected.get("login") is selected.get("register"):
        login_candidates = ranked.get("login") or []
        for _, case in login_candidates:
            if case is not selected["register"]:
                selected["login"] = case
                break

    return selected


def _case_payload_example(case: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    example = _resolve_case_example(case)
    if not isinstance(example, dict):
        return {}
    if isinstance(example.get("body"), dict):
        return dict(example["body"])
    if isinstance(example.get("payload"), dict):
        return dict(example["payload"])
    return dict(example)


def _search_payload_for_keys(payload: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in payload.values():
        if isinstance(value, dict):
            nested = _search_payload_for_keys(value, keys)
            if nested:
                return nested
    return None


def _extract_actor_credentials(
    story: Optional[str],
    register_case: Dict[str, Any],
    login_case: Dict[str, Any],
) -> Tuple[str, str]:
    defaults = _extract_story_actor_details(story)
    email = defaults.get("email") if isinstance(defaults, dict) else None
    password = defaults.get("password") if isinstance(defaults, dict) else None

    for case in (register_case, login_case):
        payload = _case_payload_example(case)
        if not payload:
            continue
        if not email:
            email = _search_payload_for_keys(payload, ("email", "username", "user"))
        if not password:
            password = _search_payload_for_keys(payload, ("password", "passcode", "secret"))
        if email and password:
            break

    email = email or _placeholder_scalar("email")
    password = password or _placeholder_scalar("password")
    return email, password


def _find_key_by_tokens(mapping: Dict[str, Any], tokens: Tuple[str, ...]) -> Optional[str]:
    for key in mapping:
        lowered = str(key).lower()
        if any(token in lowered for token in tokens):
            return str(key)
    return None


def _fallback_request_fields(case: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(case, dict):
        return []
    schema = _extract_case_request_schema(case)
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        return [str(key) for key in props.keys()]
    example = _case_payload_example(case)
    if isinstance(example, dict) and example:
        return [str(key) for key in example.keys()]
    return []


def _infer_auth_field_names(case: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(case, dict):
        return None, None
    example = _case_payload_example(case)
    if isinstance(example, dict):
        email_key = _find_key_by_tokens(example, ("email", "username", "user", "login"))
        password_key = _find_key_by_tokens(example, ("password", "passcode", "secret", "pwd"))
        if email_key or password_key:
            return email_key, password_key

    schema = _extract_case_request_schema(case)
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict):
        email_key = _find_key_by_tokens(props, ("email", "username", "user", "login"))
        password_key = _find_key_by_tokens(props, ("password", "passcode", "secret", "pwd"))
        if email_key or password_key:
            return email_key, password_key

    return None, None


def _derive_invalid_value(field_name: str, base_value: Any) -> Any:
    key = (field_name or "").lower()
    candidate = _placeholder_scalar(field_name) if base_value in (None, "") else base_value
    if isinstance(candidate, (int, float)):
        return -abs(candidate) - 1
    if not isinstance(candidate, str):
        candidate = str(candidate)
    if "email" in key:
        return candidate.replace("@", "") if "@" in candidate else f"{candidate}.invalid"
    if any(token in key for token in ("password", "passcode", "secret", "pwd")):
        return f"{candidate}_invalid"
    if "token" in key:
        return f"invalid_{_to_snake_case(field_name) or 'token'}"
    return f"invalid_{_to_snake_case(field_name) or 'value'}"


def _derive_long_string(field_name: str, base_value: Any, extra: int = 120) -> str:
    key = (field_name or "").lower()
    candidate = _placeholder_scalar(field_name) if base_value in (None, "") else base_value
    if not isinstance(candidate, str):
        candidate = str(candidate)
    if "email" in key and "@" in candidate:
        local, _, domain = candidate.partition("@")
        local = (local or "user") + ("a" * extra)
        return f"{local}@{domain or 'example.com'}"
    return candidate + ("x" * extra)


def _infer_token_fields_from_schema(case: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(case, dict):
        return []
    response_schema = case.get("response_schema") or {}
    schema = response_schema.get("schema") if isinstance(response_schema, dict) else None
    schema = schema if isinstance(schema, dict) else response_schema
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return []
    return [key for key in props.keys() if "token" in str(key).lower()]


def _extract_page_from_mapping(mapping: Optional[Dict[str, Any]]) -> Optional[int]:
    if not isinstance(mapping, dict):
        return None
    for key in ("page", "page_number", "page_index"):
        value = mapping.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _extract_default_page(story: Optional[str], fetch_case: Dict[str, Any]) -> int:
    defaults = _extract_story_actor_details(story)
    raw_page = defaults.get("page") if isinstance(defaults, dict) else None
    if isinstance(raw_page, int) and raw_page > 0:
        return raw_page

    for candidate in (
        fetch_case.get("default_query"),
        fetch_case.get("query"),
    ):
        page_value = _extract_page_from_mapping(candidate)
        if page_value is not None:
            return page_value

    example = _case_payload_example(fetch_case)
    if example:
        page_value = _extract_page_from_mapping(example.get("query"))
        if page_value is not None:
            return page_value

    return 1


def _placeholder_scalar(name: Optional[str]) -> Any:
    token = (name or "").strip().lower()
    snake = _to_snake_case(name) or "value"
    if not token:
        return snake
    if token.endswith("id") or token == "id":
        return 1
    if any(keyword in token for keyword in ("count", "number", "total", "amount")):
        return 1
    if any(keyword in token for keyword in ("email", "mail")):
        return "user@example.com"
    if any(keyword in token for keyword in ("url", "uri")):
        return "https://example.com/resource"
    if any(keyword in token for keyword in ("flag", "enabled", "active")):
        return True
    if any(keyword in token for keyword in ("name", "title", "label")):
        return f"{snake}_name"
    if "status" in token:
        return "available"
    return f"{snake}_value"


_GENERIC_PLACEHOLDER_LITERALS: Set[str] = {"value", "<value>", "{value}", "value_value", "sample", "any", "*", ""}


def _is_placeholder_literal(candidate: Any) -> bool:
    if candidate in ({}, None):
        return True
    if isinstance(candidate, str):
        token = candidate.strip().lower()
        return token in _GENERIC_PLACEHOLDER_LITERALS
    return False


def _sanitize_query_template_value(name: str, value: Any) -> Any:
    if isinstance(value, list):
        sanitized: List[Any] = []
        for item in value:
            cleaned = _sanitize_query_template_value(name, item)
            if isinstance(cleaned, list):
                sanitized.extend(cleaned)
                continue
            if _is_placeholder_literal(cleaned):
                continue
            sanitized.append(cleaned)
        if not sanitized:
            sanitized.append(_placeholder_scalar(name))
        return sanitized
    if isinstance(value, tuple) or isinstance(value, set):
        return _sanitize_query_template_value(name, list(value))
    if isinstance(value, str):
        return _placeholder_scalar(name) if _is_placeholder_literal(value) else value
    if _is_placeholder_literal(value):
        return _placeholder_scalar(name)
    if isinstance(value, dict):
        sanitized_dict: Dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _sanitize_query_template_value(key or name, item)
            if _is_placeholder_literal(cleaned):
                continue
            sanitized_dict[key] = cleaned
        return sanitized_dict
    return value


def _first_query_fallback(name: str, value: Any) -> Any:
    if isinstance(value, list):
        for item in value:
            fallback = _first_query_fallback(name, item)
            if not _is_placeholder_literal(fallback):
                return fallback
        return _placeholder_scalar(name)
    if isinstance(value, tuple) or isinstance(value, set):
        return _first_query_fallback(name, list(value))
    if isinstance(value, dict):
        return value if value else _placeholder_scalar(name)
    if _is_placeholder_literal(value):
        return _placeholder_scalar(name)
    return value


def _coerce_parameter_payload(name: str, definition: Any) -> Any:
    if isinstance(definition, list):
        coerced_items = [
            _coerce_parameter_payload(name, item)
            for item in definition
        ]
        cleaned = [item for item in coerced_items if item not in ({}, None, "")]
        if not cleaned:
            cleaned = [_placeholder_scalar(name)]
        return cleaned

    if isinstance(definition, str):
        token = definition.strip()
        if token.lower() in {"value", "<value>", "{value}"}:
            return _placeholder_scalar(name)
        return definition

    if isinstance(definition, dict):
        if "enum" in definition and isinstance(definition["enum"], list) and definition["enum"]:
            first = definition["enum"][0]
            if isinstance(first, (dict, list)):
                return copy.deepcopy(first)
            return first
        if "default" in definition and definition["default"] not in ({}, None):
            return copy.deepcopy(definition["default"]) if isinstance(definition["default"], (dict, list)) else definition["default"]
        if "schema" in definition and isinstance(definition["schema"], dict):
            candidate = _schema_placeholder(definition["schema"])
            if candidate not in ({}, None):
                return candidate
        if "type" in definition:
            type_hint = definition.get("type")
            if type_hint == "array":
                items = definition.get("items") or {}
                item_value = _schema_placeholder(items) if isinstance(items, dict) else _placeholder_scalar(name)
                if item_value in ({}, None, "", "sample"):
                    item_value = _placeholder_scalar(name)
                if isinstance(item_value, list):
                    if item_value:
                        return item_value
                    item_value = _placeholder_scalar(name)
                if item_value in ({}, None):
                    item_value = _placeholder_scalar(name)
                return [item_value]
            if type_hint == "integer":
                return 1
            if type_hint == "number":
                return 1.0
            if type_hint == "boolean":
                return True
            if type_hint == "string":
                return _placeholder_scalar(name)
        if "example" in definition and definition["example"] not in ({}, None):
            return definition["example"]
        if "description" in definition and len(definition) == 1:
            return _placeholder_scalar(name)
    return definition


def _schema_placeholder(schema: Dict[str, Any], depth: int = 0) -> Any:
    if depth > 6 or not isinstance(schema, dict):
        return {}

    for key in ("example", "const", "default"):
        if key in schema and schema[key] is not None:
            value = schema[key]
            return copy.deepcopy(value) if isinstance(value, (dict, list)) else value

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        value = enum_values[0]
        return copy.deepcopy(value) if isinstance(value, (dict, list)) else value

    for composite in ("allOf", "oneOf", "anyOf"):
        options = schema.get(composite)
        if isinstance(options, list) and options:
            candidate = _schema_placeholder(options[0], depth + 1)
            if candidate not in ({}, None):
                return candidate

    schema_type = schema.get("type")
    if schema_type == "object" or (schema_type is None and schema.get("properties")):
        properties = schema.get("properties") or {}
        required = schema.get("required") or list(properties.keys())
        result: Dict[str, Any] = {}
        for key, subschema in properties.items():
            placeholder = _schema_placeholder(subschema, depth + 1)
            if placeholder in ({}, None):
                placeholder = _placeholder_scalar(key)
            result[key] = placeholder
        for key in required:
            if key not in result:
                result[key] = _placeholder_scalar(key)
        return result

    if schema_type == "array":
        item_schema = schema.get("items") or {}
        item_value = _schema_placeholder(item_schema, depth + 1)
        if item_value in ({}, None):
            item_value = {}
        return [item_value]

    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1.0
    if schema_type == "boolean":
        return False
    if schema_type == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "1970-01-01T00:00:00Z"
        if fmt == "date":
            return "1970-01-01"
        if fmt == "time":
            return "00:00:00"
        if fmt == "email":
            return "user@example.com"
        if fmt == "uuid":
            return "00000000-0000-0000-0000-000000000000"
        pattern = schema.get("pattern")
        if pattern:
            return f"sample_for_{_to_snake_case(pattern) or 'pattern'}"
        return "sample"

    return _placeholder_scalar(schema.get("title") or schema.get("description"))


def _build_case_payload_template(case: Dict[str, Any]) -> Any:
    schema = _extract_case_request_schema(case)
    if not schema:
        return {}
    return _schema_placeholder(schema)


def _fallback_param_value(name: Optional[str]) -> Any:
    return _placeholder_scalar(name)


def _find_story_literal(lines: List[str], required_tokens: Set[str]) -> Optional[str]:
    if not lines:
        return None
    lowered_tokens = {token.lower() for token in required_tokens if token}
    for line in lines:
        lower_line = line.lower()
        if all(token in lower_line for token in lowered_tokens):
            for pattern in (r'"([^\"]+)"', r"'([^']+)'"):
                match = re.search(pattern, line)
                if match:
                    return match.group(1)
    return None


def _find_story_number(lines: List[str], required_tokens: Set[str]) -> Optional[int]:
    if not lines:
        return None
    lowered_tokens = {token.lower() for token in required_tokens if token}
    for line in lines:
        lower_line = line.lower()
        if all(token in lower_line for token in lowered_tokens):
            number_match = re.search(r"\b(\d+)\b", line)
            if number_match:
                try:
                    return int(number_match.group(1))
                except ValueError:
                    continue
    return None


def _parse_status_assertion(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    lowered = text.lower()
    if "success" in lowered:
        return 200
    if "not found" in lowered:
        return 404
    if "unauthorized" in lowered:
        return 401
    if "forbidden" in lowered:
        return 403
    digits = re.search(r"\b(\d{3})\b", lowered)
    if digits:
        try:
            return int(digits.group(1))
        except ValueError:
            return None
    return None


def _parse_body_contains_assertion(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for pattern in (r'"([^\"]+)"', r"'([^']+)'"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def _infer_story_param_value(lines: List[str], original: str, operation_label: str) -> Optional[Any]:
    lowered_param = (original or "").lower()
    if not lowered_param:
        return None
    for line in lines:
        lowered_line = line.lower()
        if lowered_param in lowered_line or (operation_label and operation_label.lower() in lowered_line):
            number_match = re.search(r"\b\d+\b", line)
            if number_match:
                try:
                    return int(number_match.group(0))
                except ValueError:
                    return number_match.group(0)
            for pattern in (r'"([^\"]+)"', r"'([^']+)'"):
                match = re.search(pattern, line)
                if match:
                    return match.group(1)
    if lowered_param.endswith("id"):
        for line in lines:
            lowered_line = line.lower()
            if " id" in lowered_line or lowered_line.startswith("id"):
                number_match = re.search(r"\b(\d+)\b", line)
                if number_match:
                    try:
                        return int(number_match.group(1))
                    except ValueError:
                        return number_match.group(1)
        for line in lines:
            number_match = re.search(r"\b(\d+)\b", line)
            if number_match:
                try:
                    return int(number_match.group(1))
                except ValueError:
                    return number_match.group(1)
    return None


def _story_mentions_spec(story: Optional[str], spec: Any) -> bool:
    story_text = (story or "").strip()
    if not story_text:
        return False
    story_lower = story_text.lower()
    story_compact = _compact_token(story_text)
    candidates: List[str] = []
    for item in (
        getattr(spec, "operation_name", None),
        getattr(spec, "path", None),
        getattr(spec, "service_name", None),
        getattr(spec, "key", None),
        getattr(spec, "description", None),
    ):
        if not item:
            continue
        candidate = str(item)
        candidates.append(candidate)
        compact = _compact_token(candidate)
        if compact:
            candidates.append(compact)
    for candidate in candidates:
        sniff = candidate.strip().lower()
        if not sniff:
            continue
        if sniff in story_lower:
            return True
        compact_sniff = _compact_token(sniff)
        if compact_sniff and compact_sniff in story_compact:
            return True
    return False


def _parse_non_empty_field_assertion(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    if "non-empty" not in lowered and "non empty" not in lowered:
        return None
    match = re.search(r'"([^"]+)"', text)
    if match:
        return match.group(1)
    tokens = [token for token in re.findall(r"[a-z0-9_]+", lowered) if token not in {"response", "should", "contain", "a", "an", "non", "empty"}]
    return tokens[-1] if tokens else None


def _parse_save_response_field_assertion(text: Optional[str]) -> Optional[Tuple[str, str]]:
    if not text:
        return None
    match = re.search(r'save\s+response\s+field\s+"([^"]+)"\s+as\s+"([^"]+)"', text, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    match = re.search(r'save\s+response\s+field\s+([a-z0-9_]+)\s+as\s+"([^"]+)"', text, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return None


def _parse_response_equals_saved_assertion(text: Optional[str]) -> Optional[Tuple[str, str]]:
    if not text:
        return None
    match = re.search(r'response\s+"?([a-z0-9_]+)"?\s+should\s+be\s+equal\s+to\s+saved\s+value\s+"([^"]+)"', text, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2)
    return None


def _parse_response_list_assertion(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    match = re.search(r'list\s+of\s+"?([a-z0-9_]+)"?', text, re.IGNORECASE)
    if match and "response" in text.lower():
        return match.group(1)
    return None


def _story_detail_pairs(detail_lines: Optional[List[str]]) -> List[Tuple[str, str]]:
    if not detail_lines:
        return []
    pairs: List[Tuple[str, str]] = []
    pending_key: Optional[str] = None
    for raw in detail_lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|") if cell.strip()]
            if len(cells) == 2:
                key, value = cells
                pairs.append((key, value))
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key:
                pairs.append((key, value))
            continue
        if pending_key is None:
            pending_key = line.rstrip(":")
            continue
        pairs.append((pending_key, line))
        pending_key = None
    if pending_key is not None:
        pairs.append((pending_key, ""))
    return pairs


def _render_story_payload_literal(pairs: List[Tuple[str, str]], saved_alias_vars: Dict[str, str]) -> str:
    entries: List[str] = []
    for key, value in pairs:
        normalized_key = key.strip()
        if not normalized_key:
            continue
        alias_match = re.search(r'saved\s+(?:value|token)\s+"([^"]+)"', value, re.IGNORECASE)
        if alias_match:
            alias_name = alias_match.group(1)
            saved_var = saved_alias_vars.get(alias_name)
            if saved_var:
                entries.append(f"{json.dumps(normalized_key)}: {saved_var}")
                continue
        direct_alias = saved_alias_vars.get(value)
        if direct_alias:
            entries.append(f"{json.dumps(normalized_key)}: {direct_alias}")
            continue
        entries.append(f"{json.dumps(normalized_key)}: {json.dumps(value)}")
    return "{" + ", ".join(entries) + "}"


def _parse_saved_token_reference(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    patterns = [
        r'saved\s+token\s+"([^"]+)"',
        r'saved\s+token\s+([a-z0-9_]+)',
        r'using\s+saved\s+value\s+"([^"]+)"',
        r'using\s+saved\s+value\s+([a-z0-9_]+)',
        r'using\s+token\s+"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    if "saved token" in lowered:
        tokens = re.findall(r'[a-z0-9_]+', lowered)
        if tokens:
            return tokens[-1]
    return None

def _format_case_payload(
    case: Dict[str, Any],
    body_var: Optional[str],
    param_expr_map: Dict[str, str],
    *,
    body_literal: Optional[str] = None,
    query_literal: Optional[str] = None,
    headers_literal: Optional[str] = None,
) -> str:
    entries: List[str] = []
    for original in _extract_path_params(case.get("path") or ""):
        snake = _to_snake_case(original)
        expr = param_expr_map.get(snake)
        if expr is None:
            expr = repr(f"<{original}>")
        entries.append(f'"{original}": {expr}')
    if body_literal is not None:
        entries.append(f'"body": {body_literal}')
    elif body_var:
        entries.append(f'"body": {body_var}')
    default_query = case.get("default_query") or case.get("query") or {}
    if query_literal is not None:
        entries.append(f'"query": {query_literal}')
    elif default_query:
        query_entries: List[str] = []
        for key, value in default_query.items():
            coerced = _coerce_parameter_payload(key, value)
            sanitized = _sanitize_query_template_value(key, coerced)
            if isinstance(sanitized, dict) and not sanitized:
                continue

            expr: Optional[str] = None
            target_body = None
            if key.lower() == "status" and body_var in {"create_body", "update_body"}:
                target_body = body_var
            elif isinstance(sanitized, list) and len(sanitized) == 1 and isinstance(sanitized[0], str):
                token = sanitized[0].strip().lower()
                if token in {"available", "active", "enabled"} and body_var in {"create_body", "update_body"}:
                    target_body = body_var

            if target_body:
                fallback_value = _first_query_fallback(key, sanitized)
                fallback_literal = repr(fallback_value)
                if isinstance(sanitized, list) and sanitized:
                    if len(sanitized) > 1:
                        tail_literal = repr(sanitized[1:])
                        expr = f"[{target_body}.get({repr(key)}, {fallback_literal})] + {tail_literal}"
                    else:
                        expr = f"[{target_body}.get({repr(key)}, {fallback_literal})]"
                else:
                    expr = f"{target_body}.get({repr(key)}, {fallback_literal})"

            if expr is None:
                expr_value: Any = sanitized
                if isinstance(expr_value, set):
                    expr_value = sorted(expr_value)
                expr = repr(expr_value)

            query_entries.append(f'"{key}": {expr}')

        if query_entries:
            entries.append('"query": {' + ", ".join(query_entries) + '}')
    if headers_literal is not None:
        entries.append(f'"headers": {headers_literal}')
    default_headers = case.get("default_headers") or case.get("headers") or {}
    if default_headers:
        header_params: Dict[str, Any] = {}
        for key, value in default_headers.items():
            coerced = _coerce_parameter_payload(key, value)
            if isinstance(coerced, dict):
                continue
            header_params[key] = coerced
        if header_params:
            entries.append(f'"headers": {repr(header_params)}')
    if not entries:
        return "{}"
    if len(entries) == 1:
        return "{" + entries[0] + "}"
    return "{" + ", ".join(entries) + "}"


def _format_chain_snippet(details: Dict[str, Any]) -> str:
    requires_payload = bool(details.get("requires_payload"))
    test_data_method = details.get("test_data_method")
    alias = details.get("example_alias")
    alias_literal = json.dumps(alias) if alias is not None else None
    spec_id = details.get("spec_id")

    variable_name = "payload" if requires_payload else "params"
    lines: List[str] = []
    if spec_id is not None:
        lines.append(f"# Spec ID: {spec_id}")
    if test_data_method:
        if alias_literal is not None:
            lines.append(f"{variable_name} = test_data.{test_data_method}({alias_literal})")
        else:
            lines.append(f"{variable_name} = test_data.{test_data_method}()")
    else:
        entries: List[str] = []
        for param in _extract_path_params(details.get("path") or ""):
            entries.append(f'    # "{param}": "",')
        if requires_payload:
            entries.append('    # "body": {},')
        default_query = details.get("default_query") or details.get("query") or {}
        if default_query:
            entries.append(f'    # "query": {json.dumps(default_query, sort_keys=True)},')
        default_headers = details.get("default_headers") or details.get("headers") or {}
        if default_headers:
            entries.append(f'    # "headers": {json.dumps(default_headers, sort_keys=True)},')

        if entries:
            lines.append("# TODO: supply values")
            lines.append(f"{variable_name} = {{")
            lines.extend(entries)
            lines.append("}")
        else:
            lines.append(f"{variable_name} = {{}}  # TODO: supply values")
    lines.append("")

    call_expression = f"{details['class_name']}.{details['method_name']}({variable_name})"
    response_schema = details.get("response_schema") or {}
    expected_status = response_schema.get("expected_status") or details.get("expected_status") or 200
    chain_steps = [f".status_should_be({expected_status})"]

    for value in response_schema.get("body_contains", []) or []:
        chain_steps.append(f".body_should_contain({json.dumps(value)})")

    if chain_steps:
        snippet_lines = [f"{call_expression} \\"]
        for index, step in enumerate(chain_steps):
            trailer = " \\" if index < len(chain_steps) - 1 else ""
            snippet_lines.append(f"    {step}{trailer}")
        snippet_lines[-1] = snippet_lines[-1].rstrip(" \\")
    else:
        snippet_lines = [call_expression]

    return "\n".join(lines + snippet_lines)


def _group_cases(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        service_name = case["service"]
        entry = grouped.setdefault(
            service_name,
            {
                "service": service_name,
                "class_name": case["class_name"],
                "base_url": case.get("base_url"),
                "auth_schemes": case.get("auth_schemes") or {},
                "service_security": case.get("service_security") or [],
                "service_auth": case.get("service_auth") or {},
                "operations": [],
            },
        )
        if not entry.get("auth_schemes") and case.get("auth_schemes"):
            entry["auth_schemes"] = case.get("auth_schemes") or {}
        if not entry.get("service_security") and case.get("service_security"):
            entry["service_security"] = case.get("service_security") or []
        if not entry.get("service_auth") and case.get("service_auth"):
            entry["service_auth"] = case.get("service_auth") or {}
        entry["operations"].append(case)

    ordered: List[Dict[str, Any]] = []
    for service in sorted(grouped):
        entry = grouped[service]
        entry["operations"] = sorted(
            entry["operations"],
            key=lambda item: (item.get("operation") or "", item.get("method_name") or ""),
        )
        ordered.append(entry)
    return ordered


def _build_service_config_fixture(services: List[Dict[str, Any]]) -> str:
    lines: List[str] = [
        "@pytest.fixture(scope=\"module\", autouse=True)",
        "def _configure_service_clients():",
    ]

    for service in services:
        class_name = service.get("class_name") or "ApiService"
        env_prefix = _service_env_prefix(service.get("service") or class_name)
        lines.append(f'    base_url = os.getenv("{env_prefix}_BASE_URL")')
        lines.append("    if not base_url:")
        lines.append(f'        base_url = "offline://{env_prefix.lower()}"')
        lines.append("    credentials = {}")

        schemes = service.get("auth_schemes") or {}
        used_var_counts: Dict[str, int] = {}

        def _claim_var(raw: str) -> str:
            base = _to_snake_case(raw) or "auth"
            count = used_var_counts.get(base, 0)
            used_var_counts[base] = count + 1
            return base if count == 0 else f"{base}_{count + 1}"

        for scheme_name in sorted(schemes):
            scheme = schemes.get(scheme_name) or {}
            scheme_type = (scheme.get("type") or "").lower()
            env_base = f"{env_prefix}_{_service_env_prefix(scheme_name or scheme_type)}"
            var_base = _claim_var(f"{scheme_name}_{scheme_type}")

            if scheme_type == "apikey":
                value_var = f"{var_base}_key"
                lines.append(f'    {value_var} = os.getenv("{env_base}_KEY")')
                lines.append(f"    if {value_var}:")
                lines.append(f'        credentials["{scheme_name}"] = {value_var}')
                continue

            if scheme_type == "http":
                http_scheme = (scheme.get("scheme") or "").lower()
                if http_scheme == "basic":
                    user_var = f"{var_base}_username"
                    pass_var = f"{var_base}_password"
                    lines.append(f'    {user_var} = os.getenv("{env_base}_USERNAME")')
                    lines.append(f'    {pass_var} = os.getenv("{env_base}_PASSWORD")')
                    lines.append(f"    if {user_var} and {pass_var}:")
                    lines.append(f'        credentials["{scheme_name}"] = {{"username": {user_var}, "password": {pass_var}}}')
                    continue
                if http_scheme == "bearer":
                    token_var = f"{var_base}_token"
                    lines.append(f'    {token_var} = os.getenv("{env_base}_TOKEN")')
                    lines.append(f"    if {token_var}:")
                    lines.append(f'        credentials["{scheme_name}"] = {token_var}')
                    continue

            if scheme_type in {"oauth2", "openidconnect"}:
                token_var = f"{var_base}_token"
                lines.append(f'    {token_var} = os.getenv("{env_base}_TOKEN")')
                lines.append(f"    if {token_var}:")
                lines.append(f'        credentials["{scheme_name}"] = {token_var}')
                continue

            lines.append(f"    # TODO: Provide credentials for auth scheme '{scheme_name}' of type '{scheme_type}'.")

        lines.append(f"    {class_name}.configure(")
        lines.append("        base_url,")
        lines.append("        credentials=credentials or None,")
        lines.append("        offline_fallback=True,")
        lines.append("    )")
        lines.append("")

    lines.append("    yield")
    lines.append("")
    return "\n".join(lines)


def _describe_oauth_flows(scheme: Dict[str, Any]) -> List[str]:
    flows = scheme.get("flows")
    if not isinstance(flows, dict):
        legacy_flow = scheme.get("flow")
        scopes = scheme.get("scopes") if isinstance(scheme.get("scopes"), dict) else {}
        if legacy_flow:
            lines = [f"      flow: {legacy_flow}"]
            if scheme.get("authorizationUrl"):
                lines.append(f"      authorizationUrl: {scheme['authorizationUrl']}")
            if scheme.get("tokenUrl"):
                lines.append(f"      tokenUrl: {scheme['tokenUrl']}")
            if scopes:
                lines.append("      scopes:")
                for scope, description in scopes.items():
                    lines.append(f"        - {scope}: {description}")
            return lines
        return []

    lines: List[str] = []
    for flow_name, flow in flows.items():
        lines.append(f"      flow: {flow_name}")
        if isinstance(flow, dict):
            if flow.get("authorizationUrl"):
                lines.append(f"        authorizationUrl: {flow['authorizationUrl']}")
            if flow.get("tokenUrl"):
                lines.append(f"        tokenUrl: {flow['tokenUrl']}")
            if flow.get("refreshUrl"):
                lines.append(f"        refreshUrl: {flow['refreshUrl']}")
            scopes = flow.get("scopes")
            if isinstance(scopes, dict) and scopes:
                lines.append("        scopes:")
                for scope, description in scopes.items():
                    lines.append(f"          - {scope}: {description}")
    return lines


def _build_credentials_text_content(services: List[Dict[str, Any]]) -> str:
    lines: List[str] = []

    for service in services or []:
        service_name = service.get("service") or service.get("class_name") or "Service"
        if lines:
            lines.append("")
        lines.append(f"# {service_name}")

        schemes = service.get("auth_schemes") or {}
        for scheme_name in sorted(schemes):
            scheme = schemes.get(scheme_name) or {}
            scheme_type = (scheme.get("type") or "").lower()

            if scheme_type == "apikey":
                value = scheme.get("name") or ""
                lines.append(f"{scheme_name}={value}")
                continue

            if scheme_type in {"oauth2", "openidconnect"}:
                values: List[str] = []
                for item in _describe_oauth_flows(scheme):
                    stripped = item.strip()
                    if stripped.startswith("flow:"):
                        values.append(stripped.replace("flow: ", "flow=", 1))
                    elif stripped:
                        cleaned = stripped.replace(": ", "=") if ": " in stripped else stripped
                        values.append(cleaned)
                scopes = scheme.get("scopes")
                if isinstance(scopes, dict):
                    scope_literal = ",".join(sorted(scopes.keys()))
                    if scope_literal:
                        values.append(f"scopes={scope_literal}")
                if not values and scheme.get("authorizationUrl"):
                    values.append(f"authorizationUrl={scheme['authorizationUrl']}")
                if not values and scheme.get("tokenUrl"):
                    values.append(f"tokenUrl={scheme['tokenUrl']}")
                lines.append(f"{scheme_name}={' ; '.join(values)}")

    if not lines:
        return "# No API key or OAuth details found\n"

    return "\n".join(lines) + "\n"


def _build_auth_metadata(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for service in services:
        env_prefix = _service_env_prefix(service.get("service") or service.get("class_name"))
        entry: Dict[str, Any] = {
            "service": service.get("service"),
            "class_name": service.get("class_name"),
            "env_prefix": env_prefix,
            "auth_schemes": copy.deepcopy(service.get("auth_schemes") or {}),
            "default_security": copy.deepcopy(service.get("service_security") or []),
            "operations": [],
        }

        for operation in service.get("operations") or []:
            raw_security = operation.get("operation_security")
            if raw_security is None:
                effective_security = copy.deepcopy(entry["default_security"])
            else:
                effective_security = copy.deepcopy(raw_security)

            scheme_names: Set[str] = set()
            if isinstance(effective_security, list):
                for requirement in effective_security:
                    if isinstance(requirement, dict):
                        for scheme_name in requirement.keys():
                            if scheme_name:
                                scheme_names.add(str(scheme_name))

            entry["operations"].append(
                {
                    "name": operation.get("operation") or operation.get("method_name"),
                    "method": operation.get("http_method"),
                    "path": operation.get("path"),
                    "requires_auth": bool(effective_security),
                    "security": effective_security,
                    "schemes": sorted(scheme_names),
                }
            )

        metadata[env_prefix] = entry

    return metadata


def _build_operation_method_lines(operation: Dict[str, Any]) -> List[str]:
    method_lines: List[str] = []
    method_lines.append("")
    method_lines.append("    @classmethod")
    arg_name = "payload" if operation.get("requires_payload") else "params"
    method_lines.append(f"    def {operation['method_name']}(cls, {arg_name}: Dict[str, Any]) -> ApiResponse:")
    method_lines.append(f"        data = dict({arg_name} or {{}})")

    path_pairs = [(param, _to_snake_case(param)) for param in _extract_path_params(operation.get("path") or "")]
    for original, var_name in path_pairs:
        method_lines.append(f'        {var_name} = cls._require(data, "{original}")')

    method_lines.append('        query_raw = cls._optional_dict(data.get("query"))')
    method_lines.append('        if query_raw is None:')
    method_lines.append('            query = None')
    method_lines.append('        else:')
    method_lines.append('            query = dict(query_raw)')
    method_lines.append('            status_ref = data.get("status")')
    method_lines.append('            if status_ref is None:')
    method_lines.append('                body_candidate = data.get("body")')
    method_lines.append('                if isinstance(body_candidate, dict):')
    method_lines.append('                    status_ref = body_candidate.get("status")')
    method_lines.append('            if status_ref is not None and cls._is_placeholder_status(query.get("status")):')
    method_lines.append('                if isinstance(query.get("status"), (list, tuple, set)):')
    method_lines.append('                    query["status"] = [status_ref]')
    method_lines.append('                else:')
    method_lines.append('                    query["status"] = status_ref')
    method_lines.append('        headers = cls._optional_dict(data.get("headers"))')

    if operation.get("requires_payload"):
        method_lines.append("        body = cls._extract_body(data) or {}")

    path_template = operation.get("path") or ""
    use_path_var = False
    if not path_pairs:
        parts = [part for part in path_template.split("/") if part]
        if len(parts) >= 2 and _looks_like_example_id(parts[-1]):
            resource_name = parts[-2]
            path_base = "/" + "/".join(parts[:-1])
            method_lines.append(f'        resource_id = cls._extract_id_from_data(data, "{resource_name}")')
            if operation.get("requires_payload"):
                method_lines.append("        if resource_id is None:")
                method_lines.append(f'            resource_id = cls._extract_id_from_data(body, "{resource_name}")')
            else:
                method_lines.append("        if resource_id is None:")
                method_lines.append("            body_candidate = cls._extract_body(data) or {}")
                method_lines.append(f'            resource_id = cls._extract_id_from_data(body_candidate, "{resource_name}")')
            method_lines.append("        if resource_id is None and isinstance(query, dict):")
            method_lines.append(f'            resource_id = cls._extract_id_from_data(query, "{resource_name}")')
            method_lines.append(
                f'        path = f"{path_base}/{{resource_id}}" if resource_id not in (None, "") else "{path_base}"'
            )
            use_path_var = True

    for original, var_name in path_pairs:
        path_template = path_template.replace("{" + original + "}", f"{{{var_name}}}")

    http_method = (operation.get("http_method") or "GET").upper()
    security_literal = repr(operation.get("operation_security"))
    method_lines.append("        return cls._request(")
    method_lines.append(f'            "{http_method}",')
    if use_path_var:
        method_lines.append("            path,")
    else:
        method_lines.append(f'            f"{path_template}",')
    method_lines.append("            query=query,")
    if operation.get("requires_payload"):
        method_lines.append("            body=body,")
    method_lines.append("            headers=headers,")
    method_lines.append(f"            security={security_literal},")
    method_lines.append("        )")
    return method_lines


def _build_method_alias_lines(operations: List[Dict[str, Any]]) -> List[str]:
    alias_lines: List[str] = []
    existing_methods: Set[str] = {
        operation.get("method_name")
        for operation in operations
        if operation.get("method_name")
    }
    seen_aliases: Set[str] = set()

    for operation in operations:
        method_name = operation.get("method_name")
        if not method_name:
            continue

        alias_candidates: List[str] = []
        raw_operation = operation.get("operation")
        if isinstance(raw_operation, str):
            alias_candidates.append(raw_operation.strip())
        alias_candidates.append(method_name.lower())

        arg_name = "payload" if operation.get("requires_payload") else "params"
        path_params = _extract_path_params(operation.get("path") or "")

        for candidate in alias_candidates:
            alias_name = (candidate or "").strip()
            if not alias_name:
                continue
            if alias_name == method_name:
                continue
            if alias_name in existing_methods or alias_name in seen_aliases:
                continue
            alias_identifier = alias_name.replace(" ", "_")
            if not alias_identifier.isidentifier() or keyword.iskeyword(alias_identifier):
                continue
            alias_lines.append("")
            alias_lines.append("    @classmethod")
            alias_lines.append(f"    def {alias_identifier}(cls, {arg_name}: Dict[str, Any]) -> ApiResponse:")
            alias_lines.append(f"        data = dict({arg_name} or {{}})")
            for original in path_params:
                alias_lines.append(f'        cls._require(data, "{original}")')
            alias_lines.append(f"        return cls.{method_name}(data)")
            seen_aliases.add(alias_identifier)

    return alias_lines


def _build_service_class_lines(service: Dict[str, Any]) -> List[str]:
    class_name = service.get("class_name") or "ApiService"
    env_prefix = _service_env_prefix(service.get("service") or class_name)
    auth_literal = repr(service.get("auth_schemes") or {})
    default_security_literal = repr(service.get("service_security") or [])
    lines: List[str] = ["", "", f"class {class_name}:"]
    lines.append("    _client: Optional[Any] = None")
    lines.append("    _offline_client: Optional[Any] = None")
    lines.append("    _offline_fallback_enabled: bool = False")
    lines.append(f"    _AUTH_SCHEMES: Dict[str, Any] = {auth_literal}")
    lines.append(f"    _DEFAULT_SECURITY: List[Dict[str, Any]] = {default_security_literal}")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def configure(")
    lines.append("        cls,")
    lines.append("        base_url: Optional[str],")
    lines.append("        *,")
    lines.append("        credentials: Optional[Dict[str, Any]] = None,")
    lines.append("        default_security: Optional[List[Dict[str, Any]]] = None,")
    lines.append("        timeout: int = 30,")
    lines.append("        offline_fallback: bool = True,")
    lines.append("    ) -> None:")
    lines.append("        security = default_security if default_security is not None else cls._DEFAULT_SECURITY")
    lines.append("        cls._offline_fallback_enabled = offline_fallback")
    lines.append("        cls._offline_client = cls._build_offline_client() if offline_fallback else None")
    lines.append("        normalized = (base_url or \"\").strip()")
    lines.append("        if not normalized and offline_fallback:")
    lines.append("            cls._client = cls._offline_client or cls._build_offline_client()")
    lines.append("            return")
    lines.append("        lowered = normalized.lower()")
    lines.append("        if lowered.startswith(\"offline://\") or lowered.startswith(\"stub://\"):")
    lines.append("            if offline_fallback:")
    lines.append("                cls._client = cls._offline_client or cls._build_offline_client()")
    lines.append("                return")
    lines.append("        if not normalized:")
    lines.append("            raise ValueError(\"Base URL is required when offline fallback is disabled.\")")
    lines.append("        cls._client = ServiceClient(")
    lines.append("            normalized,")
    lines.append("            auth_schemes=dict(cls._AUTH_SCHEMES),")
    lines.append("            default_security=list(security) if security is not None else [],")
    lines.append("            credentials=credentials,")
    lines.append("            timeout=timeout,")
    lines.append("        )")
    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def _client_or_fail(cls) -> Any:")
    lines.append("        if cls._client is None:")
    lines.append("            offline_client = cls._get_offline_client()")
    lines.append("            cls._client = offline_client or cls._build_offline_client()")
    lines.append("        return cls._client")
    lines.append("")
    lines.append("    @staticmethod")
    lines.append("    def _require(mapping: Dict[str, Any], key: str) -> Any:")
    lines.append('        if key not in mapping or mapping[key] in (None, ""):')
    lines.append('            raise ValueError(f"{key} is required")')
    lines.append("        return mapping[key]")
    lines.append("")
    lines.append("    @staticmethod")
    lines.append("    def _optional_dict(candidate: Any) -> Optional[Dict[str, Any]]:")
    lines.append("        return candidate if isinstance(candidate, dict) else None")
    lines.append("")
    lines.append("    @staticmethod")
    lines.append("    def _is_placeholder_status(candidate: Any) -> bool:")
    lines.append('        if candidate in (None, ""):')
    lines.append('            return True')
    lines.append("        if isinstance(candidate, str):")
    lines.append('            return candidate.strip().lower() in {"value", "any", "*", ""}')
    lines.append("        if isinstance(candidate, (list, tuple, set)):")
    lines.append(f"            return all({class_name}._is_placeholder_status(item) for item in candidate)")
    lines.append("        return False")
    lines.append("")
    lines.append("    @staticmethod")
    lines.append("    def _extract_body(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:")
    lines.append('        for key in ("body", "json", "payload"):')
    lines.append("            candidate = data.get(key)")
    lines.append("            if candidate is not None:")
    lines.append("                return candidate")
    lines.append("        return None")
    lines.append("")
    lines.append("    @staticmethod")
    lines.append("    def _extract_id_from_data(data: Dict[str, Any], resource: Optional[str]) -> Optional[Any]:")
    lines.append("        if not isinstance(data, dict):")
    lines.append("            return None")
    lines.append("        if \"id\" in data and data.get(\"id\") not in (None, \"\"):")
    lines.append("            return data.get(\"id\")")
    lines.append("        resource_token = (resource or \"\").strip().lower()")
    lines.append("        if resource_token:")
    lines.append("            for key in (f\"{resource_token}Id\", f\"{resource_token}_id\"):")
    lines.append("                if key in data and data.get(key) not in (None, \"\"):")
    lines.append("                    return data.get(key)")
    lines.append("        for key, value in data.items():")
    lines.append("            if str(key).lower().endswith(\"id\") and value not in (None, \"\"):")
    lines.append("                return value")
    lines.append("        return None")

    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def _build_offline_client(cls) -> _OfflineServiceClient:")
    lines.append("        return _OfflineServiceClient()")

    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def _get_offline_client(cls) -> Optional[Any]:")
    lines.append("        if not cls._offline_fallback_enabled:")
    lines.append("            return None")
    lines.append("        if cls._offline_client is None:")
    lines.append("            cls._offline_client = cls._build_offline_client()")
    lines.append("        return cls._offline_client")

    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def _should_use_offline_status(cls, status_code: int) -> bool:")
    lines.append("        if not cls._offline_fallback_enabled:")
    lines.append("            return False")
    lines.append("        return status_code in {401, 403, 404} or status_code >= 500")

    lines.append("")
    lines.append("    @classmethod")
    lines.append("    def _request(")
    lines.append("        cls,")
    lines.append("        method: str,")
    lines.append("        path: str,")
    lines.append("        *,")
    lines.append("        query: Optional[Dict[str, Any]] = None,")
    lines.append("        body: Optional[Dict[str, Any]] = None,")
    lines.append("        headers: Optional[Dict[str, Any]] = None,")
    lines.append("        security: Optional[List[Dict[str, Any]]] = None,")
    lines.append("        credential_overrides: Optional[Dict[str, Any]] = None,")
    lines.append("    ) -> ApiResponse:")
    lines.append("        client = cls._client_or_fail()")
    lines.append("        try:")
    lines.append("            response = client.request(")
    lines.append("                method,")
    lines.append("                path,")
    lines.append("                query=query,")
    lines.append("                body=body,")
    lines.append("                headers=headers,")
    lines.append("                security=security,")
    lines.append("                credential_overrides=credential_overrides,")
    lines.append("            )")
    lines.append("        except (requests.exceptions.RequestException, ApiAuthError):")
    lines.append("            offline_client = cls._get_offline_client()")
    lines.append("            if offline_client is not None and offline_client is not client:")
    lines.append("                return offline_client.request(")
    lines.append("                    method,")
    lines.append("                    path,")
    lines.append("                    query=query,")
    lines.append("                    body=body,")
    lines.append("                    headers=headers,")
    lines.append("                    security=security,")
    lines.append("                    credential_overrides=credential_overrides,")
    lines.append("                )")
    lines.append("            raise")
    lines.append("        if cls._should_use_offline_status(response.status_code):")
    lines.append("            offline_client = cls._get_offline_client()")
    lines.append("            if offline_client is not None and offline_client is not client:")
    lines.append("                return offline_client.request(")
    lines.append("                    method,")
    lines.append("                    path,")
    lines.append("                    query=query,")
    lines.append("                    body=body,")
    lines.append("                    headers=headers,")
    lines.append("                    security=security,")
    lines.append("                    credential_overrides=credential_overrides,")
    lines.append("                )")
    lines.append("        return response")

    operations = service.get("operations", [])
    for operation in operations:
        lines.extend(_build_operation_method_lines(operation))

    alias_lines = _build_method_alias_lines(operations)
    if alias_lines:
        lines.extend(alias_lines)

    lines.append("")
    lines.append(f'if "{env_prefix}_BASE_URL" not in os.environ:')
    lines.append(f'    os.environ["{env_prefix}_BASE_URL"] = "offline://{env_prefix.lower()}"')

    return lines


def _story_lines(story: Optional[str]) -> List[str]:
    lines: List[str] = []
    for raw in (story or "").splitlines():
        text = raw.strip()
        if text:
            lines.append(text)
    return lines


def _extract_story_scalar_after_keyword(lines: List[str], keyword: str) -> Optional[str]:
    lowered_keyword = keyword.lower()
    for index, line in enumerate(lines):
        lower_line = line.lower()
        if lowered_keyword not in lower_line:
            continue
        if ":" in line:
            candidate = line.split(":", 1)[1].strip().strip('"').strip("'")
            if candidate:
                return candidate
        for follow in lines[index + 1 :]:
            candidate = follow.strip().strip('"').strip("'")
            if not candidate:
                continue
            if lowered_keyword in candidate.lower():
                continue
            return candidate
    return None


def _extract_story_email(lines: List[str]) -> Optional[str]:
    email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for line in lines:
        match = email_pattern.search(line)
        if match:
            return match.group(0)
    return None


def _extract_story_password(lines: List[str]) -> Optional[str]:
    return _extract_story_scalar_after_keyword(lines, "password")


def _extract_story_page_number(lines: List[str]) -> int:
    page_literal = _extract_story_scalar_after_keyword(lines, "page")
    if page_literal:
        match = re.search(r"\b(\d{1,3})\b", page_literal)
        if match:
            return int(match.group(1))
    for line in lines:
        match = re.search(r"page[^0-9]*(\d{1,3})", line, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 1


def _derive_case_email(base_email: str, suffix: str) -> str:
    if "@" in base_email:
        local_part, _, domain = base_email.partition("@")
        normalized_local = _compact_token(local_part) or "user"
        normalized_suffix = _compact_token(suffix) or "case"
        return f"{normalized_local}.{normalized_suffix}@{domain}"
    normalized_suffix = _compact_token(suffix) or "case"
    return f"{normalized_suffix}@example.com"


def _collect_service_classes(services: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for service in services:
        name = service.get("class_name") or "ApiService"
        if name not in seen:
            seen.append(name)
    return seen


def _format_test_case(details: Dict[str, Any], story: Optional[str]) -> str:
    requires_payload = bool(details.get("requires_payload"))
    test_data_method = details.get("test_data_method")
    alias = details.get("example_alias")
    alias_literal = json.dumps(alias) if alias is not None else None
    spec_id = details.get("spec_id")

    variable_name = "payload" if requires_payload else "params"
    identifier = _normalize_identifier(f"{details.get('service', '')}_{details.get('method_name', '')}") or "case"
    test_name = f"test_{identifier}"

    lines: List[str] = [f"def {test_name}():"]

    story_line_items = _story_lines(story or details.get("story"))
    if story_line_items:
        lines.append('    """')
        for text in story_line_items:
            lines.append(f"    {text}")
        lines.append('    """')
    if spec_id is not None:
        lines.append(f"    # Spec ID: {spec_id}")

    if test_data_method:
        lines.append("    if test_data is None:")
        lines.append("        raise RuntimeError(\"Populate tests.test_data before running these tests.\")")
        if alias_literal is not None:
            lines.append(f"    {variable_name} = test_data.{test_data_method}({alias_literal})")
        else:
            lines.append(f"    {variable_name} = test_data.{test_data_method}()")
    else:
        entries: List[str] = []
        for param in _extract_path_params(details.get("path") or ""):
            entries.append(f'        # "{param}": "",')
        if requires_payload:
            entries.append('        # "body": {},')
        default_query = details.get("default_query") or details.get("query") or {}
        if default_query:
            entries.append(f'        # "query": {json.dumps(default_query, sort_keys=True)},')
        default_headers = details.get("default_headers") or details.get("headers") or {}
        if default_headers:
            entries.append(f'        # "headers": {json.dumps(default_headers, sort_keys=True)},')

        if entries:
            lines.append("    # TODO: supply request values")
            lines.append(f"    {variable_name} = {{")
            lines.extend(entries)
            lines.append("    }")
        else:
            lines.append(f"    {variable_name} = {{}}  # TODO: supply values")

    call_expression = f"{details['class_name']}.{details['method_name']}({variable_name})"
    lines.append(f"    response = {call_expression}")

    response_schema = details.get("response_schema") or {}
    expected_status = response_schema.get("expected_status") or details.get("expected_status") or 200
    lines.append(f"    response.status_should_be({expected_status})")

    for value in response_schema.get("body_contains", []) or []:
        lines.append(f"    response.body_should_contain({json.dumps(value)})")

    lines.append("")
    return "\n".join(lines)


def _build_story_test_case(cases: List[Dict[str, Any]], story: str, _services: List[Dict[str, Any]]) -> str:
    doc_lines = _story_lines(story)
    story_blocks = _partition_story_steps(doc_lines)
    explicit_payload_blocks = [block for block in story_blocks if block and block.get("details")]
    scenario_line = next((line for line in doc_lines if line.lower().startswith("scenario")), "")
    scenario_name = scenario_line.split(":", 1)[-1].strip() if scenario_line else "story_flow"
    identifier = _normalize_identifier(scenario_name) or "story_flow"
    test_name = f"test_{identifier}"

    start_message = json.dumps(f"starting {scenario_name or identifier} api test")
    lines: List[str] = [f"def {test_name}():"]
    lines.append(f"    print({start_message})")
    if doc_lines:
        lines.append('    """')
        for text in doc_lines:
            lines.append(f"    {text}")
        lines.append('    """')

    service_operations: Dict[str, List[Dict[str, Any]]] = {}
    for service in _services or []:
        class_name = service.get("class_name")
        if not class_name:
            continue
        ops = service_operations.setdefault(class_name, [])
        ops.extend(service.get("operations", []))

    param_meta: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        operation_label = case.get("operation") or case.get("method_name") or "operation"
        for original in _extract_path_params(case.get("path") or ""):
            var_name = _to_snake_case(original)
            if var_name in param_meta:
                continue
            inferred = _infer_story_param_value(doc_lines, original, operation_label)
            param_meta[var_name] = {"original": original, "value": inferred}

    param_expr_map: Dict[str, str] = {}
    param_originals: Dict[str, str] = {}
    for var_name, meta in param_meta.items():
        value = meta.get("value")
        if value is None:
            value = _fallback_param_value(meta.get("original"))
            meta["value"] = value
        lines.append(f"    {var_name} = {repr(value)}")
        param_expr_map[var_name] = var_name
        original = meta.get("original")
        if isinstance(original, str):
            param_originals[original] = var_name
            param_expr_map[_to_snake_case(original)] = var_name
            param_expr_map[original.lower()] = var_name
            param_expr_map[original] = var_name

    saved_alias_vars: Dict[str, str] = {}
    saved_name_counts: Dict[str, int] = {}

    primary_id_expr = None
    candidate_keys = ["id", "resource_id", "record_id"]
    candidate_keys.extend(sorted(param_expr_map.keys()))
    for key in candidate_keys:
        expr = param_expr_map.get(key)
        if expr:
            primary_id_expr = expr
            break

    create_name_var: Optional[str] = None
    update_name_var: Optional[str] = None

    payload_cases = [case for case in cases if case.get("requires_payload")]
    story_mentions_update = any("update" in (line or "").lower() for line in doc_lines)
    needs_update_body = any((case.get("http_method") or "").upper() in {"PUT", "PATCH"} for case in cases)
    if story_mentions_update:
        needs_update_body = True
    if payload_cases and not explicit_payload_blocks:
        create_case = _select_best_create_case(payload_cases) or payload_cases[0]
        create_example = _resolve_case_example(create_case)
        if create_example is None:
            create_example = _build_case_payload_template(create_case)

        if isinstance(create_example, (dict, list)):
            lines.append(f"    create_body = {repr(create_example)}")
        else:
            lines.append("    create_body = {}  # TODO: Supply request payload")
            create_example = {}

        if isinstance(create_example, dict):
            id_expr_candidates: List[str] = []
            for meta in param_meta.values():
                original = meta.get("original")
                snake = _to_snake_case(original)
                expr = param_expr_map.get(snake)
                if not expr:
                    continue
                candidate_keys: List[Optional[str]] = [original, snake]
                if isinstance(original, str):
                    candidate_keys.append(original.lower())
                if snake.endswith("_id"):
                    candidate_keys.append("id")
                for key in candidate_keys:
                    if key and key in create_example:
                        lines.append(f"    create_body[{repr(key)}] = {expr}")
                        break
                if isinstance(original, str) and original.lower().endswith("id"):
                    id_expr_candidates.append(expr)

            if id_expr_candidates and "id" in create_example:
                lines.append(f"    create_body['id'] = {id_expr_candidates[0]}")

            name_tokens = [key for key in ("name", "title") if key in create_example]
            if name_tokens:
                default_label = identifier.replace("test_", "").replace("_", " ").strip() or "Generated Item"
                create_label = _find_story_literal(doc_lines, {"create", "name"}) or _find_story_literal(doc_lines, {"name"}) or default_label
                lines.append(f"    create_name = {repr(create_label)}")
                create_name_var = "create_name"
                for token in name_tokens:
                    lines.append(f"    create_body[{repr(token)}] = create_name")

            if "status" in create_example:
                status_value = _find_story_literal(doc_lines, {"status"}) or create_example.get("status", "available")
                lines.append(f"    create_body['status'] = {repr(status_value)}")

            if needs_update_body:
                lines.append("    update_body = dict(create_body)")
                update_label = _find_story_literal(doc_lines, {"update", "name"}) or f"{create_label} Updated"
                lines.append(f"    update_name = {repr(update_label)}")
                update_name_var = "update_name"
                if name_tokens:
                    for token in name_tokens:
                        lines.append(f"    update_body[{repr(token)}] = update_name")
                else:
                    lines.append("    update_body['name'] = update_name")
                if "status" in create_example:
                    lines.append("    update_body['status'] = 'pending'")
        elif needs_update_body:
            lines.append("    update_body = create_body")

    lines.append("")

    delete_case = next((case for case in cases if (case.get("http_method") or "").upper() == "DELETE"), None)

    given_lines = [line for line in doc_lines if line.lower().startswith("given ")]
    for given_line in given_lines:
        lower_given = given_line.lower()
        lines.append(f"    # Given: {given_line.strip()}")

        expected_status = _parse_status_assertion(given_line)
        if expected_status is None:
            expected_status = 404 if "does not exist" in lower_given else 200

        matched_case = _match_story_line_to_case(given_line, cases)
        related_class = None
        if matched_case:
            related_class = matched_case.get("class_name")
        elif cases:
            related_class = cases[0].get("class_name")

        if expected_status == 404 and related_class:
            candidates = [
                op
                for op in service_operations.get(related_class, [])
                if (op.get("http_method") or "").upper() in {"GET", "HEAD"}
            ]
            fallback_case = _match_story_line_to_case(given_line, candidates) if candidates else None
            if fallback_case:
                matched_case = fallback_case

        if not matched_case:
            lines.append("    # TODO: Unable to automatically map Given step to an operation")
            lines.append("")
            continue

        if "does not exist" in lower_given and delete_case is not None:
            cleanup_payload = _format_case_payload(delete_case, None, param_expr_map)
            lines.append(f"    cleanup_response = {delete_case['class_name']}.{delete_case['method_name']}({cleanup_payload})")
            lines.append("    assert cleanup_response.status_code in {200, 404}")

        body_var = None
        if matched_case.get("requires_payload"):
            method_upper = (matched_case.get("http_method") or "").upper()
            if method_upper == "POST":
                body_var = "create_body"
            elif method_upper in {"PUT", "PATCH"}:
                body_var = "update_body" if needs_update_body else "create_body"

        precondition_payload = _format_case_payload(matched_case, body_var, param_expr_map)
        lines.append(f"    precondition_response = {matched_case['class_name']}.{matched_case['method_name']}({precondition_payload})")
        lines.append(f"    assert precondition_response.status_code == {expected_status}")
        if expected_status == 404:
            lines.append("    if isinstance(precondition_response.body, dict):")
            lines.append("        message = str(precondition_response.body.get(\"message\", \"\"))")
            lines.append("        assert \"not found\" in message.lower()")
            lines.append("    else:")
            lines.append("        assert \"not found\" in str(precondition_response.body).lower()")
        lines.append("")

    remaining_cases = list(cases)
    block_case_pairs: List[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = []

    all_operation_candidates: List[Dict[str, Any]] = []
    for ops in service_operations.values():
        all_operation_candidates.extend(ops)

    primary_class = cases[0].get("class_name") if cases else None

    for block in story_blocks:
        when_line = block.get("when") if block else ""
        candidate = _match_story_line_to_case(when_line or "", remaining_cases) if when_line else None

        normalized_when = (when_line or "").lower()
        mentions_id = " id " in f" {normalized_when} " or "with id" in normalized_when or re.search(r"\b\d+\b", normalized_when)
        mentions_retrieve = any(keyword in normalized_when for keyword in ["retrieve", "retrieves", "fetch", "fetches", " get "])
        if mentions_retrieve and mentions_id and remaining_cases:
            prioritized_candidates = [
                case
                for case in remaining_cases
                if (case.get("http_method") or "").upper() in {"GET", "HEAD"}
                and any(
                    (param or "").lower().endswith("id")
                    for param in _extract_path_params(case.get("path") or "")
                )
            ]
            if prioritized_candidates:
                best_match = _match_story_line_to_case(when_line or "", prioritized_candidates)
                candidate = best_match or candidate or prioritized_candidates[0]

        if _line_mentions_create(when_line):
            scoped_candidates = [case for case in remaining_cases if (not primary_class) or case.get("class_name") == primary_class]
            create_candidate = _select_best_create_case(scoped_candidates)
            if not create_candidate:
                scoped_all = [case for case in all_operation_candidates if (not primary_class) or case.get("class_name") == primary_class]
                create_candidate = _select_best_create_case(scoped_all)
            if create_candidate:
                candidate = create_candidate

        if when_line:
            login_tokens = [" login", " log in", " logs in", " logging in", "signin", "sign in", " signs in"]
            if any(token in normalized_when for token in login_tokens):
                def _is_login_case(case: Optional[Dict[str, Any]]) -> bool:
                    if not case:
                        return False
                    operation_label = (case.get("operation") or case.get("method_name") or "").lower()
                    path_literal = (case.get("path") or "").lower()
                    return (
                        any(fragment in operation_label for fragment in ["login", "log_in", "logging_in", "sign_in", "signin"])
                        or "/login" in path_literal
                    )

                login_remaining = [case for case in remaining_cases if _is_login_case(case)]
                login_candidates = login_remaining or [case for case in all_operation_candidates if _is_login_case(case)]
                if login_candidates:
                    best_login = _match_story_line_to_case(when_line, login_candidates)
                    candidate = best_login or candidate or login_candidates[0]

        if candidate is None and when_line:
            scoped_all = [case for case in all_operation_candidates if (not primary_class) or case.get("class_name") == primary_class]
            candidate = _match_story_line_to_case(when_line, scoped_all)

        if candidate is None and remaining_cases:
            for fallback_case in remaining_cases:
                if not _is_form_encoded_case(fallback_case):
                    candidate = fallback_case
                    break
            if candidate is None:
                candidate = remaining_cases[0]

        if candidate in remaining_cases:
            remaining_cases.remove(candidate)
        
        block_case_pairs.append((block, candidate))

    for leftover_case in remaining_cases:
        if doc_lines:
            break
        if _is_form_encoded_case(leftover_case):
            continue
        block_case_pairs.append((None, leftover_case))

    delete_occurred = False
    deleted_param_exprs: Set[str] = set()
    step_index = 0
    header_alias_vars: Dict[str, str] = {}
    used_header_names: Set[str] = set()
    for block, chosen_case in block_case_pairs:
        if not chosen_case:
            continue

        operation_label = chosen_case.get("operation") or chosen_case.get("method_name") or f"step_{step_index}"
        response_schema = chosen_case.get("response_schema") or {}
        expected_status = response_schema.get("expected_status") or chosen_case.get("expected_status") or 200
        body_assertions: List[str] = list(response_schema.get("body_contains") or [])
        status_locked = False
        save_fields: List[Tuple[str, str]] = []
        non_empty_fields: List[str] = []
        equal_saved_fields: List[Tuple[str, str]] = []
        list_field_assertions: List[str] = []

        if block:
            for assertion in block.get("assertions", []):
                handled = False
                forced_status = _parse_status_assertion(assertion)
                if forced_status is not None:
                    expected_status = forced_status
                    status_locked = True
                    handled = True
                if not handled:
                    non_empty = _parse_non_empty_field_assertion(assertion)
                    if non_empty:
                        non_empty_fields.append(non_empty)
                        handled = True
                if not handled:
                    save_field = _parse_save_response_field_assertion(assertion)
                    if save_field:
                        save_fields.append(save_field)
                        handled = True
                if not handled:
                    equal_saved = _parse_response_equals_saved_assertion(assertion)
                    if equal_saved:
                        equal_saved_fields.append(equal_saved)
                        handled = True
                if not handled:
                    list_field = _parse_response_list_assertion(assertion)
                    if list_field:
                        list_field_assertions.append(list_field)
                        handled = True
                if not handled:
                    fragment = _parse_body_contains_assertion(assertion)
                    if fragment:
                        body_assertions.append(fragment)
                        handled = True
                if not handled:
                    lines.append(f"    # TODO: {assertion}")

        method_upper = (chosen_case.get("http_method") or "").upper()
        body_var = None
        if chosen_case.get("requires_payload"):
            if method_upper == "POST":
                body_var = "create_body"
            elif method_upper in {"PUT", "PATCH"}:
                body_var = "update_body" if needs_update_body else "create_body"

        detail_pairs = _story_detail_pairs(block.get("details") if block else None)
        body_literal_override: Optional[str] = None
        query_literal_override: Optional[str] = None
        headers_literal_override: Optional[str] = None
        if detail_pairs:
            payload_literal_text = _render_story_payload_literal(detail_pairs, saved_alias_vars)
            if method_upper in {"GET", "DELETE"}:
                query_literal_override = payload_literal_text
            else:
                body_literal_override = payload_literal_text
                body_var = None

        token_alias_name = _parse_saved_token_reference(block.get("when") if block else None)
        if not token_alias_name and block:
            for assertion in block.get("assertions", []):
                token_alias_name = _parse_saved_token_reference(assertion)
                if token_alias_name:
                    break
        if not token_alias_name and block:
            for detail_key, detail_value in detail_pairs:
                token_alias_name = _parse_saved_token_reference(detail_key) or _parse_saved_token_reference(detail_value)
                if token_alias_name:
                    break
        if token_alias_name:
            saved_var = saved_alias_vars.get(token_alias_name)
            if saved_var:
                header_var = header_alias_vars.get(token_alias_name)
                if not header_var:
                    base_name = f"{saved_var}_headers"
                    sanitized = _to_snake_case(base_name) or "headers"
                    candidate = sanitized
                    index = 1
                    while candidate in used_header_names or candidate in saved_alias_vars.values():
                        index += 1
                        candidate = f"{sanitized}_{index}"
                    used_header_names.add(candidate)
                    header_var = candidate
                    header_alias_vars[token_alias_name] = header_var
                    header_line = '    {header_var} = {{"Authorization": f"Bearer {{{saved_var}}}"}}'.format(
                        header_var=header_var,
                        saved_var=saved_var,
                    )
                    lines.append(header_line)
                headers_literal_override = header_var
            else:
                lines.append(f"    # TODO: Saved token '{token_alias_name}' referenced before assignment")

        path_param_exprs = [
            param_expr_map.get(_to_snake_case(param))
            or param_expr_map.get(param.lower())
            or param_expr_map.get(param)
            for param in _extract_path_params(chosen_case.get("path") or "")
        ]
        path_param_exprs = [expr for expr in path_param_exprs if expr]

        is_collection_get = method_upper == "GET" and any(
            token in (operation_label or "").lower() for token in ("find", "list", "search")
        )
        if is_collection_get and expected_status == 404:
            expected_status = 200

        references_deleted_resource = bool(
            delete_occurred and path_param_exprs and deleted_param_exprs.intersection(path_param_exprs)
        )
        if (
            method_upper == "GET"
            and not is_collection_get
            and references_deleted_resource
            and not status_locked
        ):
            expected_status = 404

        payload_literal = _format_case_payload(
            chosen_case,
            body_var,
            param_expr_map,
            body_literal=body_literal_override,
            query_literal=query_literal_override,
            headers_literal=headers_literal_override,
        )
        response_var = _to_snake_case(f"{chosen_case.get('method_name', f'step_{step_index}')}_response") or f"response_{step_index}"

        lines.append(f"    # Step: {operation_label}")
        lines.append(f"    {response_var} = {chosen_case['class_name']}.{chosen_case['method_name']}({payload_literal})")
        lines.append(f"    {response_var}.status_should_be({expected_status})")

        dict_name_var: Optional[str] = None
        body_alias_name = f"{response_var}_body"
        body_alias_declared = False
        if body_var == "create_body" and create_name_var:
            dict_name_var = create_name_var
        elif body_var == "update_body" and update_name_var:
            dict_name_var = update_name_var

        if dict_name_var and expected_status == 200:
            if not body_alias_declared:
                lines.append(f"    {body_alias_name} = {response_var}.body")
                body_alias_declared = True
            lines.append(f"    if isinstance({body_alias_name}, dict):")
            lines.append(f"        assert {body_alias_name}.get('name') == {dict_name_var}")
            lines.append("    else:")
            lines.append(f"        assert {dict_name_var}.lower() in str({body_alias_name}).lower()")
        for fragment in body_assertions:
            lines.append(f"    {response_var}.body_should_contain({json.dumps(fragment)})")
        if expected_status == 404:
            lines.append(f"    if isinstance({response_var}.body, dict):")
            lines.append(f"        message = str({response_var}.body.get(\"message\", \"\"))")
            lines.append("        assert \"not found\" in message.lower()")
            lines.append("    else:")
            lines.append(f"        assert \"not found\" in str({response_var}.body).lower()")

        needs_body_alias = bool(non_empty_fields or save_fields or equal_saved_fields or list_field_assertions)
        if needs_body_alias and not body_alias_declared:
            lines.append(f"    {body_alias_name} = {response_var}.body")
            body_alias_declared = True

        for field in non_empty_fields:
            lines.append(f"    if isinstance({body_alias_name}, dict):")
            lines.append(f"        assert {body_alias_name}.get({json.dumps(field)})")
            lines.append("    else:")
            lines.append(f"        assert {json.dumps(field)} in str({body_alias_name})")

        for field, alias_name in save_fields:
            saved_var = saved_alias_vars.get(alias_name)
            if not saved_var:
                base_name = _to_snake_case(alias_name) or "value"
                if not base_name.startswith("saved_"):
                    base_name = f"saved_{base_name}"
                count = saved_name_counts.get(base_name, 0)
                saved_name_counts[base_name] = count + 1
                saved_var = base_name if count == 0 else f"{base_name}_{count + 1}"
                saved_alias_vars[alias_name] = saved_var
            lines.append(f"    if isinstance({body_alias_name}, dict):")
            lines.append(f"        {saved_var} = {body_alias_name}.get({json.dumps(field)})")
            lines.append("    else:")
            lines.append(f"        {saved_var} = str({body_alias_name})")
            lines.append(f"    assert {saved_var}")

        for field, alias_name in equal_saved_fields:
            saved_var = saved_alias_vars.get(alias_name)
            if not saved_var:
                lines.append(f"    # TODO: Saved value '{alias_name}' referenced before assignment")
                continue
            lines.append(f"    if isinstance({body_alias_name}, dict):")
            lines.append(f"        assert {body_alias_name}.get({json.dumps(field)}) == {saved_var}")
            lines.append("    else:")
            lines.append(f"        assert {saved_var} in str({body_alias_name})")

        for field in list_field_assertions:
            items_var = _to_snake_case(field) or "items"
            items_var = f"{items_var}_items"
            lines.append(f"    if isinstance({body_alias_name}, dict):")
            lines.append(f"        {items_var} = {body_alias_name}.get({json.dumps(field)})")
            lines.append(f"        assert isinstance({items_var}, list)")
            lines.append(f"        assert {items_var}")
            lines.append("    else:")
            lines.append(f"        assert {json.dumps(field)} in str({body_alias_name})")

        if is_collection_get:
            if not body_alias_declared:
                lines.append(f"    {body_alias_name} = {response_var}.body")
                body_alias_declared = True
            lines.append(f"    if isinstance({body_alias_name}, list):")
            if delete_occurred:
                if create_name_var:
                    lines.append(
                        f"        assert all(not (isinstance(item, dict) and item.get('name') == {create_name_var}) for item in {body_alias_name})"
                    )
                targets = list(deleted_param_exprs) or ([primary_id_expr] if primary_id_expr else [])
                for expr in targets:
                    lines.append(
                        f"        assert all(not (isinstance(item, dict) and item.get('id') == {expr}) for item in {body_alias_name})"
                    )
            else:
                if create_name_var:
                    lines.append(
                        f"        assert any(isinstance(item, dict) and item.get('name') == {create_name_var} for item in {body_alias_name})"
                    )
                elif primary_id_expr:
                    lines.append(
                        f"        assert any(isinstance(item, dict) and item.get('id') == {primary_id_expr} for item in {body_alias_name})"
                    )
            lines.append("    else:")
            if delete_occurred:
                if create_name_var:
                    lines.append(f"        assert {create_name_var}.lower() not in str({body_alias_name}).lower()")
                if primary_id_expr:
                    lines.append(f"        assert str({primary_id_expr}) not in str({body_alias_name})")
            else:
                if create_name_var:
                    lines.append(f"        assert {create_name_var}.lower() in str({body_alias_name}).lower()")
                elif primary_id_expr:
                    lines.append(f"        assert str({primary_id_expr}) in str({body_alias_name})")

        if method_upper == "DELETE":
            delete_occurred = True
            if path_param_exprs:
                deleted_param_exprs.update(path_param_exprs)
        lines.append("")
        step_index += 1

    return "\n".join(lines).rstrip() + "\n"


def _fallback_flow_negative_cases(story_block: str) -> Optional[Dict[str, Any]]:
    story_lines = _story_lines(story_block)
    if not story_lines:
        return None

    lowered_story = "\n".join(story_lines).lower()
    return _fallback_generic_flow_cases(story_lines, lowered_story)


def _normalize_flow_negative_payload(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    generated_cases = payload.get("generated_testcases")
    if isinstance(generated_cases, list):
        if not isinstance(payload.get("skipped_categories"), list):
            payload["skipped_categories"] = []
        return payload

    flow_cases = payload.get("flow_negative_testcases")
    edge_cases = payload.get("edge_cases")
    normalized_cases: List[Dict[str, Any]] = []

    if isinstance(flow_cases, list):
        for case in flow_cases:
            if not isinstance(case, dict):
                continue
            normalized = dict(case)
            if not normalized.get("category"):
                normalized["category"] = normalized.get("break_type") or "input_break"
            normalized_cases.append(normalized)

    if isinstance(edge_cases, list):
        for case in edge_cases:
            if not isinstance(case, dict):
                continue
            normalized = dict(case)
            if not normalized.get("category"):
                normalized["category"] = (
                    normalized.get("edge_type")
                    or normalized.get("break_type")
                    or "edge"
                )
            normalized_cases.append(normalized)

    if not normalized_cases:
        return None

    normalized_payload = dict(payload)
    normalized_payload["generated_testcases"] = normalized_cases
    normalized_payload.setdefault("skipped_categories", [])
    return normalized_payload


def _fallback_reqres_flow_cases(story_lines: List[str]) -> Optional[Dict[str, Any]]:
    story_email = _extract_story_email(story_lines) or _placeholder_scalar("email")
    story_password = _extract_story_password(story_lines) or _placeholder_scalar("password")
    page_number = _extract_story_page_number(story_lines)

    actor = {
        "email": story_email,
        "password": story_password,
        "page": page_number,
    }

    flow_cases = [
        {
            "id": "FN-01",
            "break_type": "input_break",
            "scenario_description": "POST /api/register omitting the required password field for the saved actor.",
            "broken_contract": "Registration requires a non-empty password to issue a token for the actor.",
            "api_expected_result": {
                "status_code": 400,
                "behavior": "API rejects the request due to the missing password.",
            },
        },
        {
            "id": "FN-02",
            "break_type": "input_break",
            "scenario_description": "POST /api/login without the password while using the saved actor email.",
            "broken_contract": "Login requires the password field to authenticate the actor.",
            "api_expected_result": {
                "status_code": 400,
                "behavior": "API rejects the request due to the missing password.",
            },
        },
        {
            "id": "FN-03",
            "break_type": "dependency_break",
            "scenario_description": "POST /api/login with the saved actor email but an incorrect password value.",
            "broken_contract": "Login must reject invalid credential pairs for the saved actor.",
            "api_expected_result": {
                "status_code": 400,
                "behavior": "API rejects the login attempt and no token is issued.",
            },
        },
    ]

    for case in flow_cases:
        case["actor"] = dict(actor)
        case["category"] = case.get("break_type") or "input_break"

    return {
        "actor": actor,
        "generated_testcases": flow_cases,
        "flow_negative_testcases": flow_cases,
        "edge_cases": [],
        "skipped_categories": [],
    }


def _fallback_generic_flow_cases(
    story_lines: List[str],
    lowered_story: str,
) -> Optional[Dict[str, Any]]:
    story_email = _extract_story_email(story_lines) or _placeholder_scalar("email")
    story_password = _extract_story_password(story_lines) or _placeholder_scalar("password")

    flow_cases: List[Dict[str, Any]] = []

    case_index = 1

    def _next_id() -> str:
        nonlocal case_index
        value = f"FN-{case_index:02d}"
        case_index += 1
        return value

    if "register" in lowered_story or "sign up" in lowered_story:
        flow_cases.append(
            {
                "id": _next_id(),
                "break_type": "input_break",
                "scenario_description": f"POST register for {story_email} without password to confirm required fields enforcement.",
                "broken_contract": "Registration must reject payloads missing password for the saved actor.",
                "api_expected_result": {
                    "status_code": 400,
                    "behavior": "API returns validation error and does not issue a token.",
                },
            }
        )

    if "login" in lowered_story or "log in" in lowered_story or "sign in" in lowered_story:
        flow_cases.append(
            {
                "id": _next_id(),
                "break_type": "input_break",
                "scenario_description": f"POST login for {story_email} with incorrect password while keeping registered email constant.",
                "broken_contract": "Login must fail when the supplied password does not match the registered actor.",
                "api_expected_result": {
                    "status_code": 400,
                    "behavior": "API rejects the login attempt and no token is issued.",
                },
            }
        )

    if not flow_cases:
        return None

    for case in flow_cases:
        case["category"] = case.get("break_type") or "input_break"

    return {
        "generated_testcases": flow_cases,
        "flow_negative_testcases": flow_cases,
        "edge_cases": [],
        "skipped_categories": [],
    }


def _generate_flow_negative_cases(story: Optional[str]) -> Optional[Dict[str, Any]]:
    story_block = (story or "").strip()
    if not story_block:
        return None
    if openai_client is None:
        return _fallback_flow_negative_cases(story_block)
    prompt = build_flow_negative_cases_prompt(story_block)
    try:
        model_name = os.getenv("AI_MODEL_NAME", "gpt-4o")
        result = openai_client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=int(os.getenv("AI_MAX_TOKENS", "1024")),
            temperature=float(os.getenv("AI_TEMPERATURE", "0")),
        )
        raw_content = (result.choices[0].message.content or "").strip()
        cleaned = re.sub(r"^```json\\s*|```$", "", raw_content, flags=re.MULTILINE).strip()
        payload = json.loads(cleaned)
    except Exception:
        payload = None
    normalized_payload = _normalize_flow_negative_payload(payload)
    if normalized_payload:
        return normalized_payload

    return _fallback_flow_negative_cases(story_block)


def _scenario_text(entry: Dict[str, Any]) -> str:
    for key in ("scenario_description", "description"):
        value = entry.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


def _expected_result_payload(entry: Dict[str, Any]) -> Any:
    if not isinstance(entry, dict):
        return None
    if "api_expected_result" in entry:
        return entry["api_expected_result"]
    if "expected_result" in entry:
        return entry["expected_result"]
    return None


def _extract_expected_status(entry: Dict[str, Any], break_type: str) -> int:
    if isinstance(entry, dict) and entry.get("flow_stop"):
        return 0
    default_map = {
        "auth": 401,
        "auth_break": 401,
        "backend_failure": 500,
        "dependency_break": 401,
        "edge": 400,
        "fuzz": 400,
        "input_break": 400,
        "state": 409,
        "state_break": 401,
        "sequence_break": 401,
        "output_break": 500,
        "boundary_value": 400,
        "timing_issue": 409,
        "optional_field_gap": 200,
        "idempotency_issue": 200,
        "edge_case": 400,
    }
    default_status = default_map.get(break_type, 400)
    payload = _expected_result_payload(entry)
    candidates: List[int] = []
    text_fragments: List[str] = []

    if isinstance(payload, dict):
        for key in ("status_code", "http_status", "expected_status", "status"):
            value = payload.get(key)
            if isinstance(value, int) and 100 <= value <= 599:
                candidates.append(value)
            elif isinstance(value, (list, tuple, set)):
                for item in value:
                    if isinstance(item, int) and 100 <= item <= 599:
                        candidates.append(item)
                    elif isinstance(item, str):
                        digits = re.search(r"\b(\d{3})\b", item)
                        if digits:
                            status = int(digits.group(1))
                            if 100 <= status <= 599:
                                candidates.append(status)
                        elif item.isdigit():
                            status = int(item)
                            if 100 <= status <= 599:
                                candidates.append(status)
            elif isinstance(value, str):
                digits = re.search(r"\b(\d{3})\b", value)
                if digits:
                    status = int(digits.group(1))
                    if 100 <= status <= 599:
                        candidates.append(status)
                elif value.isdigit():
                    status = int(value)
                    if 100 <= status <= 599:
                        candidates.append(status)
        for value in payload.values():
            if isinstance(value, str):
                text_fragments.append(value)
            elif isinstance(value, (list, tuple, set)):
                text_fragments.extend(str(item) for item in value)
    elif isinstance(payload, (list, tuple, set)):
        text_fragments.extend(str(item) for item in payload)
    elif payload is not None:
        text_fragments.append(str(payload))

    for fragment in text_fragments:
        digits = re.search(r"\b(\d{3})\b", fragment)
        if digits:
            status = int(digits.group(1))
            if 100 <= status <= 599:
                candidates.append(status)

    keyword_map = {
        "unauthorized": 401,
        "forbidden": 403,
        "not found": 404,
        "bad request": 400,
        "validation": 422,
        "timeout": 504,
        "duplicate": 409,
        "conflict": 409,
        "internal": 500,
        "server error": 500,
    }
    lowered_payload = " ".join(text_fragments).lower()
    for keyword, status in keyword_map.items():
        if keyword in lowered_payload:
            candidates.append(status)

    for status in candidates:
        if 100 <= status <= 599:
            return status
    return default_status


def _extract_body_fragments(entry: Dict[str, Any]) -> List[str]:
    payload = _expected_result_payload(entry)
    fragments: List[str] = []
    if isinstance(payload, dict):
        for key in ("body_contains", "body_expectations", "error_fragments"):
            value = payload.get(key)
            if isinstance(value, (list, tuple, set)):
                fragments.extend(str(item) for item in value if str(item).strip())
        for key in ("error_message", "message_contains"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                fragments.append(value)
    elif isinstance(payload, (list, tuple, set)):
        fragments.extend(str(item) for item in payload if str(item).strip())
    elif isinstance(payload, str):
        quoted = [match.group(1) for match in re.finditer(r'"([^"]+)"', payload)]
        if quoted:
            fragments.extend(quoted)
        else:
            keywords = ["error", "invalid", "missing", "duplicate", "conflict", "unauthorized"]
            lowered = payload.lower()
            for keyword in keywords:
                if keyword in lowered:
                    fragments.append(keyword)
    return list(dict.fromkeys(fragment.strip() for fragment in fragments if fragment.strip()))


def _fake_email(tag: str) -> str:
    base = str(_placeholder_scalar("email"))
    local, _, domain = base.partition("@")
    local = local or "user"
    domain = domain or "example.com"
    slug = re.sub(r"[^a-z0-9]+", ".", (tag or "edge").lower()).strip(".")
    if slug:
        return f"{local}.{slug}@{domain}"
    return f"{local}@{domain}"


def _fake_password(tag: str) -> str:
    base = str(_placeholder_scalar("password"))
    slug = re.sub(r"[^a-z0-9]+", "", (tag or "edge").lower()) or "edge"
    return f"{base}_{slug}"


def _infer_flow_operation(entry: Dict[str, Any]) -> str:
    text_parts = [
        _scenario_text(entry),
        str(entry.get("broken_contract") or ""),
        str(entry.get("affected_api_step") or ""),
    ]
    lowered = " ".join(part.lower() for part in text_parts if part)
    if any(token in lowered for token in ["fetch", "list", "retrieve", "users", "profile"]):
        return "fetch"
    if any(token in lowered for token in ["login", "log in", "signin", "authenticate"]):
        return "login"
    return "register"


def _infer_edge_operation(entry: Dict[str, Any]) -> str:
    text_parts = [
        str(entry.get("affected_api_step") or ""),
        _scenario_text(entry),
        str(entry.get("description") or ""),
    ]
    lowered = " ".join(part.lower() for part in text_parts if part)
    if any(token in lowered for token in ["login", "log in", "signin", "authenticate"]):
        return "login"
    if any(token in lowered for token in ["fetch", "list", "retrieve", "users", "profile"]):
        return "fetch"
    return "register"


def _render_flow_case_test(case: Dict[str, Any]) -> List[str]:
    case_id = case.get("id") or "case"
    break_type = (case.get("break_type") or case.get("category") or "").lower()
    slug = _normalize_identifier(case_id) or "case"
    break_slug = _normalize_identifier(break_type) or "scenario"
    test_name = f"test_flow_negative_{slug}_{break_slug}"

    if break_type and break_type != "input_break":
        return [
            f"def {test_name}():",
            f"    scenario = FLOW_NEGATIVE_SCENARIO_MAP[{json.dumps(case_id)}]",
            f"    pytest.skip('Unsupported break type: {break_type}')",
        ]

    scenario_lookup = json.dumps(case_id)
    scenario_text = _scenario_text(case)
    expected_status = _extract_expected_status(case, break_type)
    body_fragments = _extract_body_fragments(case)
    base_tag = f"{case_id}_flow"
    actor_info = case.get("actor") or {}
    actor_email = actor_info.get("email") or case.get("actor_email") or _fake_email(base_tag)
    actor_password = actor_info.get("password") or case.get("actor_password") or _fake_password(base_tag)
    scenario_lower = (scenario_text or "").lower()
    shared_email = actor_email or _fake_email(base_tag)
    shared_password = actor_password or _fake_password(base_tag)
    email_field, password_field = _infer_auth_field_names(case)
    if not email_field or not password_field:
        fallback_fields = _fallback_request_fields(case)
        if not email_field and fallback_fields:
            email_field = fallback_fields[0]
        if not password_field and len(fallback_fields) > 1:
            password_field = fallback_fields[1]
    if not email_field or not password_field:
        return [
            f"def {test_name}():",
            f"    scenario = FLOW_NEGATIVE_SCENARIO_MAP[{scenario_lookup}]",
            "    pytest.skip('Required auth fields could not be inferred for negative flow case')",
        ]

    operation = _infer_flow_operation(case)
    lines = [
        f"def {test_name}():",
        f"    scenario = FLOW_NEGATIVE_SCENARIO_MAP[{scenario_lookup}]",
        f"    expected_status = {expected_status}",
    ]

    if scenario_text:
        lines.append("    scenario_description = scenario.get('scenario_description') or scenario.get('description') or ''")
        lines.append("    if scenario_description:")
        lines.append("        print(f\"Flow negative: {scenario_description}\")")
    else:
        lines.append("    scenario_description = scenario.get('scenario_description') or scenario.get('description') or ''")

    if operation == "register":
        lines.append(
            f"    request_data = {{'body': {{{json.dumps(email_field)}: {json.dumps(shared_email)}, {json.dumps(password_field)}: {json.dumps(shared_password)}}}}}"
        )
        if "missing password" in scenario_lower or "empty password" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(password_field)}, None)")
        elif "invalid password" in scenario_lower:
            invalid_password = _derive_invalid_value(password_field or "password", shared_password)
            lines.append(
                f"    request_data['body'][{json.dumps(password_field)}] = {json.dumps(invalid_password)}"
            )
        if "empty email" in scenario_lower or "missing email" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(email_field)}, None)")
        elif "invalid email" in scenario_lower:
            invalid_email = _derive_invalid_value(email_field or "email", shared_email)
            lines.append(f"    request_data['body'][{json.dumps(email_field)}] = {json.dumps(invalid_email)}")
        elif "long email" in scenario_lower or "max email" in scenario_lower:
            long_email = _derive_long_string(email_field or "email", shared_email)
            lines.append(f"    request_data['body'][{json.dumps(email_field)}] = {json.dumps(long_email)}")
        lines.append("    response = REQRES.register(request_data)")
    elif operation == "login":
        lines.append(
            f"    setup_payload = {{'body': {{{json.dumps(email_field)}: {json.dumps(shared_email)}, {json.dumps(password_field)}: {json.dumps(shared_password)}}}}}"
        )
        lines.append("    setup_response = REQRES.register(setup_payload)")
        lines.append("    setup_response.status_should_be(200)")
        lines.append("    setup_body = setup_response.body")
        lines.append("    assert isinstance(setup_body, dict)")
        lines.append("    assert setup_body")
        lines.append(
            f"    request_data = {{'body': {{{json.dumps(email_field)}: {json.dumps(shared_email)}, {json.dumps(password_field)}: {json.dumps(shared_password)}}}}}"
        )
        if "missing password" in scenario_lower or "empty password" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(password_field)}, None)")
        elif "wrong password" in scenario_lower or "incorrect password" in scenario_lower or "invalid password" in scenario_lower:
            invalid_password = _derive_invalid_value(password_field or "password", shared_password)
            lines.append(
                f"    request_data['body'][{json.dumps(password_field)}] = {json.dumps(invalid_password)}"
            )
        if "empty email" in scenario_lower or "missing email" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(email_field)}, None)")
        elif "invalid email" in scenario_lower:
            invalid_email = _derive_invalid_value(email_field or "email", shared_email)
            lines.append(f"    request_data['body'][{json.dumps(email_field)}] = {json.dumps(invalid_email)}")
        elif "long email" in scenario_lower or "max email" in scenario_lower:
            long_email = _derive_long_string(email_field or "email", shared_email)
            lines.append(f"    request_data['body'][{json.dumps(email_field)}] = {json.dumps(long_email)}")
        lines.append("    response = REQRES.verify_by_logging_in(request_data)")
    elif operation == "fetch":
        return [
            f"def {test_name}():",
            f"    scenario = FLOW_NEGATIVE_SCENARIO_MAP[{scenario_lookup}]",
            "    pytest.skip('Fetch operation automation is disabled for input_break flow negatives')",
        ]
    else:
        return [
            f"def {test_name}():",
            f"    scenario = FLOW_NEGATIVE_SCENARIO_MAP[{scenario_lookup}]",
            f"    pytest.skip('No automation implemented for operation: {operation}')",
        ]

    lines.append("    response.status_should_be(expected_status)")

    if body_fragments or expected_status >= 400:
        lines.append("    response_body = response.body")
        if body_fragments:
            lines.append("    response_body_text = str(response_body)")
            for fragment in body_fragments:
                safe_fragment = fragment.lower()
                lines.append(f"    assert {json.dumps(safe_fragment)} in response_body_text.lower()")
        else:
            lines.append("    assert str(response_body)")

    return lines


def _render_edge_case_test(edge_case: Dict[str, Any]) -> List[str]:
    edge_id = edge_case.get("id") or "edge"
    slug = _normalize_identifier(edge_id) or "edge"
    test_name = f"test_edge_case_{slug}"
    edge_lookup = json.dumps(edge_id)
    scenario_text = _scenario_text(edge_case)
    break_type = (
        edge_case.get("break_type")
        or edge_case.get("edge_type")
        or edge_case.get("category")
        or "edge_case"
    ).lower()
    expected_status = _extract_expected_status(edge_case, break_type)
    body_fragments = _extract_body_fragments(edge_case)
    base_tag = f"{edge_id}_edge"
    actor_info = edge_case.get("actor") or {}
    actor_email = actor_info.get("email") or edge_case.get("actor_email") or _fake_email(base_tag)
    actor_password = actor_info.get("password") or edge_case.get("actor_password") or _fake_password(base_tag)
    page_number = actor_info.get("page") or edge_case.get("page") or 1
    try:
        page_number = int(page_number)
    except Exception:
        page_number = 1

    scenario_lower = (scenario_text or "").lower()
    email_field, password_field = _infer_auth_field_names(edge_case)
    if not email_field or not password_field:
        fallback_fields = _fallback_request_fields(edge_case)
        if not email_field and fallback_fields:
            email_field = fallback_fields[0]
        if not password_field and len(fallback_fields) > 1:
            password_field = fallback_fields[1]
    if not email_field or not password_field:
        return [
            f"def {test_name}():",
            f"    scenario = EDGE_CASE_SCENARIO_MAP[{edge_lookup}]",
            "    pytest.skip('Required auth fields could not be inferred for edge case')",
        ]

    if actor_info:
        lines: List[str] = [
            f"def {test_name}():",
            f"    scenario = EDGE_CASE_SCENARIO_MAP[{edge_lookup}]",
            f"    expected_status = {expected_status}",
            "    scenario_description = scenario.get('scenario_description') or scenario.get('description') or ''",
        ]
        lines.append("    if scenario_description:")
        lines.append("        print(f\"Edge case: {scenario_description}\")")

        lines.append(
            f"    register_payload = {{'body': {{{json.dumps(email_field)}: {json.dumps(actor_email)}, {json.dumps(password_field)}: {json.dumps(actor_password)}}}}}"
        )
        lines.append("    register_response = REQRES.register(register_payload)")
        lines.append("    register_response.status_should_be(200)")
        lines.append("    register_body = register_response.body")
        lines.append("    assert isinstance(register_body, dict)")
        lines.append("    saved_token = None")
        lines.append("    if isinstance(register_body, dict):")
        lines.append("        for value in register_body.values():")
        lines.append("            if isinstance(value, str) and value:")
        lines.append("                saved_token = value")
        lines.append("                break")
        lines.append("    assert saved_token")

        lines.append(
            f"    login_payload = {{'body': {{{json.dumps(email_field)}: {json.dumps(actor_email)}, {json.dumps(password_field)}: {json.dumps(actor_password)}}}}}"
        )
        lines.append("    login_response = REQRES.verify_by_logging_in(login_payload)")
        lines.append("    login_response.status_should_be(200)")
        lines.append("    login_body = login_response.body")
        lines.append("    login_token = None")
        lines.append("    if isinstance(login_body, dict):")
        lines.append("        for value in login_body.values():")
        lines.append("            if isinstance(value, str) and value:")
        lines.append("                login_token = value")
        lines.append("                break")
        lines.append("    if login_token is None:")
        lines.append("        login_token = str(login_body)")
        lines.append("    assert login_token == saved_token")

        lines.append("    authorized_headers = {'Authorization': f'Bearer {saved_token}'}")

        if "idempotency" in scenario_lower or "duplicate" in scenario_lower:
            lines.append(f"    first_fetch = REQRES.fetch_records({{'query': {{'page': {page_number}}}, 'headers': authorized_headers}})")
            lines.append("    first_fetch.status_should_be(expected_status)")
            lines.append(f"    second_fetch = REQRES.fetch_records({{'query': {{'page': {page_number}}}, 'headers': authorized_headers}})")
            lines.append("    second_fetch.status_should_be(expected_status)")
            lines.append("    assert first_fetch.body == second_fetch.body")
            return lines

        if "re-login" in scenario_lower or "state_resilience" in scenario_lower or "session" in scenario_lower:
            lines.append(f"    fetch_response = REQRES.fetch_records({{'query': {{'page': {page_number}}}, 'headers': authorized_headers}})")
            lines.append("    fetch_response.status_should_be(200)")
            lines.append(
                f"    relogin_payload = {{'body': {{{json.dumps(email_field)}: {json.dumps(actor_email)}, {json.dumps(password_field)}: {json.dumps(actor_password)}}}}}"
            )
            lines.append("    relogin_response = REQRES.verify_by_logging_in(relogin_payload)")
            lines.append("    relogin_response.status_should_be(expected_status)")
            lines.append("    relogin_body = relogin_response.body")
            lines.append("    relogin_token = None")
            lines.append("    if isinstance(relogin_body, dict):")
            lines.append("        for value in relogin_body.values():")
            lines.append("            if isinstance(value, str) and value:")
            lines.append("                relogin_token = value")
            lines.append("                break")
            lines.append("    if relogin_token is None:")
            lines.append("        relogin_token = str(relogin_body)")
            lines.append("    assert relogin_token == saved_token")
    return lines

    # Fallback generic behavior
    operation = _infer_edge_operation(edge_case)
    shared_email = _fake_email(base_tag)
    shared_password = _fake_password(base_tag)

    lines = [
        f"def {test_name}():",
        f"    scenario = EDGE_CASE_SCENARIO_MAP[{edge_lookup}]",
        f"    expected_status = {expected_status}",
        "    scenario_description = scenario.get('scenario_description') or scenario.get('description') or ''",
    ]

    if operation == "register":
        lines.append(
            f"    request_data = {{'body': {{{json.dumps(email_field)}: {json.dumps(shared_email)}, {json.dumps(password_field)}: {json.dumps(shared_password)}}}}}"
        )
        if "empty password" in scenario_lower or "missing password" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(password_field)}, None)")
        elif "long password" in scenario_lower or "max password" in scenario_lower:
            long_password = _derive_long_string(password_field or "password", shared_password, extra=256)
            lines.append(
                f"    request_data['body'][{json.dumps(password_field)}] = {json.dumps(long_password)}"
            )
        if "empty email" in scenario_lower or "missing email" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(email_field)}, None)")
        elif "long email" in scenario_lower or "max email" in scenario_lower:
            long_email = _derive_long_string(email_field or "email", shared_email)
            lines.append(f"    request_data['body'][{json.dumps(email_field)}] = {json.dumps(long_email)}")
        lines.append("    response = REQRES.register(request_data)")
    elif operation == "login":
        lines.append(
            f"    setup_payload = {{'body': {{{json.dumps(email_field)}: {json.dumps(shared_email)}, {json.dumps(password_field)}: {json.dumps(shared_password)}}}}}"
        )
        lines.append("    setup_response = REQRES.register(setup_payload)")
        lines.append("    setup_response.status_should_be(200)")
        lines.append(
            f"    request_data = {{'body': {{{json.dumps(email_field)}: {json.dumps(shared_email)}, {json.dumps(password_field)}: {json.dumps(shared_password)}}}}}"
        )
        if "empty password" in scenario_lower or "missing password" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(password_field)}, None)")
        elif "long password" in scenario_lower or "max password" in scenario_lower:
            long_password = _derive_long_string(password_field or "password", shared_password, extra=256)
            lines.append(
                f"    request_data['body'][{json.dumps(password_field)}] = {json.dumps(long_password)}"
            )
        if "empty email" in scenario_lower or "missing email" in scenario_lower:
            lines.append(f"    request_data['body'].pop({json.dumps(email_field)}, None)")
        elif "long email" in scenario_lower or "max email" in scenario_lower:
            long_email = _derive_long_string(email_field or "email", shared_email)
            lines.append(f"    request_data['body'][{json.dumps(email_field)}] = {json.dumps(long_email)}")
        lines.append("    response = REQRES.verify_by_logging_in(request_data)")
    elif operation == "fetch":
        lines.append(f"    request_data = {{'query': {{'page': {page_number}}}}}")
        if "token" in scenario_lower or "authorization" in scenario_lower:
            invalid_token = _derive_invalid_value("token", None)
            lines.append(
                f"    request_data['headers'] = {{'Authorization': 'Bearer {invalid_token}'}}"
            )
        if "duplicate" in scenario_lower or "replay" in scenario_lower or "idempotent" in scenario_lower:
            lines.append("    first_response = REQRES.fetch_records(request_data)")
            lines.append("    first_response.status_should_be(200)")
        lines.append("    response = REQRES.fetch_records(request_data)")
    else:
        return [
            f"def {test_name}():",
            f"    scenario = EDGE_CASE_SCENARIO_MAP[{edge_lookup}]",
            "    pytest.skip('Edge case requires manual validation')",
        ]

    lines.append("    response.status_should_be(expected_status)")

    if body_fragments or expected_status >= 400:
        lines.append("    response_body_text = str(response.body)")
        if body_fragments:
            for fragment in body_fragments:
                safe_fragment = fragment.lower()
                lines.append(f"    assert {json.dumps(safe_fragment)} in response_body_text.lower()")
        else:
            lines.append("    assert response_body_text")

    return lines


def _render_flow_negative_tests(payload: Dict[str, Any]) -> str:
    payload = _normalize_flow_negative_payload(payload) or payload
    raw_generated_cases = [
        case for case in payload.get("generated_testcases", []) if isinstance(case, dict)
    ]
    raw_flow_cases: List[Dict[str, Any]] = []
    raw_edge_cases: List[Dict[str, Any]] = []

    if raw_generated_cases:
        for case in raw_generated_cases:
            category = str(case.get("category") or case.get("break_type") or "").lower()
            if category == "edge":
                raw_edge_cases.append(case)
            else:
                raw_flow_cases.append(case)
    else:
        raw_flow_cases = [case for case in payload.get("flow_negative_testcases", []) if isinstance(case, dict)]
        raw_edge_cases = [case for case in payload.get("edge_cases", []) if isinstance(case, dict)]

    if not raw_flow_cases and not raw_edge_cases:
        return ""

    actor_payload = None
    if isinstance(payload, dict):
        actor_payload = payload.get("actor")

    normalized_flow_cases: List[Dict[str, Any]] = []
    for raw_case in raw_flow_cases:
        normalized = dict(raw_case)
        if not normalized.get("break_type"):
            normalized["break_type"] = normalized.get("category") or "input_break"
        scenario_text = _scenario_text(normalized)
        if scenario_text and not normalized.get("scenario_description"):
            normalized["scenario_description"] = scenario_text
        if scenario_text and not normalized.get("description"):
            normalized["description"] = scenario_text
        if "api_expected_result" not in normalized and normalized.get("expected_result") is not None:
            normalized["api_expected_result"] = normalized["expected_result"]
        if actor_payload and "actor" not in normalized and isinstance(actor_payload, dict):
            normalized["actor"] = dict(actor_payload)
        normalized_flow_cases.append(normalized)

    normalized_edge_cases: List[Dict[str, Any]] = []
    for raw_case in raw_edge_cases:
        normalized = dict(raw_case)
        if not normalized.get("break_type"):
            normalized["break_type"] = (
                normalized.get("category")
                or normalized.get("edge_type")
                or "edge"
            )
        scenario_text = _scenario_text(normalized)
        if scenario_text and not normalized.get("scenario_description"):
            normalized["scenario_description"] = scenario_text
        if scenario_text and not normalized.get("description"):
            normalized["description"] = scenario_text
        if "api_expected_result" not in normalized and normalized.get("expected_result") is not None:
            normalized["api_expected_result"] = normalized["expected_result"]
        if actor_payload and "actor" not in normalized and isinstance(actor_payload, dict):
            normalized["actor"] = dict(actor_payload)
        normalized_edge_cases.append(normalized)

    lines: List[str] = []
    flow_literal = json.dumps(normalized_flow_cases, indent=4)
    lines.append(f"FLOW_NEGATIVE_SCENARIOS = {flow_literal}")
    lines.append("FLOW_NEGATIVE_SCENARIO_MAP = {case['id']: case for case in FLOW_NEGATIVE_SCENARIOS if 'id' in case}")

    if normalized_edge_cases:
        edge_literal = json.dumps(normalized_edge_cases, indent=4)
        lines.append(f"EDGE_CASE_SCENARIOS = {edge_literal}")
        lines.append("EDGE_CASE_SCENARIO_MAP = {case['id']: case for case in EDGE_CASE_SCENARIOS if 'id' in case}")
    else:
        lines.append("EDGE_CASE_SCENARIOS = []")
        lines.append("EDGE_CASE_SCENARIO_MAP = {}")

    lines.append("")

    for case in normalized_flow_cases:
        case_lines = _render_flow_case_test(case)
        if case_lines:
            lines.extend(case_lines)
            lines.append("")

    for edge in normalized_edge_cases:
        edge_lines = _render_edge_case_test(edge)
        if edge_lines:
            lines.extend(edge_lines)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_negative_story_test_case(
    cases: List[Dict[str, Any]],
    story: Optional[str],
    services: List[Dict[str, Any]],
    flow_payload: Optional[Dict[str, Any]] = None,
) -> str:
    flow_payload = flow_payload or _generate_flow_negative_cases(story)
    if flow_payload:
        rendered = _render_flow_negative_tests(flow_payload)
        if rendered:
            return rendered

    if not cases:
        return ""

    negative_target: Optional[Dict[str, Any]] = None
    negative_mode: Optional[str] = None

    for candidate in cases:
        method = (candidate.get("http_method") or "").upper()
        if method == "GET" and _extract_path_params(candidate.get("path") or ""):
            negative_target = candidate
            negative_mode = "missing_resource"
            break

    if negative_target is None:
        for candidate in cases:
            method = (candidate.get("http_method") or "").upper()
            if candidate.get("requires_payload") and method in {"POST", "PUT", "PATCH"}:
                negative_target = candidate
                negative_mode = "invalid_payload"
                break

    if negative_target is None:
        return ""

    doc_lines = _story_lines(story)
    scenario_line = next((line for line in doc_lines if line.lower().startswith("scenario")), "")
    scenario_name = scenario_line.split(":", 1)[-1].strip() if scenario_line else "story_flow"
    identifier = _normalize_identifier(scenario_name) or "story_flow"
    test_name = f"test_{identifier}_negative"

    lines: List[str] = [f"def {test_name}():"]
    lines.append(f'    print("starting {identifier} negative api test")')
    lines.append('    """Negative scenario generated automatically."""')

    target_class = negative_target.get("class_name") or "ApiService"
    method_name = negative_target.get("method_name") or negative_target.get("operation") or "operation"

    param_expr_map: Dict[str, str] = {}
    path_params = _extract_path_params(negative_target.get("path") or "")
    for original in path_params:
        snake = _to_snake_case(original) or "param"
        invalid_value = _derive_invalid_value(original, _placeholder_scalar(original))
        lines.append(f"    {snake} = {repr(invalid_value)}")
        param_expr_map[snake] = snake
        param_expr_map[_to_snake_case(original)] = snake
        param_expr_map[original] = snake
        param_expr_map[original.lower()] = snake

    body_var: Optional[str] = None
    if negative_target.get("requires_payload") and negative_mode == "invalid_payload":
        body_var = "invalid_body"
        lines.append("    invalid_body = {}  # Missing required fields for negative test")

    payload_literal = _format_case_payload(negative_target, body_var, param_expr_map)
    response_var = _to_snake_case(f"{method_name}_negative_response") or "negative_response"

    lines.append(f"    {response_var} = {target_class}.{method_name}({payload_literal})")

    if negative_mode == "missing_resource":
        expected_status = negative_target.get("negative_status") or 404
        lines.append(f"    {response_var}.status_should_be({expected_status})")
        lines.append(f"    if isinstance({response_var}.body, dict):")
        lines.append(f"        message = str({response_var}.body.get('message', ''))")
        lines.append("        assert 'not found' in message.lower()")
        lines.append("    else:")
        lines.append(f"        assert 'not found' in str({response_var}.body).lower()")
    else:
        expected_status = negative_target.get("negative_status") or 400
        lines.append(f"    {response_var}.status_should_be({expected_status})")
        lines.append(f"    if isinstance({response_var}.body, dict):")
        lines.append(f"        assert {response_var}.body.get('error')")
        lines.append("    else:")
        lines.append(f"        {response_var}.body_should_contain('error')")

    lines.append("")
    return "\n".join(lines)


_METHOD_KEYWORDS: Dict[str, Set[str]] = {
    "GET": {
        "get",
        "gets",
        "retrieve",
        "retrieves",
        "fetch",
        "fetches",
        "list",
        "lists",
        "find",
        "finds",
        "read",
        "reads",
    },
    "POST": {
        "create",
        "creates",
        "creating",
        "add",
        "adds",
        "adding",
        "submit",
        "submits",
        "posting",
        "post",
        "posts",
    },
    "PUT": {
        "update",
        "updates",
        "updating",
        "modify",
        "modifies",
        "modifying",
        "set",
        "sets",
        "replace",
        "replaces",
        "overwrite",
        "overwrites",
    },
    "PATCH": {
        "patch",
        "patches",
        "update",
        "updates",
        "modify",
        "modifies",
        "change",
        "changes",
        "changing",
    },
    "DELETE": {
        "delete",
        "deletes",
        "deleting",
        "remove",
        "removes",
        "removing",
        "cancel",
        "cancels",
        "canceling",
    },
}

_COMMON_STORY_STOPWORDS: Set[str] = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "with",
    "without",
    "to",
    "for",
    "in",
    "on",
    "at",
    "by",
    "this",
    "that",
    "these",
    "those",
    "when",
    "then",
    "should",
    "shouldnt",
    "shouldn't",
    "client",
    "api",
    "service",
    "story",
    "step",
    "from",
    "of",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "per",
    "as",
}


def _spec_keyword_tokens(spec: Any) -> Set[str]:
    tokens: Set[str] = set()
    tokens.update(_tokenize(getattr(spec, "operation_name", "") or ""))
    tokens.update(_tokenize(getattr(spec, "path", "") or ""))
    method = (getattr(spec, "http_method", "") or "").upper()
    tokens.update(_METHOD_KEYWORDS.get(method, set()))
    return {token for token in tokens if token and token not in _COMMON_STORY_STOPWORDS}


def _case_keyword_tokens(case: Dict[str, Any]) -> Set[str]:
    tokens: Set[str] = set()
    tokens.update(_tokenize(case.get("operation", "") or ""))
    tokens.update(_tokenize(case.get("path", "") or ""))
    method = (case.get("http_method", "") or "").upper()
    tokens.update(_METHOD_KEYWORDS.get(method, set()))
    return {token for token in tokens if token and token not in _COMMON_STORY_STOPWORDS}


def _score_token_overlap(line_tokens: Set[str], spec_tokens: Set[str]) -> int:
    matches: Set[str] = set()
    for token in line_tokens:
        for spec_token in spec_tokens:
            if token == spec_token or token.startswith(spec_token) or spec_token.startswith(token):
                matches.add(spec_token)
                break
    return len(matches)


def _match_story_line_to_spec(line: str, specs: List[Any]) -> Optional[Any]:
    tokens = {token for token in _tokenize(line) if token}
    if not tokens:
        return None

    best_spec = None
    best_score = 0
    for spec in specs:
        spec_tokens = _spec_keyword_tokens(spec)
        if not spec_tokens:
            continue
        score = _score_token_overlap(tokens, spec_tokens)
        if score > best_score:
            best_score = score
            best_spec = spec

    return best_spec if best_score > 0 else None


def _match_story_line_to_case(line: str, cases: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    tokens = {token for token in _tokenize(line) if token}
    if not tokens:
        return None

    best_case = None
    best_score = 0
    lowered_line = line.lower()
    prefer_get = any(
        fragment in lowered_line
        for fragment in [
            "does not exist",
            "retrieve",
            "retrieves",
            "fetch",
            "fetches",
            " get ",
            "look up",
            "lookup",
            "view",
            "reads",
        ]
    )
    prefer_delete = any(fragment in lowered_line for fragment in ["delete", "deletes", "remov", "cleanup"])
    prefer_create = any(fragment in lowered_line for fragment in ["create", "creates", "add", "adding", "post"])
    prefer_update = any(fragment in lowered_line for fragment in ["update", "updates", "change", "changes", "modify", "modifies", "set "])
    prefer_login = any(
        fragment in lowered_line
        for fragment in [
            " login",
            " log in",
            " logs in",
            " logging in",
            "signin",
            "sign in",
            " signs in",
        ]
    )
    if prefer_login:
        tokens.update({"login", "log", "signin", "sign", "logging"})
    mentions_id = " id " in f" {lowered_line} " or "with id" in lowered_line
    numeric_tokens = {token for token in tokens if token.isdigit()}
    mentions_numeric_id = bool(numeric_tokens)
    mentions_status = "status" in lowered_line
    mentions_upload = "upload" in lowered_line or "file" in lowered_line

    for case in cases:
        case_tokens = _case_keyword_tokens(case)
        if not case_tokens:
            continue
        score = _score_token_overlap(tokens, case_tokens)
        http_method = (case.get("http_method") or "").upper()
        operation_name = (case.get("operation") or case.get("method_name") or "").lower()
        path_params = _extract_path_params(case.get("path") or "")
        path_literal = (case.get("path") or "").lower()
        is_form_case = _is_form_encoded_case(case)
        default_query_raw = case.get("default_query") or case.get("query") or {}
        default_query = default_query_raw if isinstance(default_query_raw, dict) else {}
        has_id_param = any(param.lower().endswith("id") for param in path_params)
        has_path_params = bool(path_params)
        path_mentions_status = "status" in path_literal

        if prefer_get:
            if http_method in {"GET", "HEAD"}:
                score += 6
            else:
                score -= 4
            if any(keyword in operation_name for keyword in ["get", "find", "list", "retrieve"]):
                score += 3
            if "update" in operation_name or "delete" in operation_name:
                score -= 5
            if path_mentions_status and not mentions_status:
                score -= 3
        elif prefer_delete:
            if http_method == "DELETE":
                score += 5
            else:
                score -= 4
            if "delete" not in operation_name and "remove" not in operation_name:
                score -= 2
        elif prefer_create:
            if http_method != "POST":
                score -= 4
            else:
                score += 4
            if any(keyword in operation_name for keyword in ["add", "create", "post"]):
                score += 4
            if "update" in operation_name or "delete" in operation_name:
                score -= 10
            if path_params:
                score -= 4
            else:
                score += 2
            if case.get("requires_payload"):
                score += 2
            else:
                score -= 3
            if is_form_case:
                score -= 6
        elif prefer_update:
            if http_method in {"PUT", "PATCH"}:
                score += 6
            elif http_method == "POST":
                score -= 4
            if any(keyword in operation_name for keyword in ["update", "modify", "change", "set"]):
                score += 5
            if not case.get("requires_payload"):
                score -= 3
            if is_form_case:
                score -= 8
        elif is_form_case:
            score -= 2
        if is_form_case and not prefer_create and not prefer_update:
            score -= 1
        if mentions_id or mentions_numeric_id:
            if has_id_param:
                score += 6
            elif has_path_params:
                score += 3
            else:
                score -= 2
        elif has_id_param:
            score += 1
        if (mentions_id or mentions_numeric_id) and default_query:
            score -= 1
            if any((key or "").lower() == "status" for key in default_query):
                score -= 2
        if (mentions_id or mentions_numeric_id) and "status" in operation_name:
            score -= 2
        if mentions_status and ("status" in operation_name or path_mentions_status):
            score += 2
        if not mentions_status and path_mentions_status:
            score -= 2
        if mentions_upload and "upload" in operation_name:
            score += 3
        if prefer_login:
            if any(
                keyword in operation_name
                for keyword in ["login", "log_in", "logging_in", "logging-in", "sign_in", "signin"]
            ):
                score += 8
            elif "register" in operation_name or "fetch" in operation_name:
                score -= 6
            if "login" in path_literal:
                score += 4
            if http_method not in {"POST", "PUT", "PATCH"}:
                score -= 3
            if case.get("requires_payload"):
                score += 2
            else:
                score -= 2
        if operation_name.startswith("find") and not prefer_get and not mentions_status:
            score -= 1
        if score > best_score:
            best_score = score
            best_case = case

    return best_case if best_score > 0 else None


def _match_story_blocks_to_specs(blocks: List[Dict[str, Any]], specs: List[Any]) -> List[str]:
    matched_keys: List[str] = []
    for block in blocks:
        when_line = block.get("when")
        if not when_line:
            continue
        spec = _match_story_line_to_spec(when_line, specs)
        if spec is None:
            continue
        normalized = _normalize_identifier(spec.key)
        matched_keys.append(normalized)
    return matched_keys


def _build_story_flow_suite(
    services: List[Dict[str, Any]],
    cases: List[Dict[str, Any]],
    story: Optional[str],
    flow_payload: Optional[Dict[str, Any]] = None,
) -> str:
    flow_cases = _identify_story_flow_cases(cases)
    register_case = flow_cases.get("register")
    login_case = flow_cases.get("login")
    fetch_case = flow_cases.get("fetch")
    all_cases: List[Dict[str, Any]] = []
    for service in services:
        for operation in service.get("operations", []):
            if isinstance(operation, dict):
                all_cases.append(operation)

    all_flow_cases = _identify_story_flow_cases(all_cases) if all_cases else {}
    register_case = register_case or all_flow_cases.get("register")
    login_case = login_case or all_flow_cases.get("login")
    fetch_case = fetch_case or all_flow_cases.get("fetch")

    if login_case in (register_case, fetch_case):
        login_candidate = all_flow_cases.get("login")
        if login_candidate is not None and login_candidate not in (register_case, fetch_case):
            login_case = login_candidate

    if fetch_case is login_case:
        fetch_candidate = all_flow_cases.get("fetch")
        if fetch_candidate is not None and fetch_candidate is not login_case:
            fetch_case = fetch_candidate

    if not register_case or not login_case or not fetch_case:
        return ""

    class_names = _collect_service_classes(services)
    if not class_names:
        return ""

    module_name = _FILE_NAME.replace(".py", "")
    import_targets = ", ".join(class_names)

    primary_class = (
        register_case.get("class_name")
        or login_case.get("class_name")
        or fetch_case.get("class_name")
        or class_names[0]
    )

    service_entry = next(
        (service for service in services if service.get("class_name") == primary_class),
        services[0] if services else {},
    )
    env_service_name = service_entry.get("service") or service_entry.get("class_name") or primary_class
    env_prefix = _service_env_prefix(env_service_name)

    actor_email, actor_password = _extract_actor_credentials(story, register_case, login_case)
    email_field, password_field = _infer_auth_field_names(register_case or login_case)
    if not email_field or not password_field:
        fallback_fields = _fallback_request_fields(register_case or login_case)
        if not email_field and fallback_fields:
            email_field = fallback_fields[0]
        if not password_field and len(fallback_fields) > 1:
            password_field = fallback_fields[1]
    token_fields = []
    for case in (register_case, login_case, fetch_case):
        token_fields.extend(_infer_token_fields_from_schema(case))
    token_fields = sorted({field for field in token_fields if field})
    default_page = _extract_default_page(story, fetch_case)

    scenario_lines = _story_lines(story)
    scenario_line = next((line for line in scenario_lines if line.lower().startswith("scenario")), "")
    scenario_name = scenario_line.split(":", 1)[-1].strip() if scenario_line else "story_flow"
    scenario_identifier = _normalize_identifier(scenario_name) or "story_flow"
    scenario_display = scenario_name or scenario_identifier

    register_method = _method_identifier(register_case)
    login_method = _method_identifier(login_case)
    fetch_method = _method_identifier(fetch_case)

    register_success_status = int(
        register_case.get("expected_status")
        or (register_case.get("response_schema") or {}).get("expected_status")
        or 200
    )
    login_success_status = int(
        login_case.get("expected_status")
        or (login_case.get("response_schema") or {}).get("expected_status")
        or 200
    )
    fetch_success_status = int(
        fetch_case.get("expected_status")
        or (fetch_case.get("response_schema") or {}).get("expected_status")
        or 200
    )

    bad_request_status = 400
    unauthorized_status = 401

    flow_negative_map: Dict[str, Dict[str, Any]] = {}
    flow_break_map: Dict[str, Dict[str, Any]] = {}
    if isinstance(flow_payload, dict):
        normalized_payload = _normalize_flow_negative_payload(flow_payload) or flow_payload
        entries = normalized_payload.get("generated_testcases")
        if not isinstance(entries, list):
            entries = normalized_payload.get("flow_negative_testcases", [])
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id:
                flow_negative_map[entry_id] = entry
            break_type = (entry.get("break_type") or entry.get("category") or "").lower()
            if break_type and break_type not in flow_break_map:
                flow_break_map[break_type] = entry

    def _flow_expected(case_id: str, fallback_status: int, fallback_break: str) -> int:
        entry = flow_negative_map.get(case_id)
        if entry is None and fallback_break:
            entry = flow_break_map.get(fallback_break)
        if entry is None:
            return fallback_status
        return _extract_expected_status(entry, (entry.get("break_type") or entry.get("category") or fallback_break or ""))

    fn1_expected_status = _flow_expected("FN-01", bad_request_status, "input_break")
    fn2_expected_status = _flow_expected("FN-02", bad_request_status, "input_break")
    fn3_expected_status = _flow_expected("FN-03", bad_request_status, "input_break")

    message_literal = json.dumps(f"starting {scenario_display} api test")

    lines: List[str] = [
        '"""Auto-generated API tests."""',
        "",
        "from __future__ import annotations",
        "",
        "import os",
        "from typing import Any, Dict, Tuple",
        "",
        "import pytest",
        "",
        f"from pages.{module_name} import {import_targets}",
        "",
    ]

    fixture_block = _build_service_config_fixture(services).rstrip()
    if fixture_block:
        lines.append(fixture_block)
        lines.append("")

    lines.append(f"ACTOR_EMAIL = {json.dumps(actor_email)}")
    lines.append(f"ACTOR_PASSWORD = {json.dumps(actor_password)}")
    lines.append(f"EMAIL_FIELD = {repr(email_field)}")
    lines.append(f"PASSWORD_FIELD = {repr(password_field)}")
    lines.append(f"TOKEN_FIELDS = {repr(token_fields)}")
    lines.append(f"DEFAULT_PAGE = {default_page}")
    lines.append("")

    lines.append("def _story_credentials() -> Dict[str, str]:")
    lines.append("    payload: Dict[str, str] = {}")
    lines.append("    if EMAIL_FIELD:")
    lines.append("        payload[EMAIL_FIELD] = ACTOR_EMAIL")
    lines.append("    if PASSWORD_FIELD:")
    lines.append("        payload[PASSWORD_FIELD] = ACTOR_PASSWORD")
    lines.append("    return payload")
    lines.append("")

    lines.append("def _reset_service_client() -> None:")
    lines.append(f'    base_url = os.getenv("{env_prefix}_BASE_URL") or "offline://{env_prefix.lower()}"')
    lines.append(f"    {primary_class}.configure(")
    lines.append("        base_url,")
    lines.append("        credentials=None,")
    lines.append("        offline_fallback=True,")
    lines.append("    )")
    lines.append("")

    lines.append("def _extract_token(body: Any) -> str:")
    lines.append("    if isinstance(body, dict) and TOKEN_FIELDS:")
    lines.append("        for key in TOKEN_FIELDS:")
    lines.append("            token = body.get(key)")
    lines.append("            if token:")
    lines.append("                return token")
    lines.append("    raise AssertionError(\"Expected a token field in response body.\")")
    lines.append("")

    lines.append("def _build_auth_headers(token: str) -> Dict[str, str]:")
    lines.append("    if not token:")
    lines.append("        raise AssertionError(\"Saved token not available for protected call.\")")
    lines.append("    return {\"Authorization\": f\"Bearer {token}\"}")
    lines.append("")

    lines.append("def _register_and_login() -> Tuple[str, str]:")
    lines.append("    payload = _story_credentials()")
    lines.append(f"    register_response = {primary_class}.{register_method}({{'body': dict(payload)}})")
    lines.append(f"    register_response.status_should_be({register_success_status})")
    lines.append("    register_token = _extract_token(register_response.body)")
    lines.append(f"    login_response = {primary_class}.{login_method}({{'body': dict(payload)}})")
    lines.append(f"    login_response.status_should_be({login_success_status})")
    lines.append("    login_token = _extract_token(login_response.body)")
    lines.append("    return register_token, login_token")
    lines.append("")

    lines.append(f"def test_{scenario_identifier}():")
    lines.append("    _reset_service_client()")
    lines.append(f"    print({message_literal})")
    lines.append("    if not TOKEN_FIELDS:")
    lines.append("        pytest.skip('Token fields not inferred from spec; update the story or spec')")
    lines.append("    if not EMAIL_FIELD or not PASSWORD_FIELD:")
    lines.append("        pytest.skip('Auth fields not inferred from spec; update the story or spec')")
    lines.append("")
    lines.append("    register_token, login_token = _register_and_login()")
    lines.append("    assert isinstance(register_token, str) and register_token.strip()")
    lines.append("    assert isinstance(login_token, str) and login_token.strip()")
    lines.append("    assert login_token == register_token")
    lines.append("")
    lines.append("    auth_headers = _build_auth_headers(register_token)")
    lines.append(
        f"    fetch_response = {primary_class}.{fetch_method}({{'query': {{'page': DEFAULT_PAGE}}, 'headers': auth_headers}})"
    )
    lines.append(f"    fetch_response.status_should_be({fetch_success_status})")
    lines.append("    fetch_body = fetch_response.body")
    lines.append("    if not isinstance(fetch_body, dict):")
    lines.append("        raise AssertionError(\"Expected JSON body for protected data response.\")")
    lines.append("    if 'page' in fetch_body:")
    lines.append("        assert fetch_body['page'] == DEFAULT_PAGE")
    lines.append("    for field in TOKEN_FIELDS:")
    lines.append("        if field in fetch_body:")
    lines.append("            assert fetch_body[field] == register_token")
    lines.append("    records = None")
    lines.append("    for key in ('data', 'users', 'items', 'records', 'results'):")
    lines.append("        value = fetch_body.get(key)")
    lines.append("        if isinstance(value, list):")
    lines.append("            records = value")
    lines.append("            break")
    lines.append("    assert isinstance(records, list)")
    lines.append("    assert len(records) > 0")
    lines.append("    first_record = records[0]")
    lines.append("    assert isinstance(first_record, dict)")
    lines.append("    assert 'id' in first_record")
    lines.append("")

    lines.append("def test_flow_negative_FN_01_input_break():")
    lines.append("    _reset_service_client()")
    lines.append("    print(\"Flow negative: Registration fails when password is omitted\")")
    lines.append("    payload = {'body': {}}")
    lines.append("    if EMAIL_FIELD:")
    lines.append("        payload['body'][EMAIL_FIELD] = ACTOR_EMAIL")
    lines.append(
        f"    response = {primary_class}.{register_method}(payload)"
    )
    lines.append("    assert response.status_code in (400, 422)")
    lines.append("    body = response.body")
    lines.append("    assert body is not None")
    lines.append("    if isinstance(body, dict) and TOKEN_FIELDS:")
    lines.append("        for field in TOKEN_FIELDS:")
    lines.append("            assert field not in body")
    lines.append("")

    lines.append("def test_flow_negative_FN_02_input_break():")
    lines.append("    _reset_service_client()")
    lines.append("    print(\"Flow negative: Login fails when password is omitted\")")
    lines.append("    payload = {'body': {}}")
    lines.append("    if EMAIL_FIELD:")
    lines.append("        payload['body'][EMAIL_FIELD] = ACTOR_EMAIL")
    lines.append(
        f"    response = {primary_class}.{login_method}(payload)"
    )
    lines.append("    assert response.status_code in (400, 422)")
    lines.append("    body = response.body")
    lines.append("    assert body is not None")
    lines.append("    if isinstance(body, dict) and TOKEN_FIELDS:")
    lines.append("        for field in TOKEN_FIELDS:")
    lines.append("            assert field not in body")
    lines.append("")

    lines.append("def test_flow_negative_FN_03_dependency_break():")
    lines.append("    _reset_service_client()")
    lines.append("    print(\"Flow negative: Login fails when password is incorrect\")")
    lines.append("    setup_payload = {'body': dict(_story_credentials())}")
    lines.append(
        f"    setup_response = {primary_class}.{register_method}(setup_payload)"
    )
    lines.append(f"    setup_response.status_should_be({register_success_status})")
    invalid_password = _derive_invalid_value(password_field or "password", actor_password)
    lines.append(
        f"    wrong_login_payload = {{'body': {{{json.dumps(email_field)}: ACTOR_EMAIL, {json.dumps(password_field)}: {json.dumps(invalid_password)}}}}}"
    )
    lines.append(
        f"    response = {primary_class}.{login_method}(wrong_login_payload)"
    )
    lines.append("    assert response.status_code in (400, 401)")
    lines.append("    body = response.body")
    lines.append("    assert body is not None")
    lines.append("    if isinstance(body, dict) and TOKEN_FIELDS:")
    lines.append("        for field in TOKEN_FIELDS:")
    lines.append("            assert field not in body")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _build_tests_file_content(services: List[Dict[str, Any]], story: Optional[str], cases: List[Dict[str, Any]]) -> str:
    module_name = _FILE_NAME.replace(".py", "")
    class_names = _collect_service_classes(services)
    if not class_names:
        return '"""Auto-generated API tests."""\n'
    import_targets = ", ".join(class_names)

    lines: List[str] = [
        '"""Auto-generated API tests."""',
        "",
        "from __future__ import annotations",
        "",
    ]

    if story and cases:
        flow_payload = _generate_flow_negative_cases(story)
        specialized = _build_story_flow_suite(services, cases, story, flow_payload)
        if specialized:
            return specialized
        lines.extend([
            "import os",
            "",
            "import pytest",
            "",
            f"from pages.{module_name} import {import_targets}",
            "",
        ])
        lines.append(_build_service_config_fixture(services))
        lines.append(_build_story_test_case(cases, story, services))
        negative_story = _build_negative_story_test_case(cases, story, services, flow_payload)
        if negative_story:
            lines.append(negative_story)
    else:
        lines.extend([
            "import pytest",
            "",
            f"from pages.{module_name} import {import_targets}",
            "",
            "try:",
            "    from tests import test_data  # type: ignore",  # noqa: F401
            "except ImportError:  # pragma: no cover",
            "    test_data = None  # type: ignore",
            "",
        ])
        for service in services:
            for operation in service.get("operations", []):
                lines.append(_format_test_case(operation, story))

    return "\n".join(lines).rstrip() + "\n"


def _test_data_stub_content() -> str:
    return textwrap.dedent(
        '''
        """Project-specific API test data helpers.

        Edit this module to return payloads used by generated tests.
        """

        from __future__ import annotations

        from typing import Any, Dict


        class _TestData:
            def __getattr__(self, name: str):
                raise NotImplementedError(
                    f"Define test_data.{name}() to return request payloads for your tests."
                )


        test_data = _TestData()
        '''
    ).strip() + "\n"


def _build_file_content(services: List[Dict[str, Any]]) -> str:
    lines: List[str] = [
        '"""Auto-generated API test cases."""',
        "",
        "from __future__ import annotations",
        "",
        "import base64",
        "import json",
        "import os",
        "import uuid",
        "",
        "from typing import Any, Dict, List, Optional",
        "",
        "import requests",
        "",
        "",
        "class ApiResponse:",
        "    def __init__(self, response: requests.Response):",
        "        self.status_code = response.status_code",
        "        try:",
        "            self.body = response.json()",
        "        except ValueError:",
        "            self.body = response.text",
        "",
        "    def status_should_be(self, expected: int) -> \"ApiResponse\":",
        "        if self.status_code != expected:",
        "            raise AssertionError(f\"Expected status {expected}, got {self.status_code}\")",
        "        return self",
        "",
        "    def body_should_contain(self, expected_fragment: str) -> \"ApiResponse\":",
        "        haystack = self.body if isinstance(self.body, str) else repr(self.body)",
        "        if expected_fragment not in haystack:",
        "            raise AssertionError(f\"Expected body to contain {expected_fragment}\")",
        "        return self",
        "",
        "",
        "class ApiAuthError(RuntimeError):",
        "    \"\"\"Raised when authentication requirements cannot be satisfied.\"\"\"",
        "",
        "",
        "def _build_api_response(status_code: int, body: Any) -> ApiResponse:",
        "    response = requests.Response()",
        "    response.status_code = status_code",
        "    response.encoding = \"utf-8\"",
        "    if isinstance(body, (dict, list)):",
        "        response.headers[\"Content-Type\"] = \"application/json\"",
        "        response._content = json.dumps(body).encode(\"utf-8\")",
        "    else:",
        "        response._content = str(body).encode(\"utf-8\")",
        "    return ApiResponse(response)",
        "",
        "",
        "class _OfflineServiceClient:",
        "    def __init__(self) -> None:",
        "        self._token: Optional[str] = None",
        "        self._users: Dict[str, str] = {}",
        "        self._entities: Dict[str, Dict[str, Dict[str, Any]]] = {}",
        "",
        "    def request(",
        "        self,",
        "        method: str,",
        "        path: str,",
        "        *,",
        "        query: Optional[Dict[str, Any]] = None,",
        "        body: Optional[Dict[str, Any]] = None,",
        "        headers: Optional[Dict[str, Any]] = None,",
        "        security: Optional[List[Dict[str, Any]]] = None,",
        "        credential_overrides: Optional[Dict[str, Any]] = None,",
        "    ) -> ApiResponse:",
        "        del security, credential_overrides",
        "        verb = method.upper()",
        "        payload = dict(body or {})",
        "        header_copy = dict(headers or {})",
        "        query_copy = dict(query or {})",
        "        if verb == \"POST\" and path == \"/api/register\":",
        "            return self._handle_register(payload)",
        "        if verb == \"POST\" and path == \"/api/login\":",
        "            return self._handle_login(payload)",
        "        if verb == \"GET\" and path == \"/api/users\":",
        "            return self._handle_list_users(headers=header_copy, query=query_copy)",
        "        if verb in {\"POST\", \"PUT\", \"PATCH\"}:",
        "            upsert = self._handle_entity_upsert(path, payload)",
        "            if upsert is not None:",
        "                return upsert",
        "        if verb == \"GET\":",
        "            fetched = self._handle_entity_get(path)",
        "            if fetched is not None:",
        "                return fetched",
        "        if verb == \"DELETE\":",
        "            deleted = self._handle_entity_delete(path)",
        "            if deleted is not None:",
        "                return deleted",
        "        return _build_api_response(200, {",
        "            \"offline\": True,",
        "            \"method\": verb,",
        "            \"path\": path,",
        "            \"body\": payload,",
        "        })",
        "",
        "    def _handle_register(self, payload: Dict[str, Any]) -> ApiResponse:",
        "        email = payload.get(\"email\")",
        "        password = payload.get(\"password\")",
        "        if not email or not password:",
        "            return _build_api_response(400, {\"error\": \"Missing required credentials.\"})",
        "        if self._token is None:",
        "            self._token = uuid.uuid4().hex",
        "        self._users[email] = password",
        "        response_body: Dict[str, Any] = {\"token\": self._token}",
        "        for key in (\"name\", \"status\", \"id\"):",
        "            if key in payload:",
        "                response_body[key] = payload[key]",
        "        return _build_api_response(200, response_body)",
        "",
        "    @staticmethod",
        "    def _normalize_resource(token: str) -> str:",
        "        value = (token or \"\").strip().strip(\"/\")",
        "        if value.lower() in {\"v1\", \"v2\", \"v3\"}:",
        "            return \"\"",
        "        return value",
        "",
        "    @staticmethod",
        "    def _extract_path_parts(path: str) -> tuple[Optional[str], Optional[str]]:",
        "        parts = [p for p in (path or \"\").split(\"/\") if p]",
        "        if not parts:",
        "            return None, None",
        "        if parts[0].lower() in {\"v1\", \"v2\", \"v3\"}:",
        "            parts = parts[1:]",
        "        if not parts:",
        "            return None, None",
        "        resource = parts[0]",
        "        entity_id = parts[1] if len(parts) > 1 else None",
        "        return resource, entity_id",
        "",
        "    @staticmethod",
        "    def _extract_payload_id(payload: Dict[str, Any], resource: Optional[str]) -> Any:",
        "        if not isinstance(payload, dict):",
        "            return None",
        "        if \"id\" in payload:",
        "            return payload.get(\"id\")",
        "        resource_token = (resource or \"\").strip().lower()",
        "        if resource_token:",
        "            for key in (f\"{resource_token}Id\", f\"{resource_token}_id\"):",
        "                if key in payload:",
        "                    return payload.get(key)",
        "        for key, value in payload.items():",
        "            if str(key).lower().endswith(\"id\") and value not in (None, \"\"):",
        "                return value",
        "        return None",
        "",
        "    def _entity_store(self, resource: str) -> Dict[str, Dict[str, Any]]:",
        "        if resource not in self._entities:",
        "            self._entities[resource] = {}",
        "        return self._entities[resource]",
        "",
        "    def _handle_entity_upsert(self, path: str, payload: Dict[str, Any]) -> Optional[ApiResponse]:",
        "        resource, path_id = self._extract_path_parts(path)",
        "        resource = self._normalize_resource(resource or \"\")",
        "        if not resource:",
        "            return None",
        "        entity_id = self._extract_payload_id(payload, resource) or path_id",
        "        if entity_id in (None, \"\"):",
        "            return _build_api_response(400, {\"error\": \"Missing id.\"})",
        "        store = self._entity_store(resource)",
        "        entity = dict(store.get(str(entity_id), {}))",
        "        entity.update(payload or {})",
        "        entity[\"id\"] = entity_id",
        "        store[str(entity_id)] = entity",
        "        return _build_api_response(200, entity)",
        "",
        "    def _handle_entity_get(self, path: str) -> Optional[ApiResponse]:",
        "        resource, entity_id = self._extract_path_parts(path)",
        "        resource = self._normalize_resource(resource or \"\")",
        "        if not resource:",
        "            return None",
        "        store = self._entity_store(resource)",
        "        if entity_id:",
        "            entity = store.get(str(entity_id))",
        "            if not entity:",
        "                return _build_api_response(404, {\"error\": f\"{resource} not found.\"})",
        "            return _build_api_response(200, entity)",
        "        return _build_api_response(200, list(store.values()))",
        "",
        "    def _handle_entity_delete(self, path: str) -> Optional[ApiResponse]:",
        "        resource, entity_id = self._extract_path_parts(path)",
        "        resource = self._normalize_resource(resource or \"\")",
        "        if not resource or not entity_id:",
        "            return None",
        "        store = self._entity_store(resource)",
        "        existed = store.pop(str(entity_id), None)",
        "        if existed is None:",
        "            return _build_api_response(404, {\"error\": f\"{resource} not found.\"})",
        "        return _build_api_response(200, {\"deleted\": True, \"id\": entity_id})",
        "",
        "    def _handle_login(self, payload: Dict[str, Any]) -> ApiResponse:",
        "        email = payload.get(\"email\")",
        "        password = payload.get(\"password\")",
        "        if not email or not password:",
        "            return _build_api_response(400, {\"error\": \"Missing login credentials.\"})",
        "        stored_password = self._users.get(email)",
        "        if stored_password is None:",
        "            return _build_api_response(400, {\"error\": \"User not registered.\"})",
        "        if stored_password != password:",
        "            return _build_api_response(400, {\"error\": \"Invalid login credentials.\"})",
        "        if self._token is None:",
        "            self._token = uuid.uuid4().hex",
        "        return _build_api_response(200, {\"token\": self._token})",
        "",
        "    def _handle_list_users(",
        "        self,",
        "        *,",
        "        headers: Optional[Dict[str, Any]] = None,",
        "        query: Optional[Dict[str, Any]] = None,",
        "    ) -> ApiResponse:",
        "        del query",
        "        if self._token is None:",
        "            return _build_api_response(401, {\"error\": \"Token not registered.\"})",
        "        header_map = dict(headers or {})",
        "        auth_header = header_map.get(\"Authorization\")",
        "        if not auth_header:",
        "            return _build_api_response(401, {\"error\": \"Authorization header required.\"})",
        "        expected_header = f\"Bearer {self._token}\"",
        "        if auth_header != expected_header:",
        "            return _build_api_response(403, {\"error\": \"Invalid token provided.\"})",
        "        token = self._token",
        "        response_body = {",
        "            \"token\": token,",
        "            \"registered_token\": token,",
        "            \"users\": [",
        "                {\"id\": 1, \"email\": \"eve.holt@reqres.in\"},",
        "                {\"id\": 2, \"email\": \"janet.weaver@reqres.in\"},",
        "            ],",
        "        }",
        "        return _build_api_response(200, response_body)",
        "",
        "",
        "class ServiceClient:",
        "    def __init__(",
        "        self,",
        "        base_url: str,",
        "        *,",
        "        auth_schemes: Optional[Dict[str, Any]] = None,",
        "        default_security: Optional[List[Dict[str, Any]]] = None,",
        "        credentials: Optional[Dict[str, Any]] = None,",
        "        timeout: int = 30,",
        "    ):",
        "        self.base_url = base_url.rstrip(\"/\")",
        "        self.timeout = timeout",
        "        self.session = requests.Session()",
        "        self.session.headers.update({\"Accept\": \"application/json\"})",
        "        self.auth_schemes = dict(auth_schemes or {})",
        "        self.default_security = list(default_security or [])",
        "        self.credentials = dict(credentials or {})",
        "",
        "    def set_credentials(self, credentials: Optional[Dict[str, Any]]) -> None:",
        "        self.credentials = dict(credentials or {})",
        "",
        "    def request(",
        "        self,",
        "        method: str,",
        "        path: str,",
        "        *,",
        "        query: Optional[Dict[str, Any]] = None,",
        "        body: Optional[Dict[str, Any]] = None,",
        "        headers: Optional[Dict[str, Any]] = None,",
        "        security: Optional[List[Dict[str, Any]]] = None,",
        "        credential_overrides: Optional[Dict[str, Any]] = None,",
        "    ) -> ApiResponse:",
        "        url = f\"{self.base_url}{path}\"",
        "        query_params = dict(query or {})",
        "        header_params = dict(headers or {})",
        "        self._apply_auth(header_params, query_params, security, credential_overrides)",
        "        response = self.session.request(",
        "            method=method.upper(),",
        "            url=url,",
        "            params=query_params or None,",
        "            json=body,",
        "            headers=header_params or None,",
        "            timeout=self.timeout,",
        "        )",
        "        return ApiResponse(response)",
        "",
        "    def _apply_auth(",
        "        self,",
        "        headers: Dict[str, Any],",
        "        query: Dict[str, Any],",
        "        security: Optional[List[Dict[str, Any]]],",
        "        credential_overrides: Optional[Dict[str, Any]],",
        "    ) -> None:",
        "        requirements = security if security is not None else self.default_security",
        "        if not requirements:",
        "            return",
        "        errors: List[str] = []",
        "        for requirement in requirements:",
        "            candidate = requirement if isinstance(requirement, dict) else {}",
        "            try:",
        "                self._satisfy_requirement(headers, query, candidate, credential_overrides)",
        "                return",
        "            except ApiAuthError as exc:",
        "                errors.append(str(exc))",
        "        if errors:",
        "            raise ApiAuthError('; '.join(sorted({error for error in errors if error})))",
        "        raise ApiAuthError('Unable to satisfy security requirements for request.')",
        "",
        "    def _satisfy_requirement(",
        "        self,",
        "        headers: Dict[str, Any],",
        "        query: Dict[str, Any],",
        "        requirement: Dict[str, Any],",
        "        credential_overrides: Optional[Dict[str, Any]],",
        "    ) -> None:",
        "        if not requirement:",
        "            return",
        "        for scheme_name, scopes in requirement.items():",
        "            scheme = self.auth_schemes.get(scheme_name)",
        "            if not scheme:",
        "                raise ApiAuthError(f\"Auth scheme '{scheme_name}' is not defined.\")",
        "            credential = self._resolve_credential(scheme_name, credential_overrides)",
        "            if credential is None:",
        "                # Skip injection when credentials are not available; public endpoints may allow anonymous access.",
        "                continue",
        "            self._inject_auth(headers, query, scheme_name, scheme, credential, scopes)",
        "",
        "    def _resolve_credential(",
        "        self,",
        "        scheme_name: str,",
        "        credential_overrides: Optional[Dict[str, Any]],",
        "    ) -> Optional[Any]:",
        "        overrides = credential_overrides or {}",
        "        if scheme_name in overrides and overrides[scheme_name]:",
        "            return overrides[scheme_name]",
        "        lowered = scheme_name.lower()",
        "        if lowered in overrides and overrides[lowered]:",
        "            return overrides[lowered]",
        "        if scheme_name in self.credentials and self.credentials[scheme_name]:",
        "            return self.credentials[scheme_name]",
        "        if lowered in self.credentials and self.credentials[lowered]:",
        "            return self.credentials[lowered]",
        "        return None",
        "",
        "    def _inject_auth(",
        "        self,",
        "        headers: Dict[str, Any],",
        "        query: Dict[str, Any],",
        "        scheme_name: str,",
        "        scheme: Dict[str, Any],",
        "        credential: Any,",
        "        scopes: Any,",
        "    ) -> None:",
        "        scheme_type = (scheme.get('type') or '').lower()",
        "        if scheme_type == 'apikey':",
        "            name = scheme.get('name')",
        "            location = (scheme.get('in') or 'header').lower()",
        "            if not name:",
        "                raise ApiAuthError(f\"API key scheme '{scheme_name}' is missing a name.\")",
        "            value = credential",
        "            if isinstance(credential, dict):",
        "                value = credential.get('token') or credential.get('value')",
        "            if isinstance(credential, (list, tuple)):",
        "                value = credential[0] if credential else None",
        "            if value in (None, ''):",
        "                raise ApiAuthError(f\"API key value for '{scheme_name}' is not provided.\")",
        "            if location == 'query':",
        "                query[name] = value",
        "            elif location == 'cookie':",
        "                fragment = f\"{name}={value}\"",
        "                cookie = headers.get('Cookie')",
        "                headers['Cookie'] = f\"{cookie}; {fragment}\" if cookie else fragment",
        "            else:",
        "                headers[name] = value",
        "            return",
        "        if scheme_type == 'http':",
        "            http_scheme = (scheme.get('scheme') or '').lower()",
        "            if http_scheme == 'basic':",
        "                username = None",
        "                password = None",
        "                if isinstance(credential, dict):",
        "                    username = credential.get('username') or credential.get('user')",
        "                    password = credential.get('password') or credential.get('pass')",
        "                elif isinstance(credential, (tuple, list)) and len(credential) == 2:",
        "                    username, password = credential",
        "                if username in (None, '') or password in (None, ''):",
        "                    raise ApiAuthError(f\"Basic auth credentials for '{scheme_name}' require username and password.\")",
        "                token = base64.b64encode(f\"{username}:{password}\".encode('utf-8')).decode('ascii')",
        "                headers['Authorization'] = f\"Basic {token}\"",
        "                return",
        "            if http_scheme == 'bearer':",
        "                token = credential",
        "                if isinstance(credential, dict):",
        "                    token = credential.get('token') or credential.get('access_token')",
        "                if isinstance(credential, (list, tuple)):",
        "                    token = credential[0] if credential else None",
        "                if token in (None, ''):",
        "                    raise ApiAuthError(f\"Bearer token for '{scheme_name}' is not provided.\")",
        "                headers['Authorization'] = f\"Bearer {token}\"",
        "                return",
        "            raise ApiAuthError(f\"HTTP auth scheme '{http_scheme}' is not supported.\")",
        "        if scheme_type in {'oauth2', 'openidconnect'}:",
        "            token = credential",
        "            if isinstance(credential, dict):",
        "                token = credential.get('access_token') or credential.get('token')",
        "            if isinstance(credential, (list, tuple)):",
        "                token = credential[0] if credential else None",
        "            if token in (None, ''):",
        "                raise ApiAuthError(f\"OAuth token for '{scheme_name}' is not provided.\")",
        "            headers['Authorization'] = f\"Bearer {token}\"",
        "            return",
        "        raise ApiAuthError(f\"Auth scheme type '{scheme_type}' is not supported.\")",
    ]

    for index, service in enumerate(services):
        if index:
            lines.append("")
        lines.extend(_build_service_class_lines(service))

    return "\n".join(lines).rstrip() + "\n"


@router.post("/api-tests/generate-page-methods-prompt")
def generate_api_page_methods_prompt(
    payload: Optional[ApiTestsRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payload = payload or ApiTestsRequest()
    project = _ensure_active_project(db, current_user)
    ctx = _load_project_spec_context(db, project.id)

    if not ctx.all_specs:
        raise HTTPException(status_code=404, detail="No API specifications available for the active project.")

    normalized_keys = {
        _normalize_identifier(value)
        for value in (payload.keys or [])
        if isinstance(value, str) and value.strip()
    }
    normalized_keys = normalized_keys or None

    service_filter = {
        value
        for value in (payload.services or [])
        if isinstance(value, str) and value.strip()
    }
    service_filter = service_filter or None

    base_specs = _select_specs(ctx, normalized_keys, service_filter)
    if not base_specs:
        raise HTTPException(status_code=404, detail="No API specifications matched the provided filters.")

    example_map = {
        _normalize_identifier(key): value
        for key, value in (payload.examples or {}).items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key.strip()
        and value.strip()
    }

    cases: List[Dict[str, Any]] = []
    for spec in base_specs:
        service_method_names = ctx.service_method_maps.get(spec.service_name, {})
        alias = example_map.get(_normalize_identifier(spec.key))
        case_details = _build_snippet_payload(
            spec,
            payload.story,
            alias,
            service_method_names,
            ctx.test_data_methods,
        )
        cases.append(case_details)

    services = _group_cases(cases)
    prompts_payload: List[Dict[str, Any]] = []

    for service in services:
        operations = service.get("operations") or []
        seen_pairs: Set[Tuple[str, str]] = set()
        endpoints: List[Tuple[str, str]] = []
        for operation in operations:
            method = (operation.get("http_method") or "GET").upper()
            path = operation.get("path") or "/"
            pair = (method, path)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            endpoints.append(pair)

        if not endpoints:
            continue

        class_name = service.get("class_name") or "ApiService"
        service_name = service.get("service") or class_name
        auth_description = _describe_auth_mechanism(service)

        prompt_text = build_api_page_methods_prompt(
            service_name=service_name,
            endpoints=endpoints,
            auth_mechanism=auth_description,
            class_name=class_name,
        )

        prompts_payload.append(
            {
                "service": service_name,
                "class_name": class_name,
                "auth_description": auth_description,
                "endpoint_count": len(endpoints),
                "prompt": prompt_text,
            }
        )

    if not prompts_payload:
        raise HTTPException(status_code=404, detail="No API endpoints available to build a SmartAI prompt.")

    return {
        "project_id": project.id,
        "count": len(prompts_payload),
        "prompts": prompts_payload,
    }


@router.post("/api-tests/generate-flow-negative-prompt")
def generate_flow_negative_prompt(
    payload: Optional[ApiTestsRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payload = payload or ApiTestsRequest()
    project = _ensure_active_project(db, current_user)
    story_text = (payload.story or "").strip()
    if not story_text:
        raise HTTPException(status_code=400, detail="Story content is required to build a flow-negative prompt.")
    prompt_text = build_flow_negative_cases_prompt(story_text)
    return {
        "project_id": project.id,
        "prompt": prompt_text,
    }


@router.post("/api-tests/generate-page-file")
def generate_api_test_page_file(
    payload: Optional[ApiTestsRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payload = payload or ApiTestsRequest()
    project = _ensure_active_project(db, current_user)
    context = _load_project_spec_context(db, project.id)

    if not context.all_specs:
        raise HTTPException(status_code=404, detail="No API specifications available for the active project.")

    normalized_keys = {
        _normalize_identifier(value)
        for value in (payload.keys or [])
        if isinstance(value, str) and value.strip()
    }
    service_filter = {
        value
        for value in (payload.services or [])
        if isinstance(value, str) and value.strip()
    }

    base_specs = _select_specs(context, normalized_keys, service_filter)
    if not base_specs:
        raise HTTPException(status_code=404, detail="No API specifications matched the provided filters.")

    example_map = {
        _normalize_identifier(key): value
        for key, value in (payload.examples or {}).items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key.strip()
        and value.strip()
    }

    story_text = (payload.story or "").strip()

    cases: List[Dict[str, Any]] = []
    case_by_key: Dict[str, Dict[str, Any]] = {}
    for spec in base_specs:
        normalized_key = _normalize_identifier(spec.key)
        base_details = _build_snippet_payload(
            spec,
            payload.story,
            example_map.get(normalized_key),
            context.service_method_maps.get(spec.service_name, {}),
            context.test_data_methods,
        )
        chain_snippet = _format_chain_snippet(base_details)
        case = {
            **base_details,
            "snippet": chain_snippet,
            "story": story_text or None,
        }
        cases.append(case)
        case_by_key[normalized_key] = case

    services = _group_cases(cases)
    file_content = _build_file_content(services)

    project_root = _project_root(project).resolve()
    src_dir = project_root / "generated_runs" / "src"
    pages_dir = src_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    target_file = pages_dir / _FILE_NAME
    target_file.write_text(file_content, encoding="utf-8")

    credentials_target_dir = src_dir
    credentials_file = credentials_target_dir / "api_credentials.txt"
    credentials_content = _build_credentials_text_content(services)
    base_url_lines = _build_base_url_credentials(services)
    if base_url_lines:
        existing = credentials_content or ""
        for line in base_url_lines:
            if line in existing:
                continue
            existing = existing.rstrip() + ("\n" if existing else "") + line
        credentials_content = existing.rstrip() + "\n"
    credentials_file.write_text(credentials_content, encoding="utf-8")

    auth_dir = src_dir / "config"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file = auth_dir / "api_auth_metadata.json"
    auth_metadata = _build_auth_metadata(services)
    auth_json = json.dumps(auth_metadata, indent=2, sort_keys=True)
    auth_file.write_text(f"{auth_json}\n", encoding="utf-8")

    storage = DatabaseBackedProjectStorage(project, src_dir, db)
    relative_path = target_file.relative_to(src_dir).as_posix()
    storage.write_file(relative_path, file_content, "utf-8")

    credentials_relative_path = credentials_file.relative_to(src_dir).as_posix()
    storage.write_file(credentials_relative_path, credentials_content, "utf-8")

    auth_relative_path = auth_file.relative_to(src_dir).as_posix()
    storage.write_file(auth_relative_path, f"{auth_json}\n", "utf-8")

    generated_files: List[Dict[str, Any]] = [
        {
            "path": relative_path,
            "label": "api_page",
            "content": file_content,
        },
        {
            "path": credentials_relative_path,
            "label": "credentials",
            "content": credentials_content,
        },
        {
            "path": auth_relative_path,
            "label": "auth_metadata",
            "content": f"{auth_json}\n",
        },
    ]

    return {
        "status": "generated",
        "project_id": project.id,
        "file_path": relative_path,
        "credentials_file_path": credentials_relative_path,
        "auth_metadata_path": auth_relative_path,
        "filters": {
            "keys": sorted(normalized_keys) if normalized_keys else None,
            "services": sorted(service_filter) if service_filter else None,
        },
        "services": services,
        "content": file_content,
        "credentials_content": credentials_content,
        "auth_metadata": auth_metadata,
        "generated_files": generated_files,
    }


@router.get("/project-files")
def read_project_file(
    path: str = Query(..., description="Relative path under generated_runs/src to read."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cleaned = (path or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="File path is required.")

    project = _ensure_active_project(db, current_user)
    project_root = _project_root(project).resolve()
    src_dir = (project_root / "generated_runs" / "src").resolve()

    absolute_path = (src_dir / cleaned).resolve()
    try:
        absolute_path.relative_to(src_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Path must remain inside the project workspace.") from exc

    if not absolute_path.exists() or not absolute_path.is_file():
        raise HTTPException(status_code=404, detail="Requested file was not found.")

    storage = DatabaseBackedProjectStorage(project, src_dir, db)
    data = storage.read_file(cleaned, absolute_path)
    return {
        "path": data.path,
        "content": data.content,
        "encoding": data.encoding,
        "source": data.source,
    }
