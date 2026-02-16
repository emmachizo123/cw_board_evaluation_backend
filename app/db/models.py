"""
app.db.models
-------------
SQLAlchemy models for the C&W Board Evaluation platform (MVP).

Includes:
- Evaluation: one evaluation cycle (client + year + sector)
- Report: AI-generated report persisted as JSON
- Participant: invited board evaluator(s)
- AssessmentAssignment: granular assessment tasks with per-assignment token (NEW)
- Question: the questionnaire/instrument definition (versionable)
- Response: answers submitted by participants to questions

Note:
- For MVP we use create_all() elsewhere to create new tables.
- For production, use Alembic migrations (create_all won't alter existing columns).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ----------------------------
# Core: Evaluations + Reports
# ----------------------------

class Evaluation(Base):
    """One board evaluation cycle for a client (e.g., Demo Client Plc, 2025)."""

    __tablename__ = "evaluations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "eval-001"
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sector: Mapped[str] = mapped_column(String(100), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)

    # Keep instrument selection on evaluation (used as default for assignments)
    instrument_template_code: Mapped[str] = mapped_column(String(64), nullable=False, default="DEFAULT")
    instrument_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # JSON like {"items": ["NAICOM","FRC"]}
    regulators: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        default=dict,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    reports: Mapped[list["Report"]] = relationship(
        back_populates="evaluation",
        cascade="all, delete-orphan",
    )
    participants: Mapped[list["Participant"]] = relationship(
        back_populates="evaluation",
        cascade="all, delete-orphan",
    )
    responses: Mapped[list["Response"]] = relationship(
        back_populates="evaluation",
        cascade="all, delete-orphan",
    )

    # ✅ NEW: granular assignments (per-assignment tokens)
    assignments: Mapped[list["AssessmentAssignment"]] = relationship(
        back_populates="evaluation",
        cascade="all, delete-orphan",
    )

    # ✅ NEW: track configs enabled for this evaluation (multi-track engine)
    tracks: Mapped[list["EvaluationTrack"]] = relationship(
        back_populates="evaluation",
        cascade="all, delete-orphan",
    )


    __table_args__ = (
        Index("ix_evaluations_tenant_year", "tenant_name", "year"),
    )


class AssessmentTrackTemplate(Base):
    """
    A reusable "track definition" (global library).

    Examples:
    - BOARD_AS_WHOLE
    - CHAIR_EVAL (INED-only raters)
    - DIRECTOR_SELF
    - DIRECTOR_PEER
    - COMMITTEE_EVAL
    - GOV_AUDIT

    These templates get enabled per evaluation via EvaluationTrack.
    """

    __tablename__ = "assessment_track_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # e.g. "CHAIR_EVAL"
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Maps to AssessmentAssignment.assignment_type
    assignment_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Subject behavior:
    # "NONE" | "PARTICIPANT_ROLE" | "PARTICIPANT_SELF" | "PARTICIPANT_EACH" | "COMMITTEE"
    subject_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE")
    subject_role: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Stored as JSON for flexibility:
    # {"mode":"ALL"} or {"mode":"ROLE_IN","roles":["INED"]}
    respondent_rule: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        default=dict,
        nullable=False,
    )

    # Default instrument for this track (overridable per evaluation)
    default_template_code: Mapped[str] = mapped_column(String(64), nullable=False, default="DEFAULT")
    default_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    evaluation_tracks: Mapped[list["EvaluationTrack"]] = relationship(
        back_populates="track_template",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_track_templates_code", "code"),
        Index("ix_track_templates_active", "active"),
    )


class EvaluationTrack(Base):
    """
    Per-evaluation configuration: which track templates are enabled for an evaluation,
    plus optional per-evaluation overrides (instrument + config).

    This is what the consultant UX will manage.
    """

    __tablename__ = "evaluation_tracks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )

    track_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_track_templates.id", ondelete="RESTRICT"),
        nullable=False,
    )

    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Optional per-evaluation instrument override (if null, use template default -> else evaluation default)
    instrument_template_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    instrument_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Track-specific config (committees list, inclusion rules, etc.)
    config: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        default=dict,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    evaluation: Mapped["Evaluation"] = relationship(back_populates="tracks")
    track_template: Mapped["AssessmentTrackTemplate"] = relationship(back_populates="evaluation_tracks")

    __table_args__ = (
        UniqueConstraint("evaluation_id", "track_template_id", name="uq_eval_track"),
        Index("ix_eval_tracks_eval", "evaluation_id"),
        Index("ix_eval_tracks_tpl", "track_template_id"),
        Index("ix_eval_tracks_enabled", "enabled"),
    )

class Report(Base):
    """
    AI-generated report saved as structured JSON (institutional memory).

    IMPORTANT:
    If your Postgres schema has:
      reports.id = character varying(64)

    then this model MUST use String(64) for id (not UUID),
    otherwise SQLAlchemy may query WHERE id = $1::UUID and Postgres will fail.
    """

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # store str(uuid.uuid4()) in code
    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)

    summary_json: Mapped[Dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSONB),
        default=dict,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    evaluation: Mapped["Evaluation"] = relationship(back_populates="reports")

    __table_args__ = (
        Index("ix_reports_evaluation_created_at", "evaluation_id", "created_at"),
    )


# ----------------------------
# Participants / Assignments / Questions / Responses
# ----------------------------

class Participant(Base):
    """
    An invited evaluator (director / board member / company secretary).

    NOTE:
    - In the new granular model, tokens should be per-assignment.
    - We keep Participant.access_token for backward compatibility with the existing portal flow.

    Important fix:
    - access_token is nullable to avoid unique-constraint collisions on default "".
      (Postgres treats "" as a real value, so multiple rows would violate uniqueness.)
    """

    __tablename__ = "participants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # ✅ FIX: nullable token (avoid duplicate "" clashes with unique constraint)
    access_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    token_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Examples: "Chair", "INED", "ED", "Company Secretary"
    role: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="invited", nullable=False)

    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    evaluation: Mapped["Evaluation"] = relationship(back_populates="participants")
    responses: Mapped[list["Response"]] = relationship(
        back_populates="participant",
        cascade="all, delete-orphan",
    )

    # ✅ NEW: assignments where this participant is the respondent (the one who fills the form)
    assignments_as_respondent: Mapped[list["AssessmentAssignment"]] = relationship(
        back_populates="respondent",
        foreign_keys=lambda: [AssessmentAssignment.respondent_participant_id],
        cascade="all, delete-orphan",
    )

    # ✅ NEW: assignments where this participant is the subject (the person/thing being evaluated)
    assignments_as_subject: Mapped[list["AssessmentAssignment"]] = relationship(
        back_populates="subject",
        foreign_keys=lambda: [AssessmentAssignment.subject_participant_id],
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_participants_evaluation", "evaluation_id"),
        Index("ix_participants_email", "email"),
        Index("ix_participants_access_token", "access_token"),
        UniqueConstraint("evaluation_id", "email", name="uq_participant_eval_email"),
        UniqueConstraint("access_token", name="uq_participant_access_token"),
    )


class AssessmentAssignment(Base):
    """
    A granular assessment task within an evaluation, with its own access token.

    Why this exists:
    - Your real-world process requires multiple distinct assessments per evaluation:
      board-as-a-whole, self assessments, peer reviews, chair evaluations, committee evaluations,
      CEO evaluations, ED evaluations, governance audit, etc.
    - Each *assignment* should have its own token and status so we can track completion accurately.

    Minimal-change design:
    - We do NOT remove Participant or Response. We add this table first.
    - Next step (later): link responses to assignment (either by adding assignment_id to responses,
      or introducing an assignment_responses table).
    """

    __tablename__ = "assessment_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Who fills the assessment
    respondent_participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Who/what is being evaluated (nullable for things like "Board as a whole", "Committee", "Governance Audit")
    subject_participant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("participants.id", ondelete="SET NULL"),
        nullable=True,
    )

    # e.g.:
    # "BOARD_AS_WHOLE" | "DIRECTOR_SELF" | "DIRECTOR_PEER" | "CHAIR_EVAL" | "COMMITTEE_EVAL"
    # "CEO_SELF" | "CEO_EVAL" | "ED_SELF" | "ED_EVAL" | "GOV_AUDIT"
    assignment_type: Mapped[str] = mapped_column(String(64), nullable=False, default="BOARD_AS_WHOLE")

    # Optional detail fields (useful for reporting + UX)
    committee_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Each assignment can point to an instrument (defaults to evaluation’s instrument)
    instrument_template_code: Mapped[str] = mapped_column(String(64), nullable=False, default="DEFAULT")
    instrument_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # ✅ FIX: nullable token (avoid duplicate "" clashes with unique constraint)
    access_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=None)
    token_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="invited", nullable=False)

    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    evaluation: Mapped["Evaluation"] = relationship(back_populates="assignments")

    respondent: Mapped["Participant"] = relationship(
        back_populates="assignments_as_respondent",
        foreign_keys=lambda: [AssessmentAssignment.respondent_participant_id],
    )

    subject: Mapped[Optional["Participant"]] = relationship(
        back_populates="assignments_as_subject",
        foreign_keys=lambda: [AssessmentAssignment.subject_participant_id],
    )

    __table_args__ = (
        Index("ix_assignments_evaluation", "evaluation_id"),
        Index("ix_assignments_respondent", "respondent_participant_id"),
        Index("ix_assignments_subject", "subject_participant_id"),
        Index("ix_assignments_token", "access_token"),
        UniqueConstraint("access_token", name="uq_assignment_access_token"),
        # Prevent accidental duplicates for common cases:
        # same respondent doing same type for same subject/committee within same evaluation
        UniqueConstraint(
            "evaluation_id",
            "respondent_participant_id",
            "assignment_type",
            "subject_participant_id",
            "committee_name",
            name="uq_assignment_dedup",
        ),
    )


class Question(Base):
    """
    A question in the board evaluation instrument.

    For versioning/template control:
    - template_code groups a set of questions (e.g., "INSURANCE_V1")
    - version increments when the instrument changes
    """

    __tablename__ = "questions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    template_code: Mapped[str] = mapped_column(String(64), default="DEFAULT", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # e.g. "Risk Oversight", "Board Composition"
    dimension: Mapped[str] = mapped_column(String(120), nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # "rating" | "yesno" | "comment"
    answer_type: Mapped[str] = mapped_column(String(32), default="rating", nullable=False)

    # weighting supports dimension scoring
    weight: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # SQLite-friendly bool; for Postgres this is fine too
    active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    responses: Mapped[list["Response"]] = relationship(back_populates="question")

    __table_args__ = (
        Index("ix_questions_template_version", "template_code", "version"),
        Index("ix_questions_dimension", "dimension"),
        UniqueConstraint("template_code", "version", "text", name="uq_question_template_version_text"),
    )


class Response(Base):
    """
    One participant's answer to one question.

    For rating questions:
    - score: 1..5 (or 1..10 later)
    For comment questions:
    - comment contains the text

    NOTE:
    - For minimal changes, this still links to participant + evaluation + question.
    - Next step (later): associate responses to AssessmentAssignment (assignment_id)
      so the same participant can submit multiple distinct assessments cleanly.
    """

    __tablename__ = "responses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)



    evaluation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("participants.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )

    assignment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_assignments.id", ondelete="CASCADE"),
        nullable=True,
    )

    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    evaluation: Mapped["Evaluation"] = relationship(back_populates="responses")
    participant: Mapped["Participant"] = relationship(back_populates="responses")
    question: Mapped["Question"] = relationship(back_populates="responses")
    assignment: Mapped[Optional["AssessmentAssignment"]] = relationship()







    __table_args__ = (
        UniqueConstraint("assignment_id", "question_id", name="uq_response_assignment_question"),
        Index("ix_responses_assignment", "assignment_id"),
        Index("ix_responses_evaluation", "evaluation_id"),
        Index("ix_responses_question", "question_id"),
        Index("ix_responses_participant", "participant_id"),
    )
