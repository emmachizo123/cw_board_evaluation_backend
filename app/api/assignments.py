"""
app.api.assignments
-------------------
Assignment endpoints for the Board Evaluation platform.

This module introduces granular assessment tasks ("assignments") inside an evaluation.
Each assignment has its own access token and status.

Key endpoints:
- POST /api/v1/evaluations/{evaluation_id}/assignments/bulk
- GET  /api/v1/evaluations/{evaluation_id}/assignments
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import AssessmentAssignment, Evaluation, Participant

router = APIRouter()


# -------------------------
# Helpers
# -------------------------

def _utcnow() -> datetime:
    """Return timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def _ensure_evaluation_exists(db: Session, evaluation_id: str) -> Evaluation:
    """Fetch evaluation or raise 404."""
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(status_code=404, detail=f"Evaluation not found: {evaluation_id}")
    return ev


def _ensure_participant_exists(db: Session, participant_id: UUID) -> Participant:
    """Fetch participant by UUID or raise 404."""
    p = db.get(Participant, participant_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"Participant not found: {participant_id}")
    return p


def _generate_unique_assignment_token(db: Session, max_tries: int = 10) -> str:
    """
    Generate a URL-safe access token and verify uniqueness against AssessmentAssignment.access_token.
    """
    for _ in range(max_tries):
        token = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
        exists = (
            db.execute(select(AssessmentAssignment).where(AssessmentAssignment.access_token == token))
            .scalars()
            .first()
        )
        if not exists:
            return token
    raise HTTPException(status_code=500, detail="Failed to generate unique assignment token.")


