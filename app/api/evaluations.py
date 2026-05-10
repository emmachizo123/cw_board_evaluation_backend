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

import random
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError


from app.db.session import get_db
from app.db.models import (
    Evaluation,
    Participant,
    Question,
    Response,
    AssessmentAssignment,
    AssessmentTrackTemplate,
    EvaluationTrack,
)

from app.services.email_service import send_invite_email
from app.util.portal_urls import assignment_portal_url

router = APIRouter()

# Backwards-compatible alias (assignment tokens only; not participant tokens)
_make_portal_url = assignment_portal_url


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


def _assignment_portal_urls_for_respondent(
    db: Session,
    evaluation_id: str,
    respondent_participant_id,
) -> List[str]:
    """Full portal URLs for assignments where this participant is the respondent (ordered by created_at)."""
    rows = (
        db.execute(
            select(AssessmentAssignment)
            .where(AssessmentAssignment.evaluation_id == evaluation_id)
            .where(AssessmentAssignment.respondent_participant_id == respondent_participant_id)
            .order_by(AssessmentAssignment.created_at.asc())
        )
        .scalars()
        .all()
    )
    out: List[str] = []
    for a in rows:
        t = (getattr(a, "access_token", None) or "").strip()
        if t:
            out.append(assignment_portal_url(t))
    return out


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

def _resolve_track_template(
    db: Session,
    *,
    code: Optional[str] = None,
    track_template_id: Optional[str] = None,
) -> AssessmentTrackTemplate:
    """
    Resolve a track template by code or UUID string.
    """
    if track_template_id:
        try:
            # SQLAlchemy UUID PK type will accept a UUID object;
            # db.get() also accepts UUID-like values if the model uses UUID(as_uuid=True).
            tpl = db.get(AssessmentTrackTemplate, track_template_id)
        except Exception:
            tpl = None
        if not tpl:
            raise HTTPException(status_code=404, detail=f"Track template not found: {track_template_id}")
        return tpl

    c = (code or "").strip()
    if not c:
        raise HTTPException(status_code=400, detail="Track template 'code' or 'track_template_id' is required.")

    tpl = (
        db.execute(
            select(AssessmentTrackTemplate).where(AssessmentTrackTemplate.code == c)
        )
        .scalars()
        .first()
    )
    if not tpl:
        raise HTTPException(status_code=404, detail=f"Track template not found: code={c}")

    return tpl


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


def _role_norm(s: Optional[str]) -> str:
    return _norm(s).lower()


def _email_norm(s: Optional[str]) -> str:
    return _norm(s).lower()


def _respondent_allowed(p: Participant, rule: Dict[str, Any]) -> bool:
    """
    Apply respondent_rule JSON.

    Supported:
      {"mode":"ALL"}
      {"mode":"ROLE_IN","roles":["INED"]}
      {"mode":"ROLE_NOT_IN","roles":["ED"]}
      {"mode":"EMAIL_IN","emails":["a@b.com"]}
      {"mode":"EMAIL_NOT_IN","emails":["a@b.com"]}
    """
    rule = rule or {}
    mode = _norm(rule.get("mode")).upper() or "ALL"

    roles = [r for r in (rule.get("roles") or []) if _norm(r)]
    emails = [e for e in (rule.get("emails") or []) if _norm(e)]

    p_role = _role_norm(p.role)
    p_email = _email_norm(p.email)

    if mode == "ALL":
        return True

    if mode == "ROLE_IN":
        wanted = {_role_norm(x) for x in roles}
        return bool(p_role) and p_role in wanted

    if mode == "ROLE_NOT_IN":
        blocked = {_role_norm(x) for x in roles}
        return not (bool(p_role) and p_role in blocked)

    if mode == "EMAIL_IN":
        wanted = {_email_norm(x) for x in emails}
        return bool(p_email) and p_email in wanted

    if mode == "EMAIL_NOT_IN":
        blocked = {_email_norm(x) for x in emails}
        return not (bool(p_email) and p_email in blocked)

    # Unknown mode => safest default: allow none? (but MVP wants progress)
    # We'll default to ALL to avoid “silent nothing”.
    return True


