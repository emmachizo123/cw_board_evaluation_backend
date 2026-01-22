"""
app.api.evaluations
-------------------
Endpoints for managing evaluations and participants, plus seeding demo responses,
and (NEW) granular assessment assignments.

Separation of concerns:
- This module owns Evaluation + Participant + Response lifecycle flows.
- Question CRUD / instrument library operations live in app.api.questions.
- Portal token resolution and answer submission live in app.api.portal.

Endpoints:
- POST /api/v1/evaluations
- GET  /api/v1/evaluations
- GET  /api/v1/evaluations/{evaluation_id}

- POST /api/v1/evaluations/{evaluation_id}/participants/invite
- GET  /api/v1/evaluations/{evaluation_id}/participants

- PATCH /api/v1/evaluations/{evaluation_id}/instrument

- POST /api/v1/evaluations/{evaluation_id}/seed/demo-responses

NEW (Assignments):
- POST /api/v1/evaluations/{evaluation_id}/assignments/bulk
- GET  /api/v1/evaluations/{evaluation_id}/assignments
"""

from __future__ import annotations

import os
import random
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Evaluation, Participant, Question, Response, AssessmentAssignment
from app.services.email_service import send_invite_email

router = APIRouter()


# -------------------------
# Helpers
# -------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_evaluation_exists(db: Session, evaluation_id: str) -> Evaluation:
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(status_code=404, detail=f"Evaluation not found: {evaluation_id}")
    return ev


def _get_questions_for_instrument(db: Session, template_code: str, version: int) -> List[Question]:
    return (
        db.execute(
            select(Question)
            .where(Question.template_code == template_code)
            .where(Question.version == version)
            .where(Question.active == 1)
        )
        .scalars()
        .all()
    )


def _portal_base_url() -> str:
    """
    Return the base URL used to generate portal links.

    Configure per environment:
      - PARTICIPANT_PORTAL_BASE_URL=http://localhost:5173     (local dev)
      - PARTICIPANT_PORTAL_BASE_URL=https://cw.example.com    (production)

    The final portal link (matches your React routes) is:
      <base>/member/<token>/questions
    """
    base = (os.getenv("PARTICIPANT_PORTAL_BASE_URL") or "http://localhost:5173").strip()
    return base.rstrip("/")


def _make_portal_url(token: str) -> str:
    """
    Build the full Board Member portal URL for an *assignment* token
    (and still works for participant tokens in legacy flows).

    React Router:
      /member/:token/questions
    """
    t = (token or "").strip()
    if not t:
        return ""
    return f"{_portal_base_url()}/member/{t}/questions"


def _generate_unique_token_for_model(db: Session, model_cls, token_attr: str, max_tries: int = 8) -> str:
    """
    Generate a token that is *very likely* unique, and verify against DB.

    model_cls: Participant or AssessmentAssignment
    token_attr: "access_token"
    """
    for _ in range(max_tries):
        token = secrets.token_urlsafe(24)
        token = token.replace("-", "").replace("_", "")[:32]

        col = getattr(model_cls, token_attr)
        exists = db.execute(select(model_cls).where(col == token)).scalars().first()
        if not exists:
            return token

    raise HTTPException(status_code=500, detail="Failed to generate unique access token.")


def _generate_unique_participant_access_token(db: Session, max_tries: int = 8) -> str:
    return _generate_unique_token_for_model(db, Participant, "access_token", max_tries=max_tries)


def _generate_unique_assignment_access_token(db: Session, max_tries: int = 8) -> str:
    return _generate_unique_token_for_model(db, AssessmentAssignment, "access_token", max_tries=max_tries)


def _find_participant_by_email(db: Session, evaluation_id: str, email: str) -> Optional[Participant]:
    e = (email or "").strip().lower()
    if not e:
        return None
    return (
        db.execute(
            select(Participant)
            .where(Participant.evaluation_id == evaluation_id)
            .where(Participant.email == e)
        )
        .scalars()
        .first()
    )


# -------------------------
# Schemas
# -------------------------

class CreateEvaluationRequest(BaseModel):
    evaluation_id: Optional[str] = None
    tenant_name: str
    sector: str
    year: int
    regulators: List[str] = Field(default_factory=list)


class InviteParticipantItem(BaseModel):
    email: str
    full_name: Optional[str] = None
    role: Optional[str] = None


class InviteParticipantsRequest(BaseModel):
    participants: List[InviteParticipantItem]


class SetInstrumentRequest(BaseModel):
    template_code: str = Field(default="DEFAULT", min_length=1)
    version: int = Field(default=1, ge=1, le=10_000)


