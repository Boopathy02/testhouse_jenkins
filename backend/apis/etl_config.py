import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

API_TOKEN = os.getenv("ETL_API_TOKEN", "")
BEARER_PREFIX = "Bearer "

PROMPT_PATH = Path(os.getenv("ETL_PROMPT_PATH", str(BASE_DIR / "prompts" / "etl_prompt.txt")))
GENERATED_DIR = Path(os.getenv("ETL_GENERATED_DIR", str(BASE_DIR / "generated" / "etl")))
TOOLS_DIR = Path(os.getenv("ETL_TOOLS_DIR", str(BASE_DIR / "tools")))

DEFAULT_OUTPUT_FILENAME = "etl_testcases"
EXECUTION_TIME_DEFAULT = "0.00s"

PASS_STATUS = "PASS"
FAIL_STATUS = "FAIL"

TEST_PREFIX_AUTOMATION = "AUTO"
TEST_PREFIX_MANUAL = "MANUAL"

DATE_MIN_YEAR = 1900
DATE_COLUMN_KEYWORDS = ("date", "dt", "timestamp")
NUMERIC_COLUMN_KEYWORDS = ("salary", "amount", "price", "cost", "total")

MAX_PROMPT_TABLE_ROWS = 200
SQLITE_TABLE_LIMIT = 200

ALLOWED_STATUS = {"active", "inactive", "graduated", "suspended"}
ALLOWED_GRADES = {"A", "B", "C", "D", "E", "F"}

REQUIRED_TABLES = {
    "department": {"department_id", "dept_name", "batch_id", "load_date"},
    "student": {
        "student_id",
        "first_name",
        "last_name",
        "email",
        "department_id",
        "enrollment_year",
        "status",
        "batch_id",
        "load_date",
    },
    "course": {
        "course_id",
        "course_code",
        "course_name",
        "credits",
        "department_id",
        "batch_id",
        "load_date",
    },
    "enrollment": {
        "enrollment_id",
        "student_id",
        "course_id",
        "semester",
        "academic_year",
        "grade",
        "batch_id",
        "load_date",
    },
}

SAMPLE_DATA = {
    "batch_id": "batch_{seed}",
    "load_date": "2024-01-01",
    "department": {"dept_name": "Engineering"},
    "course": {"course_code_prefix": "ENG", "course_code_start": 100, "course_name": "Foundations", "credits": 4},
    "student": {
        "first_name": "Student{seed}",
        "last_name": "Test",
        "email_template": "student{seed}@example.com",
        "enrollment_year": 2024,
        "status": "active",
    },
    "enrollment": {"semester": "Fall", "academic_year": "2024", "grade": "A"},
}

VALIDATION_TYPES = {
    "schema": "Schema Validation",
    "reconciliation": "Row Count Reconciliation",
    "domain": "Domain Validation",
    "idempotency": "Idempotency Checks",
    "referential": "Referential Integrity",
    "duplicate": "Duplicate Checks",
    "null": "Null Value Checks",
}

TEST_CASE_TEMPLATES = [
    {"constraint": "schemaValidation", "validation": "schema", "name": "Schema matches target", "tables": []},
    {"constraint": "reconciliation", "validation": "reconciliation", "name": "Row counts match", "tables": []},
    {"constraint": "domainChecks", "validation": "domain", "name": "Domain constraints satisfied", "tables": []},
    {"constraint": "idempotencyChecks", "validation": "idempotency", "name": "Idempotency check passes", "tables": []},
    {"constraint": "referentialIntegrity", "validation": "referential", "name": "Referential integrity holds", "tables": []},
    {"constraint": "duplicateChecks", "validation": "duplicate", "name": "No duplicate keys", "tables": []},
    {"constraint": "nullChecks", "validation": "null", "name": "No nulls in required fields", "tables": []},
]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_ENDPOINT = os.getenv("OPENAI_ENDPOINT", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_TIMEOUT_SECONDS = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