def _pick_instrument_for_track(ev: Evaluation, tpl: AssessmentTrackTemplate, et: EvaluationTrack) -> Dict[str, Any]:
    """
    Precedence (most specific first):
      evaluation_tracks override -> track template default -> evaluation default
    """
    tcode = _norm(et.instrument_template_code) or _norm(tpl.default_template_code) or _norm(
        ev.instrument_template_code) or "DEFAULT"
    ver = et.instrument_version or tpl.default_version or ev.instrument_version or 1
    return {"template_code": tcode, "version": int(ver)}


def _get_subjects_for_track(
        participants: List[Participant],
        *,
        subject_mode: str,
        subject_role: Optional[str],
        respondent: Participant,
) -> List[Optional[Participant]]:
    """
    Returns a list of subjects for a given respondent.
    (We return a list because some modes produce multiple subjects.)
    """
    sm = _norm(subject_mode).upper() or "NONE"

    if sm == "NONE":
        return [None]

    if sm == "PARTICIPANT_SELF":
        return [respondent]

    if sm == "PARTICIPANT_ROLE":
        role = _role_norm(subject_role)
        if not role:
            return [None]
        subs = [p for p in participants if _role_norm(p.role) == role]
        # If multiple match (rare but possible), create one assignment per matching subject.
        return subs or [None]

    if sm == "PARTICIPANT_EACH":
        # all other participants except self
        return [p for p in participants if p.id != respondent.id]

    # COMMITTEE handled elsewhere because it produces committee_name variants
    return [None]


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


class EnableTrackItem(BaseModel):
    """
    Enable one track for an evaluation.

    You can reference the track template either by:
      - code (preferred), e.g. "CHAIR_EVAL"
      - track_template_id (UUID string)

    Optional overrides:
      - instrument_template_code, instrument_version
      - config (jsonb)
    """
    code: Optional[str] = Field(default=None, description="Track template code (e.g. CHAIR_EVAL).")
    track_template_id: Optional[str] = Field(default=None, description="Track template UUID (string).")

    enabled: int = Field(default=1, description="1=enabled, 0=disabled")

    instrument_template_code: Optional[str] = None
    instrument_version: Optional[int] = None

    config: Dict[str, Any] = Field(default_factory=dict)


class EnableTracksRequest(BaseModel):
    tracks: List[EnableTrackItem] = Field(default_factory=list)


class GenerateAssignmentsRequest(BaseModel):
    """
    Generate AssessmentAssignment rows from enabled evaluation_tracks.

    dry_run=True will compute what WOULD be created, but will not write to DB.
    send_email_notifications: if True and dry_run is False, email each respondent
    their portal link(s) (requires EMAIL_ENABLED and SMTP).
    """
    dry_run: bool = Field(default=False)
    send_email_notifications: bool = Field(default=False)



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