# --- NEW: assignments ---

class BulkAssignmentItem(BaseModel):
    respondent_email: str = Field(..., description="Who fills the form (must be a participant email).")
    assignment_type: str = Field(default="BOARD_AS_WHOLE")
    subject_email: Optional[str] = Field(default=None, description="Who is being evaluated (optional).")
    committee_name: Optional[str] = None
    instrument_template_code: Optional[str] = None
    instrument_version: Optional[int] = None


class BulkCreateAssignmentsRequest(BaseModel):
    assignments: List[BulkAssignmentItem] = Field(default_factory=list)


# -------------------------
# Evaluations
# -------------------------

@router.post("/evaluations", status_code=status.HTTP_201_CREATED)
def create_evaluation(payload: CreateEvaluationRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    evaluation_id = (payload.evaluation_id or "").strip()
    if not evaluation_id:
        evaluation_id = f"eval-{str(uuid4())[:8]}"

    if db.get(Evaluation, evaluation_id):
        raise HTTPException(status_code=409, detail=f"Evaluation already exists: {evaluation_id}")

    ev = Evaluation(
        id=evaluation_id,
        tenant_name=payload.tenant_name,
        sector=payload.sector,
        year=int(payload.year),
        regulators={"items": list(payload.regulators or [])},
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)

    return {
        "evaluation_id": ev.id,
        "tenant_name": ev.tenant_name,
        "sector": ev.sector,
        "year": ev.year,
        "regulators": (ev.regulators or {}).get("items", []) if isinstance(ev.regulators, dict) else [],
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


@router.get("/evaluations", status_code=status.HTTP_200_OK)
def list_evaluations(db: Session = Depends(get_db)) -> Dict[str, Any]:
    rows = db.execute(select(Evaluation).order_by(Evaluation.created_at.desc())).scalars().all()
    items = []
    for ev in rows:
        items.append(
            {
                "evaluation_id": ev.id,
                "tenant_name": ev.tenant_name,
                "sector": ev.sector,
                "year": ev.year,
                "regulators": (ev.regulators or {}).get("items", []) if isinstance(ev.regulators, dict) else [],
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
            }
        )
    return {"count": len(items), "items": items}


@router.get("/evaluations/{evaluation_id}", status_code=status.HTTP_200_OK)
def get_evaluation(evaluation_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    ev = _ensure_evaluation_exists(db, evaluation_id)
    return {
        "evaluation_id": ev.id,
        "tenant_name": ev.tenant_name,
        "sector": ev.sector,
        "year": ev.year,
        "regulators": (ev.regulators or {}).get("items", []) if isinstance(ev.regulators, dict) else [],
        "instrument": {
            "template_code": ev.instrument_template_code,
            "version": ev.instrument_version,
        },
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


# -------------------------
# Participants
# -------------------------

@router.post("/evaluations/{evaluation_id}/participants/invite", status_code=status.HTTP_201_CREATED)
def invite_participants(
    evaluation_id: str,
    payload: InviteParticipantsRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    ev = _ensure_evaluation_exists(db, evaluation_id)
    tenant_name = ev.tenant_name

    if not payload.participants:
        raise HTTPException(status_code=400, detail="participants list cannot be empty")

    created = 0
    skipped_existing = 0

    created_items: List[Dict[str, Any]] = []
    existing_items: List[Dict[str, Any]] = []

    for item in payload.participants:
        email = (item.email or "").strip().lower()
        if not email:
            continue

        existing = _find_participant_by_email(db, evaluation_id, email)

        # Existing participant
        if existing:
            skipped_existing += 1

            # Backfill token if missing
            if not (getattr(existing, "access_token", "") or "").strip():
                existing.access_token = _generate_unique_participant_access_token(db)
                existing.token_created_at = _utcnow()
                db.add(existing)
                db.flush()

            portal_url = _make_portal_url(existing.access_token)

            email_ok, email_err = send_invite_email(
                to_email=existing.email,
                full_name=existing.full_name,
                portal_url=portal_url,
                evaluation_id=evaluation_id,
                tenant_name=tenant_name,
            )

            existing_items.append(
                {
                    "participant_id": str(existing.id),
                    "email": existing.email,
                    "full_name": existing.full_name,
                    "role": existing.role,
                    "status": existing.status,
                    "invited_at": existing.invited_at.isoformat() if existing.invited_at else None,
                    "responded_at": existing.responded_at.isoformat() if existing.responded_at else None,
                    "portal_url": portal_url,
                    "email_sent": bool(email_ok),
                    "email_error": email_err,
                }
            )
            continue

        # New participant
        token = _generate_unique_participant_access_token(db)

        p = Participant(
            evaluation_id=evaluation_id,
            email=email,
            full_name=item.full_name,
            role=item.role,
            status="invited",
            invited_at=_utcnow(),
            access_token=token,
            token_created_at=_utcnow(),
        )
        db.add(p)
        db.flush()
        created += 1

        portal_url = _make_portal_url(p.access_token)

        email_ok, email_err = send_invite_email(
            to_email=p.email,
            full_name=p.full_name,
            portal_url=portal_url,
            evaluation_id=evaluation_id,
            tenant_name=tenant_name,
        )

        created_items.append(
            {
                "participant_id": str(p.id),
                "email": p.email,
                "full_name": p.full_name,
                "role": p.role,
                "status": p.status,
                "invited_at": p.invited_at.isoformat() if p.invited_at else None,
                "responded_at": p.responded_at.isoformat() if p.responded_at else None,
                "portal_url": portal_url,
                "email_sent": bool(email_ok),
                "email_error": email_err,
            }
        )

    db.commit()

    total_now = (
        db.execute(select(Participant).where(Participant.evaluation_id == evaluation_id))
        .scalars()
        .all()
    )

    return {
        "evaluation_id": evaluation_id,
        "created": created,
        "skipped_existing": skipped_existing,
        "total_now": len(total_now),
        "created_items": created_items,
        "existing_items": existing_items,
    }


@router.get("/evaluations/{evaluation_id}/participants", status_code=status.HTTP_200_OK)
def list_participants(evaluation_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _ensure_evaluation_exists(db, evaluation_id)

    rows = (
        db.execute(
            select(Participant)
            .where(Participant.evaluation_id == evaluation_id)
            .order_by(Participant.created_at.asc())
        )
        .scalars()
        .all()
    )

    items = []
    backfilled = False

    for p in rows:
        # Safety/backfill: ensure token exists (handles old records)
        if not (getattr(p, "access_token", "") or "").strip():
            p.access_token = _generate_unique_participant_access_token(db)
            p.token_created_at = _utcnow()
            db.add(p)
            db.flush()
            backfilled = True

        items.append(
            {
                "participant_id": str(p.id),
                "email": p.email,
                "full_name": p.full_name,
                "role": p.role,
                "status": p.status,
                "invited_at": p.invited_at.isoformat() if p.invited_at else None,
                "responded_at": p.responded_at.isoformat() if p.responded_at else None,
                "portal_url": _make_portal_url(p.access_token),
            }
        )

    if backfilled:
        db.commit()

    return {"evaluation_id": evaluation_id, "count": len(items), "items": items}


# -------------------------
# Instrument selection (belongs to Evaluation)
# -------------------------

@router.patch("/evaluations/{evaluation_id}/instrument")
def set_evaluation_instrument(
    evaluation_id: str,
    payload: SetInstrumentRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    ev = _ensure_evaluation_exists(db, evaluation_id)

    ev.instrument_template_code = (payload.template_code or "DEFAULT").strip()
    ev.instrument_version = int(payload.version or 1)

    db.commit()
    db.refresh(ev)

    return {
        "updated": True,
        "evaluation_id": ev.id,
        "instrument": {"template_code": ev.instrument_template_code, "version": ev.instrument_version},
    }


# -------------------------
# NEW: Assignments
# -------------------------

#@router.post("/evaluations/{evaluation_id}/assignments/bulk", status_code=status.HTTP_201_CREATED)
def bulk_create_assignments_old(
    evaluation_id: str,
    payload: BulkCreateAssignmentsRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Bulk create granular assessment assignments (idempotent via uq_assignment_dedup).

    Important:
    - Respondent and (optional) subject are resolved via Participant records by email.
    - Each assignment gets its own access_token (per-assignment token).
    """
    ev = _ensure_evaluation_exists(db, evaluation_id)

    if not payload.assignments:
        raise HTTPException(status_code=400, detail="assignments list cannot be empty")

    created = 0
    skipped_existing = 0
    created_items: List[Dict[str, Any]] = []
    existing_items: List[Dict[str, Any]] = []

    for item in payload.assignments:
        respondent = _find_participant_by_email(db, evaluation_id, item.respondent_email)
        if not respondent:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown respondent_email (not invited as participant in this evaluation): {item.respondent_email}",
            )

        subject = None
        if item.subject_email:
            subject = _find_participant_by_email(db, evaluation_id, item.subject_email)
            if not subject:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown subject_email (not a participant in this evaluation): {item.subject_email}",
                )

        atype = (item.assignment_type or "BOARD_AS_WHOLE").strip().upper()
        committee = (item.committee_name or "").strip() or None

        # default instrument from evaluation if not provided
        tcode = (item.instrument_template_code or ev.instrument_template_code or "DEFAULT").strip()
        ver = int(item.instrument_version or ev.instrument_version or 1)

        # idempotent lookup (mirrors uq_assignment_dedup)
        existing = (
            db.execute(
                select(AssessmentAssignment)
                .where(AssessmentAssignment.evaluation_id == evaluation_id)
                .where(AssessmentAssignment.respondent_participant_id == respondent.id)
                .where(AssessmentAssignment.assignment_type == atype)
                .where(AssessmentAssignment.subject_participant_id == (subject.id if subject else None))
                .where(AssessmentAssignment.committee_name == committee)
            )
            .scalars()
            .first()
        )

        if existing:
            skipped_existing += 1

            # backfill token if missing
            if not (getattr(existing, "access_token", "") or "").strip():
                existing.access_token = _generate_unique_assignment_access_token(db)
                existing.token_created_at = _utcnow()
                db.add(existing)
                db.flush()

            existing_items.append(
                {
                    "assignment_id": str(existing.id),
                    "evaluation_id": existing.evaluation_id,
                    "respondent_participant_id": str(existing.respondent_participant_id),
                    "subject_participant_id": str(existing.subject_participant_id) if existing.subject_participant_id else None,
                    "assignment_type": existing.assignment_type,
                    "committee_name": existing.committee_name,
                    "instrument_template_code": existing.instrument_template_code,
                    "instrument_version": existing.instrument_version,
                    "status": existing.status,
                    "invited_at": existing.invited_at.isoformat() if existing.invited_at else None,
                    "responded_at": existing.responded_at.isoformat() if existing.responded_at else None,
                    "portal_url": _make_portal_url(existing.access_token),
                }
            )
            continue

        token = _generate_unique_assignment_access_token(db)

        a = AssessmentAssignment(
            evaluation_id=evaluation_id,
            respondent_participant_id=respondent.id,
            subject_participant_id=(subject.id if subject else None),
            assignment_type=atype,
            committee_name=committee,
            instrument_template_code=tcode,
            instrument_version=ver,
            access_token=token,
            token_created_at=_utcnow(),
            status="invited",
            invited_at=_utcnow(),
        )
        db.add(a)
        db.flush()
        created += 1

        created_items.append(
            {
                "assignment_id": str(a.id),
                "evaluation_id": a.evaluation_id,
                "respondent_participant_id": str(a.respondent_participant_id),
                "subject_participant_id": str(a.subject_participant_id) if a.subject_participant_id else None,
                "assignment_type": a.assignment_type,
                "committee_name": a.committee_name,
                "instrument_template_code": a.instrument_template_code,
                "instrument_version": a.instrument_version,
                "status": a.status,
                "invited_at": a.invited_at.isoformat() if a.invited_at else None,
                "responded_at": a.responded_at.isoformat() if a.responded_at else None,
                "portal_url": _make_portal_url(a.access_token),
            }
        )

    db.commit()

    total_now = (
        db.execute(select(AssessmentAssignment).where(AssessmentAssignment.evaluation_id == evaluation_id))
        .scalars()
        .all()
    )

    return {
        "evaluation_id": evaluation_id,
        "created": created,
        "skipped_existing": skipped_existing,
        "total_now": len(total_now),
        "created_items": created_items,
        "existing_items": existing_items,
    }


@router.get("/evaluations/{evaluation_id}/assignments", status_code=status.HTTP_200_OK)
def list_assignments(evaluation_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    _ensure_evaluation_exists(db, evaluation_id)

    rows = (
        db.execute(
            select(AssessmentAssignment)
            .where(AssessmentAssignment.evaluation_id == evaluation_id)
            .order_by(AssessmentAssignment.created_at.asc())
        )
        .scalars()
        .all()
    )

    items: List[Dict[str, Any]] = []
    backfilled = False

    for a in rows:
        if not (getattr(a, "access_token", "") or "").strip():
            a.access_token = _generate_unique_assignment_access_token(db)
            a.token_created_at = _utcnow()
            db.add(a)
            db.flush()
            backfilled = True

        items.append(
            {
                "assignment_id": str(a.id),
                "evaluation_id": a.evaluation_id,
                "respondent_participant_id": str(a.respondent_participant_id),
                "subject_participant_id": str(a.subject_participant_id) if a.subject_participant_id else None,
                "assignment_type": a.assignment_type,
                "committee_name": a.committee_name,
                "instrument_template_code": a.instrument_template_code,
                "instrument_version": a.instrument_version,
                "status": a.status,
                "invited_at": a.invited_at.isoformat() if a.invited_at else None,
                "responded_at": a.responded_at.isoformat() if a.responded_at else None,
                "portal_url": _make_portal_url(a.access_token),
            }
        )

    if backfilled:
        db.commit()

    return {"evaluation_id": evaluation_id, "count": len(items), "items": items}


# -------------------------
# Seed demo responses (kept here)
# -------------------------

@router.post("/evaluations/{evaluation_id}/seed/demo-responses", status_code=status.HTTP_201_CREATED)
def seed_demo_responses(
    evaluation_id: str,
    template_code: str = "DEFAULT",
    version: int = 1,
    invited: int = 12,
    responded: int = 10,
    random_seed: int = 42,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if responded > invited:
        raise HTTPException(status_code=400, detail="responded cannot be greater than invited")

    _ensure_evaluation_exists(db, evaluation_id)

    questions = _get_questions_for_instrument(db, template_code=template_code, version=version)
    if not questions:
        raise HTTPException(status_code=400, detail="No questions found. Seed questions first.")

    rnd = random.Random(random_seed)

    roles = ["Chair", "INED", "INED", "ED", "ED", "Company Secretary"]
    created_participants = 0
    participants: List[Participant] = []

    # Create participants idempotently
    for i in range(invited):
        email = f"board_member_{i+1:02d}@demo-client.test"

        existing_p = _find_participant_by_email(db, evaluation_id, email)
        if existing_p:
            participants.append(existing_p)
            continue

        p = Participant(
            evaluation_id=evaluation_id,
            email=email,
            full_name=f"Board Member {i+1:02d}",
            role=roles[i % len(roles)],
            status="invited",
            invited_at=_utcnow(),
        )
        db.add(p)
        db.flush()
        participants.append(p)
        created_participants += 1

    db.commit()

    rating_questions = [q for q in questions if q.answer_type == "rating"]
    comment_questions = [q for q in questions if q.answer_type == "comment"]

    created_responses = 0
    updated_participants = 0

    for idx, p in enumerate(participants):
        if idx >= responded:
            continue

        if p.status != "responded":
            p.status = "responded"
            p.responded_at = _utcnow()
            updated_participants += 1

        for q in rating_questions:
            base = rnd.choices([2, 3, 4, 5], weights=[5, 25, 45, 25])[0]
            if q.dimension == "Risk Oversight":
                base = max(2, base - 1)

            exists = (
                db.execute(
                    select(Response)
                    .where(Response.participant_id == p.id)
                    .where(Response.question_id == q.id)
                )
                .scalars()
                .first()
            )
            if exists:
                continue

            db.add(
                Response(
                    evaluation_id=evaluation_id,
                    participant_id=p.id,
                    question_id=q.id,
                    score=int(base),
                    comment=None,
                )
            )
            created_responses += 1

        if idx < 3 and comment_questions:
            strengths_text = (
                "Board meetings are well-structured and adequately documented. "
                "Independent directors demonstrate robust challenge and debate. "
                "Committees provide clear oversight and escalate matters appropriately."
            )
            weaknesses_text = (
                "Limited board-level oversight of emerging technology and cyber risks. "
                "CEO performance evaluation and succession planning not fully formalized. "
                "Risk appetite statements not consistently reviewed and documented annually."
            )

            for q in comment_questions:
                exists = (
                    db.execute(
                        select(Response)
                        .where(Response.participant_id == p.id)
                        .where(Response.question_id == q.id)
                    )
                    .scalars()
                    .first()
                )
                if exists:
                    continue

                comment = strengths_text if "strength" in (q.text or "").lower() else weaknesses_text
                db.add(
                    Response(
                        evaluation_id=evaluation_id,
                        participant_id=p.id,
                        question_id=q.id,
                        score=None,
                        comment=comment,
                    )
                )
                created_responses += 1

    db.commit()

    return {
        "evaluation_id": evaluation_id,
        "template_code": template_code,
        "version": version,
        "participants_created": created_participants,
        "participants_marked_responded": updated_participants,
        "responses_created": created_responses,
        "invited": invited,
        "responded": responded,
    }
