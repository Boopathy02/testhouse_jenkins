#markers
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
from database.models import Project, TestCaseMetadata, User
from database.session import get_db

router = APIRouter(tags=["markers"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


class TokenPayload(BaseModel):
    sub: EmailStr
    uid: int
    org: str
    org_id: Optional[int] = None
    exp: Optional[int] = None


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        token_data = TokenPayload(**payload)
    except (JWTError, ValidationError) as exc:
        raise credentials_exception from exc

    user = (
        db.query(User)
        .filter(User.id == token_data.uid, User.email == str(token_data.sub).lower())
        .first()
    )
    if not user:
        raise credentials_exception

    if (user.organization or "").strip().lower() != (token_data.org or "").strip().lower():
        raise credentials_exception
    if token_data.org_id is not None and user.organization_id != token_data.org_id:
        raise credentials_exception

    return user


def _get_project_for_user(project_id: int, db: Session, org_id: int) -> Project:
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.organization_id == org_id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail=f"Project with id '{project_id}' not found")
    return project


class MarkersPayload(BaseModel):
    markers: List[str]

    @field_validator("markers")
    @classmethod
    def _validate_markers(cls, value: List[str]) -> List[str]:
        if value is None:
            raise ValueError("markers is required")
        if not isinstance(value, list):
            raise ValueError("markers must be a list of strings")
        cleaned: List[str] = []
        for marker in value:
            marker_str = str(marker).strip()
            if not marker_str:
                continue
            cleaned.append(marker_str)
        # Deduplicate while preserving order.
        deduped = list(dict.fromkeys(cleaned))
        return deduped


def _get_test_case_metadata(
    db: Session, project_id: int, test_name: str
) -> Optional[TestCaseMetadata]:
    return (
        db.query(TestCaseMetadata)
        .filter(TestCaseMetadata.project_id == project_id, TestCaseMetadata.test_name == test_name)
        .first()
    )


@router.post(
    "/projects/{project_id}/testcases/{test_name:path}/markers",
    status_code=status.HTTP_201_CREATED,
)
def create_test_case_markers(
    project_id: int,
    test_name: str,
    payload: MarkersPayload,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a markers record for a given test case within a project.

    Returns 409 if the record already exists.
    """
    _get_project_for_user(project_id, db, current_user.organization_id)

    test_name_clean = (test_name or "").strip()
    if not test_name_clean:
        raise HTTPException(status_code=400, detail="test_name is required")

    existing = _get_test_case_metadata(db, project_id, test_name_clean)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Markers already exist for this test case",
        )

    record = TestCaseMetadata(
        project_id=project_id,
        test_name=test_name_clean,
        markers=payload.markers,
    )
    try:
        db.add(record)
        db.commit()
        db.refresh(record)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Markers already exist for this test case",
        )

    return {"status": "created", "test_case_metadata": record.to_dict()}


@router.get("/projects/{project_id}/testcases/{test_name:path}/markers")
def get_test_case_markers(
    project_id: int,
    test_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve markers for a given test case within a project."""
    _get_project_for_user(project_id, db, current_user.organization_id)

    test_name_clean = (test_name or "").strip()
    if not test_name_clean:
        raise HTTPException(status_code=400, detail="test_name is required")

    record = _get_test_case_metadata(db, project_id, test_name_clean)
    if not record:
        raise HTTPException(status_code=404, detail="Markers not found for this test case")

    return {"test_case_metadata": record.to_dict()}


@router.put("/projects/{project_id}/testcases/{test_name:path}/markers")
def upsert_test_case_markers(
    project_id: int,
    test_name: str,
    payload: MarkersPayload,
    upsert: bool = Query(
        True,
        description="When true, create the record if it does not exist.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update (replace) markers for a test case.

    By default, this endpoint is an upsert: it will create the record if missing.
    """
    _get_project_for_user(project_id, db, current_user.organization_id)

    test_name_clean = (test_name or "").strip()
    if not test_name_clean:
        raise HTTPException(status_code=400, detail="test_name is required")

    record = _get_test_case_metadata(db, project_id, test_name_clean)
    if not record and not upsert:
        raise HTTPException(status_code=404, detail="Markers not found for this test case")

    try:
        if record:
            record.markers = payload.markers
            db.add(record)
            db.commit()
            db.refresh(record)
            return {"status": "updated", "test_case_metadata": record.to_dict()}

        record = TestCaseMetadata(
            project_id=project_id,
            test_name=test_name_clean,
            markers=payload.markers,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return {"status": "created", "test_case_metadata": record.to_dict()}
    except IntegrityError:
        db.rollback()
        # Another request may have created it concurrently; retry as update.
        existing = _get_test_case_metadata(db, project_id, test_name_clean)
        if not existing:
            raise HTTPException(status_code=500, detail="Failed to save markers")
        existing.markers = payload.markers
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return {"status": "updated", "test_case_metadata": existing.to_dict()}