def _serialize_assignment(a: AssessmentAssignment) -> Dict[str, Any]:
    """Serialize an AssessmentAssignment for API responses."""
    return {
        "assignment_id": str(a.id),
        "evaluation_id": a.evaluation_id,
        "respondent_participant_id": str(a.respondent_participant_id),
        "subject_participant_id": str(a.subject_participant_id) if a.subject_participant_id else None,
        "assignment_type": a.assignment_type,
        "committee_name": a.committee_name,
        "instrument_template_code": a.instrument_template_code,
        "instrument_version": int(a.instrument_version),
        "access_token": a.access_token,
        "status": a.status,
        "invited_at": a.invited_at.isoformat() if a.invited_at else None,
        "responded_at": a.responded_at.isoformat() if a.responded_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# -------------------------
# Schemas
# -------------------------

class BulkAssignmentItem(BaseModel):
    """
    One assignment to create.

    You can provide respondent_participant_id (preferred).
    respondent_email is optional (kept for backward compatibility with earlier drafts).
    """
    respondent_participant_id: UUID
    respondent_email: Optional[str] = None

    assignment_type: str = Field(default="BOARD_AS_WHOLE", min_length=1)

    subject_participant_id: Optional[UUID] = None
    committee_name: Optional[str] = None

    instrument_template_code: Optional[str] = None
    instrument_version: Optional[int] = None


class BulkCreateAssignmentsRequest(BaseModel):
    """
    Bulk create request.

    Supports BOTH keys:
    - items (older drafts)
    - assignments (newer drafts)

    Use `assignments` going forward.
    """
    assignments: List[BulkAssignmentItem] = Field(default_factory=list)
    items: List[BulkAssignmentItem] = Field(default_factory=list)


class BulkCreateAssignmentsResponse(BaseModel):
    evaluation_id: str
    created: int
    existing: int
    invalid: int
    items: List[Dict[str, Any]]


# -------------------------
# Routes
# -------------------------

@router.post(
    "/evaluations/{evaluation_id}/assignments/bulk",
    status_code=status.HTTP_201_CREATED,
    response_model=BulkCreateAssignmentsResponse,
)
def bulk_create_assignments(
    evaluation_id: str,
    payload: BulkCreateAssignmentsRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Bulk-create assignment rows for an evaluation.

    Idempotency:
    - If an assignment already exists for the same:
        evaluation_id + respondent + assignment_type + subject + committee_name
      then the DB unique constraint (uq_assignment_dedup) prevents duplicates.
    - We catch IntegrityError and return the existing assignment row.

    Safety:
    - Each row is processed in a SAVEPOINT (begin_nested).
      If one item fails, earlier successful inserts are NOT rolled back.
    """
    ev = _ensure_evaluation_exists(db, evaluation_id)

    # Accept both request keys; prefer assignments
    rows = payload.assignments or payload.items
    if not rows:
        raise HTTPException(status_code=400, detail="assignments list cannot be empty")

    created = 0
    existing = 0
    invalid = 0
    out_items: List[Dict[str, Any]] = []

    for it in rows:
        # SAVEPOINT per row
        with db.begin_nested():
            try:
                respondent = _ensure_participant_exists(db, it.respondent_participant_id)

                # respondent must belong to evaluation
                if str(respondent.evaluation_id) != str(evaluation_id):
                    raise HTTPException(
                        status_code=400,
                        detail=f"respondent_participant_id {respondent.id} is not in evaluation {evaluation_id}",
                    )

                subject_id: Optional[UUID] = None
                if it.subject_participant_id:
                    subject = _ensure_participant_exists(db, it.subject_participant_id)
                    if str(subject.evaluation_id) != str(evaluation_id):
                        raise HTTPException(
                            status_code=400,
                            detail=f"subject_participant_id {subject.id} is not in evaluation {evaluation_id}",
                        )
                    subject_id = subject.id

                template_code = (it.instrument_template_code or "").strip() or (ev.instrument_template_code or "DEFAULT").strip()
                version = int(it.instrument_version or ev.instrument_version or 1)

                a = AssessmentAssignment(
                    evaluation_id=evaluation_id,
                    respondent_participant_id=respondent.id,
                    subject_participant_id=subject_id,
                    assignment_type=(it.assignment_type or "BOARD_AS_WHOLE").strip(),
                    committee_name=(it.committee_name or None),
                    instrument_template_code=template_code,
                    instrument_version=version,
                    access_token=_generate_unique_assignment_token(db),
                    token_created_at=_utcnow(),
                    status="invited",
                    invited_at=_utcnow(),
                )

                db.add(a)
                db.flush()
                db.refresh(a)

                created += 1
                out_items.append(_serialize_assignment(a))

            except IntegrityError:
                # duplicate assignment or token conflict
                # nested transaction will rollback automatically; now locate existing
                q = (
                    select(AssessmentAssignment)
                    .where(AssessmentAssignment.evaluation_id == evaluation_id)
                    .where(AssessmentAssignment.respondent_participant_id == it.respondent_participant_id)
                    .where(AssessmentAssignment.assignment_type == (it.assignment_type or "BOARD_AS_WHOLE").strip())
                )

                if it.subject_participant_id:
                    q = q.where(AssessmentAssignment.subject_participant_id == it.subject_participant_id)
                else:
                    q = q.where(AssessmentAssignment.subject_participant_id.is_(None))

                if it.committee_name:
                    q = q.where(AssessmentAssignment.committee_name == it.committee_name)
                else:
                    q = q.where(AssessmentAssignment.committee_name.is_(None))

                found = db.execute(q).scalars().first()
                if found:
                    existing += 1
                    out_items.append(_serialize_assignment(found))
                else:
                    invalid += 1
                    out_items.append(
                        {
                            "error": "IntegrityError occurred, and existing assignment could not be located.",
                            "respondent_participant_id": str(it.respondent_participant_id),
                            "subject_participant_id": str(it.subject_participant_id) if it.subject_participant_id else None,
                            "assignment_type": it.assignment_type,
                            "committee_name": it.committee_name,
                        }
                    )

            except HTTPException as e:
                invalid += 1
                out_items.append({"error": e.detail, "respondent_participant_id": str(it.respondent_participant_id)})

            except Exception as e:
                invalid += 1
                out_items.append({"error": str(e), "respondent_participant_id": str(it.respondent_participant_id)})

    db.commit()

    return {
        "evaluation_id": evaluation_id,
        "created": created,
        "existing": existing,
        "invalid": invalid,
        "items": out_items,
    }