@router.post("/evaluations/{evaluation_id}/tracks", status_code=status.HTTP_201_CREATED)
def enable_evaluation_tracks(
    evaluation_id: str,
    payload: EnableTracksRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Enable/configure tracks for an evaluation (writes to evaluation_tracks).

    Idempotent behavior:
      - If (evaluation_id, track_template_id) exists, we update enabled/overrides/config.
      - Otherwise we create it.

    This does NOT generate assignments yet.
    Step 3C will do: POST /evaluations/{evaluation_id}/tracks/generate-assignments
    """
    ev = _ensure_evaluation_exists(db, evaluation_id)

    if not payload.tracks:
        raise HTTPException(status_code=400, detail="tracks list cannot be empty")

    created = 0
    updated = 0

    items: List[Dict[str, Any]] = []

    for item in payload.tracks:
        tpl = _resolve_track_template(db, code=item.code, track_template_id=item.track_template_id)

        existing = (
            db.execute(
                select(EvaluationTrack)
                .where(EvaluationTrack.evaluation_id == evaluation_id)
                .where(EvaluationTrack.track_template_id == tpl.id)
            )
            .scalars()
            .first()
        )

        enabled_val = int(item.enabled or 0)

        # overrides are optional; NULL means "use template default / eval default later"
        tcode_override = (item.instrument_template_code or "").strip() or None
        ver_override = int(item.instrument_version) if item.instrument_version is not None else None

        cfg = item.config or {}

        if existing:
            existing.enabled = enabled_val
            existing.instrument_template_code = tcode_override
            existing.instrument_version = ver_override
            existing.config = cfg
            db.add(existing)
            db.flush()
            updated += 1

            items.append(
                {
                    "evaluation_track_id": str(existing.id),
                    "evaluation_id": existing.evaluation_id,
                    "track_template_id": str(existing.track_template_id),
                    "code": tpl.code,
                    "enabled": int(existing.enabled or 0),
                    "instrument_template_code": existing.instrument_template_code,
                    "instrument_version": existing.instrument_version,
                    "config": existing.config or {},
                }
            )
            continue

        et = EvaluationTrack(
            evaluation_id=evaluation_id,
            track_template_id=tpl.id,
            enabled=enabled_val,
            instrument_template_code=tcode_override,
            instrument_version=ver_override,
            config=cfg,
        )
        db.add(et)
        db.flush()
        created += 1

        items.append(
            {
                "evaluation_track_id": str(et.id),
                "evaluation_id": et.evaluation_id,
                "track_template_id": str(et.track_template_id),
                "code": tpl.code,
                "enabled": int(et.enabled or 0),
                "instrument_template_code": et.instrument_template_code,
                "instrument_version": et.instrument_version,
                "config": et.config or {},
            }
        )

    db.commit()

    return {
        "evaluation_id": ev.id,
        "created": created,
        "updated": updated,
        "count": len(items),
        "items": items,
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

            assignment_urls = _assignment_portal_urls_for_respondent(db, evaluation_id, existing.id)
            primary_portal_url = assignment_urls[0] if assignment_urls else None

            email_ok, email_err = send_invite_email(
                to_email=existing.email,
                full_name=existing.full_name,
                portal_url=primary_portal_url or "",
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
                    "portal_url": primary_portal_url,
                    "assignment_portal_urls": assignment_urls,
                    "portal_status": "ready" if assignment_urls else "pending_assignments",
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

        assignment_urls = _assignment_portal_urls_for_respondent(db, evaluation_id, p.id)
        primary_portal_url = assignment_urls[0] if assignment_urls else None

        email_ok, email_err = send_invite_email(
            to_email=p.email,
            full_name=p.full_name,
            portal_url=primary_portal_url or "",
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
                "portal_url": primary_portal_url,
                "assignment_portal_urls": assignment_urls,
                "portal_status": "ready" if assignment_urls else "pending_assignments",
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
        "portal_note": (
            "portal_url is an assignment questionnaire link (first of assignment_portal_urls). "
            "It is null until tracks are enabled and assignments are generated for this participant."
        ),
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

        assignment_urls = _assignment_portal_urls_for_respondent(db, evaluation_id, p.id)
        primary_portal_url = assignment_urls[0] if assignment_urls else None

        items.append(
            {
                "participant_id": str(p.id),
                "email": p.email,
                "full_name": p.full_name,
                "role": p.role,
                "status": p.status,
                "invited_at": p.invited_at.isoformat() if p.invited_at else None,
                "responded_at": p.responded_at.isoformat() if p.responded_at else None,
                "portal_url": primary_portal_url,
                "assignment_portal_urls": assignment_urls,
                "portal_status": "ready" if assignment_urls else "pending_assignments",
            }
        )

    if backfilled:
        db.commit()

    return {
        "evaluation_id": evaluation_id,
        "count": len(items),
        "items": items,
        "portal_note": (
            "portal_url is the first assignment questionnaire link for this participant. "
            "Use GET .../assignments for the full list. Null until assignments exist."
        ),
    }


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


@router.get("/evaluations/{evaluation_id}/tracks", status_code=status.HTTP_200_OK)
def list_evaluation_tracks(
    evaluation_id: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _ensure_evaluation_exists(db, evaluation_id)

    rows = (
        db.execute(
            select(EvaluationTrack)
            .where(EvaluationTrack.evaluation_id == evaluation_id)
            .order_by(EvaluationTrack.created_at.asc())
        )
        .scalars()
        .all()
    )

    # Resolve template code/name for each row
    tpl_ids = [r.track_template_id for r in rows]
    tpls = (
        db.execute(select(AssessmentTrackTemplate).where(AssessmentTrackTemplate.id.in_(tpl_ids)))
        .scalars()
        .all()
        if tpl_ids
        else []
    )
    tpl_by_id = {t.id: t for t in tpls}

    items: List[Dict[str, Any]] = []
    for r in rows:
        tpl = tpl_by_id.get(r.track_template_id)
        items.append(
            {
                "evaluation_track_id": str(r.id),
                "evaluation_id": r.evaluation_id,
                "track_template_id": str(r.track_template_id),
                "code": tpl.code if tpl else None,
                "name": tpl.name if tpl else None,
                "enabled": int(r.enabled or 0),
                "instrument_template_code": r.instrument_template_code,
                "instrument_version": r.instrument_version,
                "config": r.config or {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )

    return {"evaluation_id": evaluation_id, "count": len(items), "items": items}



@router.get("/track-templates", status_code=status.HTTP_200_OK)
def list_track_templates(
    active_only: bool = True,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    List the global Track Template library stored in DB.

    Query params:
      - active_only=true (default): returns only templates with active=1
      - active_only=false: returns all templates
    """
    q = select(AssessmentTrackTemplate)
    if active_only:
        q = q.where(AssessmentTrackTemplate.active == 1)

    rows = db.execute(q.order_by(AssessmentTrackTemplate.code.asc())).scalars().all()

    items: List[Dict[str, Any]] = []
    for t in rows:
        items.append(
            {
                "id": str(t.id),
                "code": t.code,
                "name": t.name,
                "description": t.description,
                "assignment_type": t.assignment_type,
                "subject_mode": t.subject_mode,
                "subject_role": t.subject_role,
                "respondent_rule": t.respondent_rule or {},
                "default_template_code": t.default_template_code,
                "default_version": int(t.default_version or 1),
                "active": int(t.active or 0),
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
        )

    return {"count": len(items), "items": items}

@router.post("/evaluations/{evaluation_id}/tracks/generate-assignments", status_code=status.HTTP_201_CREATED)
def generate_assignments_from_tracks(
    evaluation_id: str,
    payload: GenerateAssignmentsRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Step 3C:
    Generate AssessmentAssignment rows from enabled evaluation_tracks + track templates.

    Output includes:
      - created / existing counts
      - items with token + portal_url
    """
    ev = _ensure_evaluation_exists(db, evaluation_id)

    # Load participants for evaluation
    participants: List[Participant] = (
        db.execute(
            select(Participant).where(Participant.evaluation_id == evaluation_id)
        )
        .scalars()
        .all()
    )

    if not participants:
        raise HTTPException(status_code=400, detail="No participants found for this evaluation. Invite participants first.")

    # Load enabled evaluation tracks
    eval_tracks: List[EvaluationTrack] = (
        db.execute(
            select(EvaluationTrack)
            .where(EvaluationTrack.evaluation_id == evaluation_id)
            .where(EvaluationTrack.enabled == 1)
        )
        .scalars()
        .all()
    )

    if not eval_tracks:
        raise HTTPException(status_code=400, detail="No enabled tracks found for this evaluation. Enable tracks first (Step 3B).")

    # Load templates in one go
    tpl_ids = [et.track_template_id for et in eval_tracks]
    templates: List[AssessmentTrackTemplate] = (
        db.execute(
            select(AssessmentTrackTemplate).where(AssessmentTrackTemplate.id.in_(tpl_ids))
        )
        .scalars()
        .all()
    )
    tpl_by_id = {t.id: t for t in templates}

    created = 0
    existing = 0
    skipped = 0

    items: List[Dict[str, Any]] = []

    def _emit(a: AssessmentAssignment, tpl: AssessmentTrackTemplate) -> None:
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
                "access_token": a.access_token,
                "portal_url": _make_portal_url(a.access_token or ""),
                "track_code": tpl.code,
            }
        )

    # Build quick lookup for participants by email (for committee_members config)
    participant_by_email = {_email_norm(p.email): p for p in participants if _norm(p.email)}

    dry_run = bool(payload.dry_run)

    for et in eval_tracks:
        tpl = tpl_by_id.get(et.track_template_id)
        if not tpl:
            skipped += 1
            continue

        # track-level instrument selection
        inst = _pick_instrument_for_track(ev, tpl, et)
        template_code = inst["template_code"]
        version = int(inst["version"])

        assignment_type = _norm(tpl.assignment_type) or _norm(tpl.code)
        subject_mode = _norm(tpl.subject_mode)
        subject_role = tpl.subject_role

        respondent_rule = tpl.respondent_rule or {}
        cfg = et.config or {}

        # committee mode generates many “committee_name” assignments
        is_committee = _norm(subject_mode).upper() == "COMMITTEE"

        committees = []
        committee_members = {}
        exclude_subject_from_respondents = bool(cfg.get("exclude_subject_from_respondents", False))

        if is_committee:
            committees = [c for c in (cfg.get("committees") or []) if _norm(c)]
            committee_members = cfg.get("committee_members") or {}
            if not committees:
                # No committees configured => nothing to generate for this track
                skipped += 1
                continue

        for respondent in participants:
            # Respondent rule filter
            if not _respondent_allowed(respondent, respondent_rule):
                continue

            # Subject calculation
            if is_committee:
                # Optional: if committee_members is provided, only those members can respond for that committee
                for cname in committees:
                    members_emails = committee_members.get(cname) or []
                    members_emails_norm = {_email_norm(e) for e in members_emails if _norm(e)}

                    if members_emails_norm:
                        if _email_norm(respondent.email) not in members_emails_norm:
                            continue

                    # COMMITTEE track: subject is None, committee_name is set
                    subject = None
                    committee_name = _norm(cname) or None

                    # Idempotent lookup (mirrors uq_assignment_dedup)
                    already = (
                        db.execute(
                            select(AssessmentAssignment)
                            .where(AssessmentAssignment.evaluation_id == evaluation_id)
                            .where(AssessmentAssignment.respondent_participant_id == respondent.id)
                            .where(AssessmentAssignment.assignment_type == assignment_type)
                            .where(AssessmentAssignment.subject_participant_id.is_(None))
                            .where(AssessmentAssignment.committee_name == committee_name)
                        )
                        .scalars()
                        .first()
                    )

                    if already:
                        existing += 1
                        _emit(already, tpl)
                        continue

                    if dry_run:
                        created += 1
                        continue

                    token = _generate_unique_assignment_access_token(db)

                    a = AssessmentAssignment(
                        evaluation_id=evaluation_id,
                        respondent_participant_id=respondent.id,
                        subject_participant_id=None,
                        assignment_type=assignment_type,
                        committee_name=committee_name,
                        instrument_template_code=template_code,
                        instrument_version=version,
                        access_token=token,
                        token_created_at=_utcnow(),
                        status="invited",
                        invited_at=_utcnow(),
                    )
                    db.add(a)
                    try:
                        db.flush()
                    except IntegrityError:
                        db.rollback()
                        # another process probably created it; treat as existing
                        already2 = (
                            db.execute(
                                select(AssessmentAssignment)
                                .where(AssessmentAssignment.evaluation_id == evaluation_id)
                                .where(AssessmentAssignment.respondent_participant_id == respondent.id)
                                .where(AssessmentAssignment.assignment_type == assignment_type)
                                .where(AssessmentAssignment.subject_participant_id.is_(None))
                                .where(AssessmentAssignment.committee_name == committee_name)
                            )
                            .scalars()
                            .first()
                        )
                        if already2:
                            existing += 1
                            _emit(already2, tpl)
                            continue
                        raise

                    created += 1
                    _emit(a, tpl)

                continue  # next respondent

            # Non-committee tracks
            subjects = _get_subjects_for_track(
                participants,
                subject_mode=subject_mode,
                subject_role=subject_role,
                respondent=respondent,
            )

            for subject in subjects:
                # Optional: exclude subject from respondents (e.g. CEO should not rate CEO)
                if exclude_subject_from_respondents and subject and subject.id == respondent.id:
                    continue

                # Some subject_mode can return None if no matching subject found
                subject_id = subject.id if subject else None

                # Idempotent lookup (mirrors uq_assignment_dedup)
                already = (
                    db.execute(
                        select(AssessmentAssignment)
                        .where(AssessmentAssignment.evaluation_id == evaluation_id)
                        .where(AssessmentAssignment.respondent_participant_id == respondent.id)
                        .where(AssessmentAssignment.assignment_type == assignment_type)
                        .where(AssessmentAssignment.subject_participant_id == subject_id)
                        .where(AssessmentAssignment.committee_name.is_(None))
                    )
                    .scalars()
                    .first()
                )

                if already:
                    existing += 1
                    _emit(already, tpl)
                    continue

                if dry_run:
                    created += 1
                    continue

                token = _generate_unique_assignment_access_token(db)

                a = AssessmentAssignment(
                    evaluation_id=evaluation_id,
                    respondent_participant_id=respondent.id,
                    subject_participant_id=subject_id,
                    assignment_type=assignment_type,
                    committee_name=None,
                    instrument_template_code=template_code,
                    instrument_version=version,
                    access_token=token,
                    token_created_at=_utcnow(),
                    status="invited",
                    invited_at=_utcnow(),
                )
                db.add(a)
                try:
                    db.flush()
                except IntegrityError:
                    db.rollback()
                    already2 = (
                        db.execute(
                            select(AssessmentAssignment)
                            .where(AssessmentAssignment.evaluation_id == evaluation_id)
                            .where(AssessmentAssignment.respondent_participant_id == respondent.id)
                            .where(AssessmentAssignment.assignment_type == assignment_type)
                            .where(AssessmentAssignment.subject_participant_id == subject_id)
                            .where(AssessmentAssignment.committee_name.is_(None))
                        )
                        .scalars()
                        .first()
                    )
                    if already2:
                        existing += 1
                        _emit(already2, tpl)
                        continue
                    raise

                created += 1
                _emit(a, tpl)

    email_sent = 0
    email_failed: List[Dict[str, Any]] = []

    if not dry_run:
        db.commit()

        if payload.send_email_notifications and items:
            from collections import defaultdict
            from uuid import UUID as _UUID

            from app.services.email_service import send_assignment_links_digest_email

            tenant_name = ev.tenant_name
            by_respondent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for it in items:
                rid = it.get("respondent_participant_id")
                if rid:
                    by_respondent[str(rid)].append(it)

            for rid_str, group_items in by_respondent.items():
                p = db.get(Participant, _UUID(rid_str))
                if not p or not (p.email or "").strip():
                    email_failed.append({"respondent_participant_id": rid_str, "error": "no email on participant"})
                    continue
                ok, err = send_assignment_links_digest_email(
                    to_email=p.email,
                    full_name=p.full_name,
                    evaluation_id=evaluation_id,
                    tenant_name=tenant_name,
                    links=group_items,
                )
                if ok:
                    email_sent += 1
                else:
                    email_failed.append({"email": p.email, "error": err})

    return {
        "evaluation_id": evaluation_id,
        "dry_run": dry_run,
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "count": len(items) if not dry_run else None,
        "items": items if not dry_run else None,
        "email_sent": email_sent if not dry_run else None,
        "email_failed": email_failed if not dry_run and payload.send_email_notifications else None,
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
