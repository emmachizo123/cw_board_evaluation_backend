"""
app.api.portal
--------------
Assignment-based portal endpoints (token-based access).

Routes:
- GET  /api/v1/portal/{token}
- GET  /api/v1/portal/{token}/responses
- POST /api/v1/portal/{token}/responses

Purpose:
- Allow invited members to open a portal link and submit answers securely via
  AssessmentAssignment.access_token (NOT participant token).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AssessmentAssignment, Evaluation, Participant, Question, Response
from app.db.session import get_db

router = APIRouter()


# -------------------------
# Helpers
# -------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_assignment_by_token(
    db: Session,
    token: str,
    *,
    allow_completed: bool = False,
) -> AssessmentAssignment:
    """
    Resolve an assignment from its access token.

    Option 1A support:
    - allow_completed=False (default): block responded assignments (used by question pages if you want)
    - allow_completed=True: allow responded assignments (used by GET /responses and POST finalize=false)
    """
    t = (token or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail="Token is required.")

    a = (
        db.execute(
            select(AssessmentAssignment).where(AssessmentAssignment.access_token == t)
        )
        .scalars()
        .first()
    )
    if not a:
        raise HTTPException(status_code=404, detail="Invalid or expired assignment link.")

    # ✅ Only block completed when caller does NOT allow it
    if not allow_completed and str(a.status or "").lower() == "responded":
        raise HTTPException(status_code=409, detail="This assignment has already been completed.")

    return a


def _ensure_evaluation(db: Session, evaluation_id: str) -> Evaluation:
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(status_code=404, detail=f"Evaluation not found: {evaluation_id}")
    return ev


def _ensure_participant(db: Session, participant_id) -> Participant:
    p = db.get(Participant, participant_id)
    if not p:
        raise HTTPException(status_code=404, detail="Participant not found.")
    return p


def _get_active_questions(db: Session, template_code: str, version: int) -> List[Question]:
    return (
        db.execute(
            select(Question)
            .where(Question.template_code == template_code)
            .where(Question.version == int(version))
            .where(Question.active == 1)
            .order_by(Question.dimension.asc(), Question.created_at.asc())
        )
        .scalars()
        .all()
    )


def _validate_answer(q: Question, score: Optional[int], comment: Optional[str]) -> None:
    at = (q.answer_type or "").lower()

    if at == "rating":
        if score is None:
            raise HTTPException(status_code=400, detail=f"Missing score for rating question: {q.id}")
        if not (1 <= int(score) <= 5):
            raise HTTPException(status_code=400, detail=f"Score must be 1..5 for rating question: {q.id}")
        return

    if at == "yesno":
        if score is None:
            raise HTTPException(status_code=400, detail=f"Missing score for yes/no question: {q.id}")
        if int(score) not in (0, 1):
            raise HTTPException(status_code=400, detail=f"Score must be 0/1 for yes/no question: {q.id}")
        return

    if at == "comment":
        txt = (comment or "").strip()
        if not txt:
            raise HTTPException(status_code=400, detail=f"Missing comment for comment question: {q.id}")
        return

    raise HTTPException(status_code=400, detail=f"Unsupported answer_type '{q.answer_type}' for question: {q.id}")


def _instrument_from_assignment_or_eval(a: AssessmentAssignment, ev: Evaluation) -> Dict[str, Any]:
    """
    Prefer assignment's instrument fields if present, else fall back to evaluation.
    This supports future per-assignment instruments without breaking the portal.
    """
    a_tpl = (getattr(a, "instrument_template_code", None) or "").strip()
    a_ver = getattr(a, "instrument_version", None)

    template_code = a_tpl or (ev.instrument_template_code or "DEFAULT").strip()
    version = int(a_ver or ev.instrument_version or 1)

    return {"template_code": template_code, "version": version}


# -------------------------
# Schemas
# -------------------------

class PortalQuestionOut(BaseModel):
    question_id: str
    dimension: str
    text: str
    answer_type: str
    weight: int
    active: int


class PortalParticipantOut(BaseModel):
    participant_id: str
    email: str
    full_name: Optional[str] = None
    role: Optional[str] = None
    status: str
    invited_at: Optional[str] = None
    responded_at: Optional[str] = None


class PortalEvaluationOut(BaseModel):
    evaluation_id: str
    tenant_name: str
    sector: str
    year: int
    regulators: List[str] = Field(default_factory=list)
    instrument: Dict[str, Any]


class PortalAssignmentOut(BaseModel):
    assignment_id: str
    assignment_type: str
    committee_name: Optional[str] = None
    status: str
    invited_at: Optional[str] = None
    responded_at: Optional[str] = None
    instrument: Dict[str, Any]


class PortalLoadOut(BaseModel):
    assignment: PortalAssignmentOut
    respondent: PortalParticipantOut
    subject: Optional[PortalParticipantOut] = None
    evaluation: PortalEvaluationOut
    questions: List[PortalQuestionOut]


class PortalAnswerIn(BaseModel):
    question_id: str
    score: Optional[int] = None
    comment: Optional[str] = None


class PortalSubmitIn(BaseModel):
    answers: List[PortalAnswerIn] = Field(default_factory=list)


# -------------------------
# Routes
# -------------------------


@router.get("/portal/{token}", status_code=status.HTTP_200_OK, response_model=PortalLoadOut)
def portal_load(token: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    # ✅ allow completed assignments to load (so invite/evaluation/thank-you can work for responded tokens)
    a = _ensure_assignment_by_token(db, token, allow_completed=True)

    ev = _ensure_evaluation(db, a.evaluation_id)

    instrument = _instrument_from_assignment_or_eval(a, ev)
    template_code = instrument["template_code"]
    version = int(instrument["version"])

    questions = _get_active_questions(db, template_code=template_code, version=version)
    if not questions:
        raise HTTPException(status_code=400, detail="No active questions found for this assignment's questionnaire.")

    respondent = _ensure_participant(db, a.respondent_participant_id)
    subject = db.get(Participant, a.subject_participant_id) if a.subject_participant_id else None

    return {
        "assignment": {
            "assignment_id": str(a.id),
            "assignment_type": a.assignment_type,
            "committee_name": a.committee_name,
            "status": a.status,
            "invited_at": a.invited_at.isoformat() if a.invited_at else None,
            "responded_at": a.responded_at.isoformat() if a.responded_at else None,
            "instrument": instrument,
        },
        "respondent": {
            "participant_id": str(respondent.id),
            "email": respondent.email,
            "full_name": respondent.full_name,
            "role": respondent.role,
            "status": respondent.status,
            "invited_at": respondent.invited_at.isoformat() if respondent.invited_at else None,
            "responded_at": respondent.responded_at.isoformat() if respondent.responded_at else None,
        },
        "subject": (
            {
                "participant_id": str(subject.id),
                "email": subject.email,
                "full_name": subject.full_name,
                "role": subject.role,
                "status": subject.status,
                "invited_at": subject.invited_at.isoformat() if subject.invited_at else None,
                "responded_at": subject.responded_at.isoformat() if subject.responded_at else None,
            }
            if subject
            else None
        ),
        "evaluation": {
            "evaluation_id": ev.id,
            "tenant_name": ev.tenant_name,
            "sector": ev.sector,
            "year": ev.year,
            "regulators": (ev.regulators or {}).get("items", []) if isinstance(ev.regulators, dict) else [],
            "instrument": instrument,
        },
        "questions": [
            {
                "question_id": str(q.id),
                "dimension": q.dimension,
                "text": q.text,
                "answer_type": q.answer_type,
                "weight": int(q.weight or 1),
                "active": int(q.active or 1),
            }
            for q in questions
        ],
    }



@router.get("/portal/{token}/responses", status_code=status.HTTP_200_OK)
def portal_get_responses(
    token: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    # ✅ allow completed (this is a read endpoint)
    a = _ensure_assignment_by_token(db, token, allow_completed=True)
    ev = _ensure_evaluation(db, a.evaluation_id)

    instrument = _instrument_from_assignment_or_eval(a, ev)
    template_code = instrument["template_code"]
    version = int(instrument["version"])

    questions = _get_active_questions(db, template_code=template_code, version=version)
    qids = {q.id for q in questions}

    rows = (
        db.execute(
            select(Response)
            .where(Response.assignment_id == a.id)
            .where(Response.question_id.in_(list(qids)) if qids else True)
            .order_by(Response.created_at.asc())
        )
        .scalars()
        .all()
    )

    items = [
        {
            "question_id": str(r.question_id),
            "score": r.score,
            "comment": r.comment,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]

    return {
        "evaluation_id": ev.id,
        "assignment_id": str(a.id),
        "respondent_participant_id": str(a.respondent_participant_id),
        "count": len(items),
        "items": items,
    }


@router.post("/portal/{token}/responses", status_code=status.HTTP_200_OK)
def portal_submit_responses(
    token: str,
    payload: PortalSubmitIn,
    finalize: bool = Query(default=False, description="If true, mark assignment as responded."),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    # ✅ Allow loading even if completed (so drafts can be saved)
    a = _ensure_assignment_by_token(db, token, allow_completed=True)

    # ✅ Option 1A: allow saving drafts after responded, but block re-finalize
    if str(a.status or "").lower() == "responded" and finalize:
        raise HTTPException(status_code=409, detail="This assignment has already been completed.")

    ev = _ensure_evaluation(db, a.evaluation_id)

    instrument = _instrument_from_assignment_or_eval(a, ev)
    template_code = instrument["template_code"]
    version = int(instrument["version"])

    questions = _get_active_questions(db, template_code=template_code, version=version)
    q_by_id = {str(q.id): q for q in questions}
    if not q_by_id:
        raise HTTPException(status_code=400, detail="No active questions found for this assignment's questionnaire.")

    if not payload.answers:
        raise HTTPException(status_code=400, detail="answers list cannot be empty")

    created = 0
    updated = 0
    skipped = 0

    for ans in payload.answers:
        qid = (str(ans.question_id) if ans.question_id is not None else "").strip()
        q = q_by_id.get(qid)

        if not q:
            skipped += 1
            continue

        _validate_answer(q, score=ans.score, comment=ans.comment)

        existing = (
            db.execute(
                select(Response)
                .where(Response.assignment_id == a.id)
                .where(Response.question_id == q.id)
            )
            .scalars()
            .first()
        )

        if existing:
            existing.score = ans.score
            existing.comment = ans.comment
            db.add(existing)
            updated += 1
        else:
            db.add(
                Response(
                    evaluation_id=ev.id,
                    participant_id=a.respondent_participant_id,
                    assignment_id=a.id,
                    question_id=q.id,
                    score=ans.score,
                    comment=ans.comment,
                )
            )
            created += 1

    if finalize:
        if a.status != "responded":
            a.status = "responded"
        a.responded_at = _utcnow()
        db.add(a)

    db.commit()

    return {
        "evaluation_id": ev.id,
        "assignment_id": str(a.id),
        "respondent_participant_id": str(a.respondent_participant_id),
        "created": created,
        "updated": updated,
        "skipped_unknown_questions": skipped,
        "finalized": bool(finalize),
        "assignment_status": a.status,
        "assignment_responded_at": a.responded_at.isoformat() if a.responded_at else None,
    }
