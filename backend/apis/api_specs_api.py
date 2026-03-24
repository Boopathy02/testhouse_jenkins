from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
import re
import copy
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple, NamedTuple, Set

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict
from sqlalchemy.orm import Session

from apis.projects_api import get_current_user
from database.models import ApiSpec, Project, User
from database.session import get_db, engine
from utils.project_context import current_project_id


router = APIRouter()

_VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _alembic_config():
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[1]
    alembic_ini = backend_root / "database" / "alembic.ini"
    migrations_dir = backend_root / "database" / "migrations"

    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(migrations_dir))
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        config.set_main_option("sqlalchemy.url", db_url)
    return config


def _validate_api_service_item(item: Any) -> ApiServiceSpec:
    try:
        return ApiServiceSpec.model_validate(item)
    except Exception as exc:  # capture pydantic validation errors and others
        preview = None
        try:
            if isinstance(item, dict):
                preview = {k: (type(v).__name__ if not isinstance(v, (str, int, float, bool, list, dict)) else v) for k, v in list(item.items())[:10]}
            else:
                preview = str(type(item))
        except Exception:
            preview = "<unavailable>"
        raise HTTPException(status_code=400, detail=f"ApiServiceSpec validation failed: {exc}. Payload preview: {preview}")


def _normalize_identifier(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_") or "api"


def _safe_token(value: str) -> str:
    return re.sub(r"\W+", "_", (value or "").strip().lower()).strip("_") or "item"


def _normalize_service_class_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return "ApiService"
    normalized = re.sub(r"[^0-9a-zA-Z]+", " ", cleaned)
    parts = [part for part in normalized.split() if part]
    if not parts:
        token = re.sub(r"[^0-9a-zA-Z]+", "", cleaned)
        return token.capitalize() or "ApiService"
    if len(parts) == 1:
        token = re.sub(r"[^0-9a-zA-Z]+", "", cleaned)
        return token[:1].upper() + token[1:] if token else parts[0].capitalize()
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _infer_resource_name(operation_name: str) -> str:
    base = _safe_token(operation_name)
    prefixes = (
        "create_",
        "update_",
        "delete_",
        "get_",
        "list_",
        "fetch_",
        "put_",
        "post_",
        "patch_",
    )
    for prefix in prefixes:
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return base or "payload"


def _extract_path_params_from_path(path: str) -> List[str]:
    if not path:
        return []
    return [match for match in re.findall(r"{([^}]+)}", path) if match]


def _service_method_map(service_specs: List[ApiSpec]) -> Dict[str, str]:
    counts: Dict[str, int] = {}
    mapping: Dict[str, str] = {}
    for spec in service_specs:
        base = _safe_token(spec.operation_name or spec.key)
        count = counts.get(base, 0) + 1
        counts[base] = count
        mapping[spec.key] = base if count == 1 else f"{base}_{count}"
    return mapping


def _test_data_method_map(specs: List[ApiSpec]) -> Dict[str, str]:
    example_specs = [spec for spec in specs if spec.examples]
    resource_map: Dict[str, List[ApiSpec]] = defaultdict(list)
    for spec in example_specs:
        resource_map[_infer_resource_name(spec.operation_name or spec.key)].append(spec)

    counts: Dict[str, int] = {}
    mapping: Dict[str, str] = {}
    for spec in example_specs:
        resource = _infer_resource_name(spec.operation_name or spec.key)
        base = resource if len(resource_map[resource]) == 1 else _safe_token(spec.operation_name or spec.key)
        count = counts.get(base, 0) + 1
        counts[base] = count
        mapping[spec.key] = base if count == 1 else f"{base}_{count}"
    return mapping


class _ProjectSpecContext(NamedTuple):
    all_specs: List[ApiSpec]
    by_key: Dict[str, ApiSpec]
    by_service: Dict[str, List[ApiSpec]]
    service_method_maps: Dict[str, Dict[str, str]]
    test_data_methods: Dict[str, str]


def _load_project_spec_context(db: Session, project_id: int) -> _ProjectSpecContext:
    all_specs = (
        db.query(ApiSpec)
        .filter(ApiSpec.project_id == project_id)
        .order_by(ApiSpec.service_name.asc(), ApiSpec.operation_name.asc())
        .all()
    )

    by_key: Dict[str, ApiSpec] = {}
    by_service: Dict[str, List[ApiSpec]] = defaultdict(list)
    for spec in all_specs:
        normalized_key = _normalize_identifier(spec.key)
        by_key[normalized_key] = spec
        by_service[spec.service_name].append(spec)

    service_method_maps = {service: _service_method_map(specs) for service, specs in by_service.items()}
    test_data_methods = _test_data_method_map(all_specs)

    return _ProjectSpecContext(
        all_specs=all_specs,
        by_key=by_key,
        by_service=dict(by_service),
        service_method_maps=service_method_maps,
        test_data_methods=test_data_methods,
    )


def _select_specs(
    context: _ProjectSpecContext,
    normalized_keys: Optional[Set[str]],
    service_filter: Optional[Set[str]],
) -> List[ApiSpec]:
    specs: List[ApiSpec]
    if normalized_keys:
        specs = [context.by_key[key] for key in normalized_keys if key in context.by_key]
    else:
        specs = list(context.all_specs)

    if service_filter:
        specs = [spec for spec in specs if spec.service_name in service_filter]

    return specs


class ApiExampleModel(BaseModel):
    alias: str = Field(..., min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("alias")
    @classmethod
    def _alias_not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("alias must not be blank")
        return cleaned


class ApiResponseContract(BaseModel):
    expected_status: int = Field(default=200, ge=100, le=599)
    body_contains: List[str] = Field(default_factory=list)
    jsonpath: Dict[str, Any] = Field(default_factory=dict)


class ApiOperationModel(BaseModel):
    name: str = Field(..., min_length=1)
    method: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    description: Optional[str] = None
    headers: Dict[str, Any] = Field(default_factory=dict)
    query: Dict[str, Any] = Field(default_factory=dict)
    request_body: Dict[str, Any] = Field(default_factory=dict)
    response: ApiResponseContract = Field(default_factory=ApiResponseContract)
    examples: List[ApiExampleModel] = Field(default_factory=list)
    security: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("operation name must not be blank")
        return cleaned

    @field_validator("method")
    @classmethod
    def _method_supported(cls, value: str) -> str:
        method = (value or "").strip().upper()
        if method not in _VALID_METHODS:
            raise ValueError(f"Unsupported method '{value}'. Allowed: {', '.join(sorted(_VALID_METHODS))}")
        return method

    @field_validator("path")
    @classmethod
    def _path_not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("path must not be blank")
        return cleaned


class ApiServiceSpec(BaseModel):
    service: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    auth: Dict[str, Any] = Field(default_factory=dict)
    operations: List[ApiOperationModel] = Field(default_factory=list)

    @field_validator("service")
    @classmethod
    def _service_not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("service name must not be blank")
        return cleaned

    @field_validator("base_url")
    @classmethod
    def _base_url_not_blank(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("base_url must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _ensure_operations(self) -> "ApiServiceSpec":
        if not self.operations:
            raise ValueError("operations must not be empty")
        return self


class ApiTestSnippetRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    story: Optional[str] = None
    user_story: Optional[str] = Field(default=None, alias="user_story")
    key: Optional[str] = None
    service: Optional[str] = None
    operation: Optional[str] = None
    example_alias: Optional[str] = None

    @model_validator(mode="after")
    def _ensure_target(self) -> "ApiTestSnippetRequest":
        if not self.story and self.user_story:
            candidate = (self.user_story or "").strip()
            self.story = candidate or None
        if not self.key and not (self.service and self.operation):
            raise ValueError("Provide either 'key' or both 'service' and 'operation'.")
        return self


class ApiTestsRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    story: Optional[str] = None
    user_story: Optional[str] = Field(default=None, alias="user_story")
    keys: Optional[List[str]] = None
    services: Optional[List[str]] = None
    examples: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sanitize(self) -> "ApiTestsRequest":
        if not self.story and self.user_story:
            candidate = (self.user_story or "").strip()
            self.story = candidate or None
        if self.keys:
            cleaned_keys = [value.strip() for value in self.keys if isinstance(value, str) and value.strip()]
            self.keys = list(dict.fromkeys(cleaned_keys)) or None
        if self.services:
            cleaned_services = [value.strip() for value in self.services if isinstance(value, str) and value.strip()]
            self.services = list(dict.fromkeys(cleaned_services)) or None
        if not self.keys:
            self.keys = None
        if not self.services:
            self.services = None
        return self


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


def _tokenize(value: str) -> Set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", (value or "").lower()) if token}


def _story_lines(story: Optional[str]) -> List[str]:
    lines: List[str] = []
    for raw in (story or "").splitlines():
        text = raw.strip()
        if text:
            lines.append(text)
    return lines


_STORY_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_STORY_PASSWORD_PATTERN = re.compile(r"password[^A-Za-z0-9]*[:=]?\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_STORY_PAGE_PATTERN = re.compile(r"page[^0-9]*(\d+)", re.IGNORECASE)


def _extract_story_actor_details(story: Optional[str]) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    text = (story or "").strip()
    if not text:
        return details

    email_match = _STORY_EMAIL_PATTERN.search(text)
    if email_match:
        details["email"] = email_match.group(0)

    password_match = _STORY_PASSWORD_PATTERN.search(text)
    if password_match:
        details["password"] = password_match.group(1)

    page_match = _STORY_PAGE_PATTERN.search(text)
    if page_match:
        try:
            details["page"] = int(page_match.group(1))
        except ValueError:
            pass

    return details


def _partition_story_steps(doc_lines: List[str]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in doc_lines:
        lowered = line.lower()
        if lowered.startswith("when "):
            current = {"when": line, "assertions": [], "details": []}
            blocks.append(current)
        elif current is not None and lowered.startswith(("then ", "and ")):
            current["assertions"].append(line)
        elif current is not None:
            current.setdefault("details", []).append(line)
    return blocks


def _spec_keyword_tokens(spec: ApiSpec) -> Set[str]:
    tokens = _tokenize(getattr(spec, "operation_name", "") or "")
    tokens |= _tokenize(getattr(spec, "path", "") or "")
    method = (getattr(spec, "http_method", "") or "").upper()
    tokens |= _METHOD_KEYWORDS.get(method, set())
    return {token for token in tokens if token and token not in _COMMON_STORY_STOPWORDS}


def _score_token_overlap(line_tokens: Set[str], spec_tokens: Set[str]) -> int:
    matches: Set[str] = set()
    for token in line_tokens:
        for spec_token in spec_tokens:
            if token == spec_token or token.startswith(spec_token) or spec_token.startswith(token):
                matches.add(spec_token)
                break
    return len(matches)


def _match_story_line_to_spec(line: str, specs: List[ApiSpec]) -> Optional[ApiSpec]:
    tokens = _tokenize(line)
    if not tokens:
        return None

    lowered_line = line.lower()
    prefer_get = any(fragment in lowered_line for fragment in [" retrieve", " retrieves", " fetch", " fetches", " get ", " look up", " looks up"])
    prefer_delete = any(fragment in lowered_line for fragment in [" delete", " deletes", " removing", " remove", " cleanup", " clean up"])
    prefer_create = any(fragment in lowered_line for fragment in [" create", " creates", " add ", " adds", " post "])
    prefer_update = any(fragment in lowered_line for fragment in [" update", " updates", " modify", " change", " patch"])
    mentions_status = "status" in lowered_line
    mentions_upload = "upload" in lowered_line or "file" in lowered_line
    mentions_id = " id " in f" {lowered_line} " or "with id" in lowered_line
    numeric_tokens = {token for token in tokens if token.isdigit()}

    best_spec = None
    best_score = 0

    for spec in specs:
        spec_tokens = _spec_keyword_tokens(spec)
        if not spec_tokens:
            continue

        score = _score_token_overlap(tokens, spec_tokens)
        http_method = (getattr(spec, "http_method", "") or "").upper()
        operation_name = (getattr(spec, "operation_name", "") or "").lower()
        path = getattr(spec, "path", "") or ""
        path_params = _extract_path_params_from_path(path)
        has_id_param = any((param or "").lower().endswith("id") for param in path_params)
        has_path_params = bool(path_params)
        path_mentions_status = "status" in path.lower()

        if prefer_get:
            if http_method in {"GET", "HEAD"}:
                score += 6
            else:
                score -= 4
            if any(keyword in operation_name for keyword in ["get", "find", "list", "retrieve"]):
                score += 3
            if "delete" in operation_name or "update" in operation_name:
                score -= 5
            if path_mentions_status and not mentions_status:
                score -= 3
        elif prefer_delete:
            if http_method == "DELETE":
                score += 5
            else:
                score -= 4
            if "delete" in operation_name or "remove" in operation_name:
                score += 2
        elif prefer_create:
            if http_method == "POST":
                score += 5
            else:
                score -= 3
            if any(keyword in operation_name for keyword in ["create", "add", "post"]):
                score += 3
            if has_path_params:
                score -= 2
        elif prefer_update:
            if http_method in {"PUT", "PATCH"}:
                score += 6
            elif http_method == "POST":
                score -= 3
            if any(keyword in operation_name for keyword in ["update", "modify", "change", "patch", "set"]):
                score += 3

        if mentions_upload and "upload" in operation_name:
            score += 2
        if mentions_status and ("status" in operation_name or path_mentions_status):
            score += 2
        if not mentions_status and path_mentions_status:
            score -= 2

        if mentions_id or numeric_tokens:
            if has_id_param:
                score += 6
            elif has_path_params:
                score += 3
            else:
                score -= 2
        elif has_id_param:
            score += 1

        if operation_name.startswith("find") and not mentions_status and not prefer_get:
            score -= 1

        if score > best_score:
            best_score = score
            best_spec = spec

    return best_spec if best_score > 0 else None


def _match_story_blocks_to_specs(blocks: List[Dict[str, Any]], specs: List[ApiSpec]) -> List[str]:
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


def _coerce_services(payload: Any) -> List[ApiServiceSpec]:
    if isinstance(payload, dict):
        if "swagger" in payload or "openapi" in payload:
            converted = _convert_openapi_document(payload)
            return [_validate_api_service_item(item) for item in converted]
        postman_root = _find_postman_root(payload)
        if postman_root:
            converted = _convert_postman_collection(postman_root)
            return [_validate_api_service_item(item) for item in converted]
        if "services" in payload and isinstance(payload["services"], list):
            return [_validate_api_service_item(item) for item in payload["services"]]
        return [_validate_api_service_item(payload)]
    if isinstance(payload, list):
        return [_validate_api_service_item(item) for item in payload]
    raise HTTPException(status_code=400, detail="Unsupported payload format. Provide a service object or a list under 'services'.")


def _xml_element_to_value(element: ET.Element) -> Any:
    children = list(element)
    if not children:
        text_value = (element.text or "").strip()
        if element.attrib:
            data = {"@attributes": dict(element.attrib)}
            if text_value:
                data["#text"] = text_value
            return data
        return text_value

    data: Dict[str, Any] = {}
    if element.attrib:
        data["@attributes"] = dict(element.attrib)
    for child in children:
        child_value = _xml_element_to_value(child)
        tag = child.tag
        if tag in data:
            if not isinstance(data[tag], list):
                data[tag] = [data[tag]]
            data[tag].append(child_value)
        else:
            data[tag] = child_value

    text_value = (element.text or "").strip()
    if text_value:
        data["#text"] = text_value
    return data


def _parse_xml_payload(text: str) -> Dict[str, Any]:
    try:
        import xmltodict  # type: ignore
    except Exception:
        xmltodict = None

    if xmltodict is not None:
        payload = xmltodict.parse(text)
    else:
        root = ET.fromstring(text)
        payload = {root.tag: _xml_element_to_value(root)}

    if not isinstance(payload, dict) or not payload:
        raise ValueError("XML payload is empty or invalid.")
    if len(payload) == 1:
        sole_key = next(iter(payload))
        sole_value = payload.get(sole_key)
        if isinstance(sole_value, dict):
            payload = sole_value
    return payload


def _ensure_active_project(db: Session, current_user: User) -> Project:
    project_id = current_project_id()
    if project_id is None:
        raise HTTPException(status_code=400, detail="Activate a project before importing API specifications.")
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.organization_id == current_user.organization_id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Active project not found or unavailable for this user.")
    return project


def _operation_to_payload(service: ApiServiceSpec, operation: ApiOperationModel) -> Dict[str, Any]:
    key = f"{_normalize_identifier(service.service)}_{_normalize_identifier(operation.name)}"
    examples = {example.alias: example.payload for example in operation.examples}
    response_contract = operation.response.model_dump()
    raw_definition = {
        "service": service.model_dump(exclude={"operations"}),
        "operation": operation.model_dump(),
    }
    return {
        "service_name": service.service.strip(),
        "operation_name": operation.name.strip(),
        "key": key,
        "base_url": service.base_url.strip(),
        "http_method": operation.method.strip().upper(),
        "path": operation.path.strip(),
        "description": operation.description.strip() if operation.description else None,
        "default_headers": operation.headers or {},
        "default_query": operation.query or {},
        "request_schema": operation.request_body or {},
        "response_schema": response_contract,
        "examples": examples,
        "raw_definition": raw_definition,
    }


def _import_services(
    services: List[ApiServiceSpec],
    project: Project,
    db: Session,
) -> Dict[str, Any]:
    existing_specs = (
        db.query(ApiSpec)
        .filter(ApiSpec.project_id == project.id)
        .all()
    )
    existing_by_key = {spec.key: spec for spec in existing_specs}

    upserted: List[str] = []
    new_keys: Set[str] = set()
    for service in services:
        for operation in service.operations:
            attrs = _operation_to_payload(service, operation)
            existing = (
                db.query(ApiSpec)
                .filter(ApiSpec.project_id == project.id, ApiSpec.key == attrs["key"])
                .first()
            )
            if existing:
                for column, value in attrs.items():
                    setattr(existing, column, value)
            else:
                spec = ApiSpec(project_id=project.id, **attrs)
                db.add(spec)
            upserted.append(attrs["key"])
            new_keys.add(attrs["key"])

    if not upserted:
        return {
            "status": "skipped",
            "project_id": project.id,
            "count": 0,
            "specs": [],
        }

    db.flush()

    for key, spec in existing_by_key.items():
        if key not in new_keys:
            db.delete(spec)

    db.flush()

    specs = (
        db.query(ApiSpec)
        .filter(ApiSpec.project_id == project.id, ApiSpec.key.in_(upserted))
        .all()
    )
    return {
        "status": "imported",
        "project_id": project.id,
        "count": len(upserted),
        "specs": [spec.to_dict() for spec in specs],
    }


def _derive_base_url_from_swagger(doc: Dict[str, Any]) -> str:
    schemes = doc.get("schemes") or ["https"]
    host = doc.get("host") or "localhost"
    base_path = doc.get("basePath") or "/"
    scheme = schemes[0] if isinstance(schemes, list) and schemes else "https"
    return f"{scheme}://{host}{base_path}".rstrip("/") or "http://localhost"


def _derive_base_url_from_openapi(doc: Dict[str, Any]) -> str:
    servers = doc.get("servers") or []
    for server in servers:
        url = server.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip().rstrip("/")
    return "http://localhost"


def _extract_parameters_swagger(params: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    headers: Dict[str, Any] = {}
    query: Dict[str, Any] = {}
    body: Dict[str, Any] = {}
    for param in params or []:
        location = (param.get("in") or "").lower()
        name = param.get("name") or ""
        if not name:
            continue
        if location == "header":
            headers[name] = {
                "required": param.get("required", False),
                "description": param.get("description", ""),
            }
        elif location == "query":
            schema = param.get("schema") or {}
            if not schema and param.get("type"):
                schema = {"type": param.get("type")}
            query[name] = {
                "required": param.get("required", False),
                "description": param.get("description", ""),
                "schema": schema,
            }
        elif location == "body":
            body = param.get("schema") or {}
    return headers, query, body


def _extract_request_body_openapi(operation: Dict[str, Any]) -> Dict[str, Any]:
    request_body = operation.get("requestBody") or {}
    if not request_body:
        return {}
    content = request_body.get("content") or {}
    for media, media_obj in content.items():
        schema = media_obj.get("schema")
        if schema:
            return {"media_type": media, "schema": schema}
    return {}


def _select_expected_status(responses: Dict[str, Any]) -> int:
    if not isinstance(responses, dict):
        return 200
    for status_code in responses.keys():
        try:
            return int(status_code)
        except (ValueError, TypeError):
            continue
    return 200


def _sanitize_security_schemes(definitions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    if not isinstance(definitions, dict):
        return sanitized
    for name, scheme in definitions.items():
        if not isinstance(name, str) or not isinstance(scheme, dict):
            continue
        try:
            sanitized[name] = json.loads(json.dumps(scheme))
        except (TypeError, ValueError):
            sanitized[name] = dict(scheme)
    return sanitized


def _postman_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        text_value = value.get("#text")
        if isinstance(text_value, str):
            return text_value
        raw_value = value.get("value")
        if isinstance(raw_value, str):
            return raw_value
        attrs = value.get("@attributes")
        if isinstance(attrs, dict):
            for key in ("value", "key", "name"):
                candidate = attrs.get(key)
                if isinstance(candidate, str):
                    return candidate
    return None


def _ensure_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _find_postman_root(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if _is_postman_collection(payload):
        return payload
    for candidate in payload.values():
        if isinstance(candidate, dict):
            if _is_postman_collection(candidate):
                return candidate
            nested = _find_postman_root(candidate)
            if nested:
                return nested
        elif isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, dict):
                    if _is_postman_collection(item):
                        return item
                    nested = _find_postman_root(item)
                    if nested:
                        return nested
    return None


def _is_postman_collection(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("info"), dict):
        return False

    # direct 'item' key (JSON format)
    items = payload.get("item")
    if isinstance(items, (list, dict)):
        return True

    # XML-to-dict conversions sometimes produce an 'items' wrapper with 'item' inside
    items_wrapper = payload.get("items")
    if isinstance(items_wrapper, list):
        return True
    if isinstance(items_wrapper, dict) and isinstance(items_wrapper.get("item"), (list, dict)):
        return True

    # Postman export may nest collection under 'collection'
    coll = payload.get("collection")
    if isinstance(coll, dict) and isinstance(coll.get("info"), dict) and isinstance(coll.get("item"), (list, dict)):
        return True

    return False


def _collect_postman_variables(collection: Dict[str, Any]) -> Dict[str, str]:
    variables: Dict[str, str] = {}
    for entry in _ensure_list(collection.get("variable")):
        if not isinstance(entry, dict):
            continue
        key = _postman_text(entry.get("key"))
        if not key or not key.strip():
            continue
        raw_value = entry.get("value")
        value = _postman_text(raw_value)
        if isinstance(value, str):
            variables[key] = value
        elif raw_value is None:
            variables[key] = ""
        else:
            variables[key] = str(raw_value)
    return variables


def _iter_postman_requests(items: List[Any], parent: str = "") -> List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]]:
    requests: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]] = []
    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        name_value = _postman_text(entry.get("name"))
        name = name_value or "Request"
        full_name = f"{parent} / {name}" if parent else name
        children = _ensure_list(entry.get("item"))
        if children:
            requests.extend(_iter_postman_requests(children, full_name))
            continue
        request = entry.get("request")
        if isinstance(request, dict):
            responses = _ensure_list(entry.get("response"))
            requests.append((full_name, request, responses))
    return requests


def _replace_postman_variables(raw: str, variables: Dict[str, str]) -> str:
    resolved = raw
    for key, value in variables.items():
        token = f"{{{{{key}}}}}"
        if token in resolved:
            resolved = resolved.replace(token, value or "")
    return resolved


def _strip_postman_templates(value: str) -> str:
    return re.sub(r"\{\{[^{}]+}}", "", value)


def _resolve_postman_url(request: Dict[str, Any], variables: Dict[str, str]) -> Tuple[str, str]:
    url_obj = request.get("url")
    if isinstance(url_obj, str):
        raw = _replace_postman_variables(url_obj, variables)
    elif isinstance(url_obj, dict):
        raw_value = _postman_text(url_obj.get("raw")) or ""
        raw = _replace_postman_variables(raw_value, variables)
    else:
        raw = ""
    sanitized = _strip_postman_templates(raw)
    parsed = urlparse(sanitized)
    base = ""
    if parsed.scheme and parsed.netloc:
        base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    path = parsed.path or "/"
    if isinstance(url_obj, dict) and not parsed.path:
        segments: List[str] = []
        for part in _ensure_list(url_obj.get("path")):
            if isinstance(part, str) and part:
                segments.append(part.strip("/"))
        if segments:
            path = "/" + "/".join(segments)
    path = path or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    path = re.sub(r"//+", "/", path)
    return base, path


def _convert_postman_headers(headers: List[Any]) -> Dict[str, Any]:
    converted: Dict[str, Any] = {}
    for header in headers or []:
        if not isinstance(header, dict):
            continue
        key = _postman_text(header.get("key"))
        if not key or not key.strip():
            continue
        converted[key] = {
            "required": not header.get("disabled", False),
            "description": header.get("description") or "",
        }
    return converted


def _convert_postman_query(params: List[Any]) -> Dict[str, Any]:
    converted: Dict[str, Any] = {}
    for param in params or []:
        if not isinstance(param, dict):
            continue
        key = _postman_text(param.get("key"))
        if not key or not key.strip():
            continue
        converted[key] = {
            "required": not param.get("disabled", False),
            "description": param.get("description") or "",
            "schema": {"type": "string"},
        }
    return converted


def _convert_postman_body(body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    request_body: Dict[str, Any] = {}
    examples: List[Dict[str, Any]] = []
    if not isinstance(body, dict):
        return request_body, examples
    mode = _postman_text(body.get("mode")) or body.get("mode")
    raw_text = _postman_text(body.get("raw")) if isinstance(body.get("raw"), (str, dict)) else None
    if mode == "raw" and isinstance(raw_text, str):
        try:
            parsed = json.loads(raw_text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            examples.append({"alias": "default", "payload": parsed})
        language = (
            body.get("options", {})
            .get("raw", {})
            .get("language", "")
        )
        if isinstance(language, str) and language.lower() == "json":
            request_body = {"media_type": "application/json", "schema": {}}
    elif mode == "urlencoded":
        payload: Dict[str, Any] = {}
        for item in _ensure_list(body.get("urlencoded")):
            if not isinstance(item, dict):
                continue
            key = _postman_text(item.get("key"))
            if not key or not key.strip():
                continue
            if item.get("disabled"):
                continue
            payload[key] = item.get("value") or ""
        if payload:
            examples.append({"alias": "default", "payload": payload})
            request_body = {"media_type": "application/x-www-form-urlencoded", "schema": {"type": "object"}}
    elif mode == "formdata":
        payload: Dict[str, Any] = {}
        for item in _ensure_list(body.get("formdata")):
            if not isinstance(item, dict):
                continue
            key = _postman_text(item.get("key"))
            if not key or not key.strip():
                continue
            if item.get("disabled") or item.get("type") == "file":
                continue
            payload[key] = item.get("value") or ""
        if payload:
            examples.append({"alias": "default", "payload": payload})
            request_body = {"media_type": "multipart/form-data", "schema": {"type": "object"}}
    return request_body, examples


def _postman_expected_status(responses: List[Dict[str, Any]]) -> int:
    for response in responses or []:
        code = response.get("code")
        if isinstance(code, int):
            return code
        if isinstance(code, str) and code.isdigit():
            return int(code)
    return 200


def _convert_postman_collection(collection: Dict[str, Any]) -> List[Dict[str, Any]]:
    service_name = _postman_text(collection.get("info", {}).get("name"))
    service = service_name.strip() if isinstance(service_name, str) else "Postman Service"
    service = service or "Postman Service"
    variables = _collect_postman_variables(collection)
    # Postman collections may use 'item' directly or be wrapped as 'items'->'item' when converted from XML
    raw_items = collection.get("item")
    if raw_items is None:
        raw_items = collection.get("items")
        if isinstance(raw_items, dict) and raw_items.get("item") is not None:
            raw_items = raw_items.get("item")
    flattened = _iter_postman_requests(_ensure_list(raw_items))
    if not flattened:
        raise HTTPException(status_code=400, detail="Postman collection does not contain any requests to import.")

    operations: List[Dict[str, Any]] = []
    base_candidates: List[str] = []
    name_counts: Dict[str, int] = {}

    for full_name, request, responses in flattened:
        method = (_postman_text(request.get("method")) or "").upper()
        if method not in _VALID_METHODS:
            continue
        base_url, path = _resolve_postman_url(request, variables)
        if base_url:
            base_candidates.append(base_url)

        token = _normalize_identifier(full_name)
        if not token:
            token = f"{method.lower()}_{len(operations) + 1}"
        count = name_counts.get(token, 0)
        name_counts[token] = count + 1
        if count:
            token = f"{token}_{count + 1}"

        headers = _convert_postman_headers(_ensure_list(request.get("header")))
        query = _convert_postman_query(
            _ensure_list((request.get("url") or {}).get("query")) if isinstance(request.get("url"), dict) else []
        )
        request_body, examples = _convert_postman_body(request.get("body") or {})
        expected_status = _postman_expected_status(responses)

        description_value = request.get("description")
        if isinstance(description_value, dict):
            description_value = description_value.get("content")
        description = description_value if isinstance(description_value, str) else None

        operations.append(
            {
                "name": token,
                "method": method,
                "path": path,
                "description": description,
                "headers": headers,
                "query": query,
                "request_body": request_body,
                "response": {
                    "expected_status": expected_status,
                    "body_contains": [],
                },
                "examples": examples,
                "security": [],
            }
        )

    if not operations:
        raise HTTPException(status_code=400, detail="Postman collection does not contain supported HTTP requests.")

    base_url = ""
    for candidate in base_candidates:
        if candidate and "{{" not in candidate:
            base_url = candidate
            break
    if not base_url and base_candidates:
        base_url = base_candidates[0]
    if not base_url:
        fallback = (
            variables.get("baseUrl")
            or variables.get("base_url")
            or variables.get("Base_URL")
            or variables.get("BASE_URL")
            or variables.get("host")
        )
        if isinstance(fallback, str) and fallback.strip():
            base_url = fallback.strip()
    base_url = base_url.strip() if isinstance(base_url, str) else ""
    base_url = _strip_postman_templates(base_url)
    base_url = base_url.rstrip("/") if base_url else "http://localhost"

    return [
        {
            "service": service,
            "base_url": base_url or "http://localhost",
            "auth": {
                "schemes": {},
                "global": [],
            },
            "operations": operations,
        }
    ]


def _convert_openapi_document(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    title = doc.get("info", {}).get("title") or "API Service"
    service_name = title.strip() or "API Service"

    if "swagger" in doc:
        base_url = _derive_base_url_from_swagger(doc)
        security_schemes = _sanitize_security_schemes(doc.get("securityDefinitions"))
        global_security = doc.get("security") or []
        paths = doc.get("paths") or {}
        operations: List[Dict[str, Any]] = []
        for raw_path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.upper() not in _VALID_METHODS:
                    continue
                op_name = operation.get("operationId") or f"{method}_{raw_path}".replace("/", "_").strip("_") or "operation"
                headers, query, body = _extract_parameters_swagger(operation.get("parameters", []))
                responses = operation.get("responses") or {}
                expected_status = _select_expected_status(responses)
                operations.append(
                    {
                        "name": op_name,
                        "method": method.upper(),
                        "path": raw_path,
                        "description": operation.get("summary") or operation.get("description"),
                        "headers": headers,
                        "query": query,
                        "request_body": body,
                        "response": {
                            "expected_status": expected_status,
                            "body_contains": [],
                        },
                        "examples": [],
                        "security": operation.get("security") or [],
                    }
                )
        return [
            {
                "service": service_name,
                "base_url": base_url,
                "auth": {
                    "schemes": security_schemes,
                    "global": global_security,
                },
                "operations": operations,
            }
        ]

    base_url = _derive_base_url_from_openapi(doc)
    security_schemes = _sanitize_security_schemes(doc.get("components", {}).get("securitySchemes"))
    global_security = doc.get("security") or []
    paths = doc.get("paths") or {}
    operations = []
    for raw_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.upper() not in _VALID_METHODS:
                continue
            op_name = operation.get("operationId") or f"{method}_{raw_path}".replace("/", "_").strip("_") or "operation"
            parameters = operation.get("parameters") or []
            headers, query, _ = _extract_parameters_swagger(parameters)
            request_body = _extract_request_body_openapi(operation)
            responses = operation.get("responses") or {}
            expected_status = _select_expected_status(responses)
            operations.append(
                {
                    "name": op_name,
                    "method": method.upper(),
                    "path": raw_path,
                    "description": operation.get("summary") or operation.get("description"),
                    "headers": headers,
                    "query": query,
                    "request_body": request_body,
                    "response": {
                        "expected_status": expected_status,
                        "body_contains": [],
                    },
                    "examples": [],
                    "security": operation.get("security") or [],
                }
            )

    return [
        {
            "service": service_name,
            "base_url": base_url,
            "auth": {
                "schemes": security_schemes,
                "global": global_security,
            },
            "operations": operations,
        }
    ]


@router.post("/api-specs/import")
def import_api_specs(
    payload: Any = Body(..., description="API specification payload."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    services = _coerce_services(payload)
    project = _ensure_active_project(db, current_user)
    return _import_services(services, project, db)


@router.post("/api-specs/import-file")
async def import_api_specs_file(
    file: UploadFile = File(..., description="JSON or XML file containing API specifications."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    filename = (file.filename or "").lower()
    is_json_ext = filename.endswith(".json")
    is_xml_ext = filename.endswith(".xml")
    is_json_type = bool(file.content_type and "json" in file.content_type)
    is_xml_type = bool(file.content_type and "xml" in file.content_type)
    if file.content_type and not (is_json_ext or is_xml_ext or is_json_type or is_xml_type):
        raise HTTPException(status_code=400, detail="Only JSON or XML files are supported.")

    try:
        raw_bytes = await file.read()
        text = raw_bytes.decode("utf-8-sig")
        if is_xml_ext or (is_xml_type and not is_json_ext):
            payload = _parse_xml_payload(text)
        else:
            payload = json.loads(text)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Unable to decode uploaded file using UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not valid JSON.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ET.ParseError as exc:
        raise HTTPException(status_code=400, detail="Uploaded file is not valid XML.") from exc

    from database.migration_runner import run_migrations_if_needed
    run_migrations_if_needed(engine, _alembic_config())
    services = _coerce_services(payload)
    project = _ensure_active_project(db, current_user)
    return _import_services(services, project, db)


@router.get("/api-specs")
def list_api_specs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _ensure_active_project(db, current_user)
    specs = (
        db.query(ApiSpec)
        .filter(ApiSpec.project_id == project.id)
        .order_by(ApiSpec.service_name.asc(), ApiSpec.operation_name.asc())
        .all()
    )
    return {
        "project_id": project.id,
        "specs": [spec.to_dict() for spec in specs],
    }


def _build_snippet_payload(
    target: ApiSpec,
    story: Optional[str],
    requested_alias: Optional[str],
    service_method_names: Dict[str, str],
    test_data_methods: Dict[str, str],
) -> Dict[str, Any]:
    method_name = service_method_names.get(target.key, _safe_token(target.operation_name or target.key))
    test_data_method = test_data_methods.get(target.key)

    raw_examples = target.examples if isinstance(target.examples, dict) else {}
    examples: Dict[str, Any] = {}
    for example_alias, payload in raw_examples.items():
        if not isinstance(example_alias, str):
            continue
        try:
            examples[example_alias] = copy.deepcopy(payload)
        except TypeError:
            examples[example_alias] = payload

    raw_definition = target.raw_definition or {}
    raw_service = raw_definition.get("service") or {}
    raw_operation = raw_definition.get("operation") or {}
    service_auth = raw_service.get("auth") or {}
    auth_schemes = service_auth.get("schemes") or {}
    if not isinstance(auth_schemes, dict):
        auth_schemes = {}
    service_security = service_auth.get("global") or []
    if not isinstance(service_security, list):
        service_security = []
    operation_security = raw_operation.get("security")
    if operation_security is not None and not isinstance(operation_security, list):
        operation_security = []

    alias = requested_alias or ("default" if "default" in examples else None)
    if alias and alias not in examples:
        alias = next(iter(examples), None)

    primary_example = None
    if alias and alias in examples and isinstance(examples[alias], dict):
        primary_example = copy.deepcopy(examples[alias])
    else:
        for payload in examples.values():
            if isinstance(payload, dict):
                primary_example = copy.deepcopy(payload)
                break

    requires_payload = (target.http_method or "").strip().upper() in {"POST", "PUT", "PATCH"} or bool(target.request_schema)
    class_name = _normalize_service_class_name(target.service_name)

    lines: List[str] = []
    if story and story.strip():
        lines.append(f"# Story: {story.strip()}")

    if test_data_method and examples:
        alias_literal = json.dumps(alias) if alias is not None else "None"
        if requires_payload:
            lines.append(f"payload = test_data.{test_data_method}({alias_literal})")
        else:
            lines.append(f"query_params = test_data.{test_data_method}({alias_literal})")
    else:
        if requires_payload:
            lines.append("# TODO: construct request payload")
            lines.append("payload = {}")
        else:
            lines.append("# TODO: construct query parameters")
            lines.append("query_params = {}")

    if requires_payload:
        invocation = f"response = {class_name}.{method_name}(payload)"
    else:
        invocation = f"response = {class_name}.{method_name}(query=query_params)"
    lines.append(invocation)

    response_schema = target.response_schema or {}
    expected_status = int(response_schema.get("expected_status", 200))
    lines.append(f"response.status_should_be({expected_status})")

    for field in response_schema.get("body_contains", []) or []:
        lines.append(f"response.body_should_contain({json.dumps(field)})")

    snippet = "\n".join(lines)

    available_examples = sorted(examples.keys()) if examples else []

    return {
        "key": target.key,
        "spec_id": target.id,
        "service": target.service_name,
        "operation": target.operation_name,
        "description": target.description,
        "class_name": class_name,
        "method_name": method_name,
        "http_method": target.http_method,
        "path": target.path,
        "base_url": target.base_url,
        "requires_payload": requires_payload,
        "test_data_method": test_data_method,
        "example_alias": alias,
        "available_examples": available_examples,
        "expected_status": expected_status,
        "headers": target.default_headers or {},
        "query": target.default_query or {},
        "response_schema": response_schema,
        "examples": examples,
        "primary_example": primary_example,
        "service_auth": service_auth,
        "auth_schemes": auth_schemes,
        "service_security": service_security,
        "operation_security": operation_security,
        "snippet": snippet,
    }


@router.post("/api-specs/generate-snippet")
def generate_api_test_snippet(
    payload: ApiTestSnippetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _ensure_active_project(db, current_user)
    ctx = _load_project_spec_context(db, project.id)
    if not ctx.all_specs:
        raise HTTPException(status_code=404, detail="No API specifications are available for the active project.")

    target: Optional[ApiSpec] = None
    if payload.key:
        normalized = _normalize_identifier(payload.key)
        target = ctx.by_key.get(normalized)
    else:
        service_name = (payload.service or "").strip()
        operation_name = (payload.operation or "").strip()
        if service_name and operation_name:
            for candidate in ctx.by_service.get(service_name, []):
                if candidate.operation_name == operation_name:
                    target = candidate
                    break

    if not target:
        raise HTTPException(status_code=404, detail="Requested API specification was not found for the active project.")

    service_method_names = ctx.service_method_maps.get(target.service_name, {})
    snippet_payload = _build_snippet_payload(
        target,
        payload.story,
        payload.example_alias,
        service_method_names,
        ctx.test_data_methods,
    )
    return snippet_payload


@router.post("/api-tests/generate")
def generate_api_tests(
    payload: Optional[ApiTestsRequest] = Body(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payload = payload or ApiTestsRequest()
    project = _ensure_active_project(db, current_user)
    ctx = _load_project_spec_context(db, project.id)

    if not ctx.all_specs:
        raise HTTPException(status_code=404, detail="No API specifications are available for the active project.")

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
    story_text = (payload.story or "").strip()

    selected_specs: List[ApiSpec] = []
    for spec in ctx.all_specs:
        key_match = not normalized_keys or _normalize_identifier(spec.key) in normalized_keys
        service_match = not service_filter or spec.service_name in service_filter
        if key_match and service_match:
            selected_specs.append(spec)

    if story_text:
        story_blocks = _partition_story_steps(_story_lines(story_text))
        story_case_keys = _match_story_blocks_to_specs(story_blocks, selected_specs)
        if story_case_keys:
            spec_map = {_normalize_identifier(spec.key): spec for spec in selected_specs}
            selected_specs = [spec_map[key] for key in story_case_keys if key in spec_map]

    if not selected_specs:
        raise HTTPException(status_code=404, detail="No API specifications found for the requested filters.")

    example_map = {
        _normalize_identifier(key): value
        for key, value in (payload.examples or {}).items()
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
    }

    generated_items: List[Dict[str, Any]] = []
    for spec in selected_specs:
        service_method_names = ctx.service_method_maps.get(spec.service_name, {})
        requested_alias = example_map.get(_normalize_identifier(spec.key))
        snippet_info = _build_snippet_payload(
            spec,
            payload.story,
            requested_alias,
            service_method_names,
            ctx.test_data_methods,
        )
        generated_items.append(snippet_info)

    grouped_by_service: Dict[str, Dict[str, Any]] = {}
    for item in generated_items:
        service_name = item["service"]
        entry = grouped_by_service.setdefault(
            service_name,
            {
                "service": service_name,
                "class_name": item["class_name"],
                "base_url": item["base_url"],
                "operations": [],
            },
        )
        entry["operations"].append(item)

    services_payload: List[Dict[str, Any]] = []
    for service_name in sorted(grouped_by_service):
        entry = grouped_by_service[service_name]
        entry["operations"] = sorted(
            entry["operations"],
            key=lambda op: (op.get("operation") or "", op["method_name"]),
        )
        services_payload.append(entry)

    return {
        "project_id": project.id,
        "count": len(generated_items),
        "filters": {
            "keys": sorted(normalized_keys) if normalized_keys else None,
            "services": sorted(service_filter) if service_filter else None,
        },
        "services": services_payload,
    }


@router.delete("/api-specs/{key}")
def delete_api_spec(
    key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    project = _ensure_active_project(db, current_user)
    normalized = _normalize_identifier(key)
    target = (
        db.query(ApiSpec)
        .filter(ApiSpec.project_id == project.id, ApiSpec.key == normalized)
        .first()
    )
    if not target:
        raise HTTPException(status_code=404, detail=f"API spec '{key}' not found for the active project.")
    db.delete(target)
    db.flush()
    return {"status": "deleted", "key": normalized, "project_id": project.id}
