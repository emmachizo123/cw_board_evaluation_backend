"""
app.api.evaluations
-------------------
Endpoints for managing evaluations and participants, plus seeding demo responses.

Separation of concerns:
- This module owns Evaluation + Participant + Response lifecycle flows.
- Question CRUD / instrument library operations live in app.api.questions.

Endpoints:
- POST /api/v1/evaluations
- GET  /api/v1/evaluations
- GET  /api/v1/evaluations/{evaluation_id}

- POST /api/v1/evaluations/{evaluation_id}/participants/invite
- GET  /api/v1/evaluations/{evaluation_id}/participants

- PATCH /api/v1/evaluations/{evaluation_id}/instrument

- POST /api/v1/evaluations/{evaluation_id}/seed/demo-responses
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Evaluation, Participant, Question, Response

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
    _ensure_evaluation_exists(db, evaluation_id)

    if not payload.participants:
        raise HTTPException(status_code=400, detail="participants list cannot be empty")

    created = 0
    skipped_existing = 0

    for item in payload.participants:
        email = (item.email or "").strip().lower()
        if not email:
            continue

        existing = (
            db.execute(
                select(Participant)
                .where(Participant.evaluation_id == evaluation_id)
                .where(Participant.email == email)
            )
            .scalars()
            .first()
        )
        if existing:
            skipped_existing += 1
            continue

        p = Participant(
            evaluation_id=evaluation_id,
            email=email,
            full_name=item.full_name,
            role=item.role,
            status="invited",
            invited_at=_utcnow(),
        )
        db.add(p)
        created += 1

    db.commit()

    total_now = db.execute(select(Participant).where(Participant.evaluation_id == evaluation_id)).scalars().all()
    return {
        "evaluation_id": evaluation_id,
        "created": created,
        "skipped_existing": skipped_existing,
        "total_now": len(total_now),
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
    for p in rows:
        items.append(
            {
                "participant_id": str(p.id),
                "email": p.email,
                "full_name": p.full_name,
                "role": p.role,
                "status": p.status,
                "invited_at": p.invited_at.isoformat() if p.invited_at else None,
                "responded_at": p.responded_at.isoformat() if p.responded_at else None,
            }
        )

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

        existing_p = (
            db.execute(
                select(Participant)
                .where(Participant.evaluation_id == evaluation_id)
                .where(Participant.email == email)
            )
            .scalars()
            .first()
        )
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
