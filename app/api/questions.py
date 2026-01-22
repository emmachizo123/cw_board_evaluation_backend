"""
app.api.questions
-----------------
All question/instrument endpoints for the C&W Board Evaluation platform.

Separation of concerns:
- Question CRUD + instrument library operations live here.
- Evaluation CRUD / participants / responses live in app.api.evaluations.

Endpoints:
- GET  /api/v1/evaluations/{evaluation_id}/questions
- POST /api/v1/evaluations/{evaluation_id}/questions
- POST /api/v1/evaluations/{evaluation_id}/questions/bulk
- PATCH /api/v1/questions/{question_id}

- POST /api/v1/evaluations/{evaluation_id}/seed/questions
- POST /api/v1/evaluations/{evaluation_id}/questionnaire/generate

- POST /api/v1/questions/clone
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from langchain.chat_models import init_chat_model

from app.db.models import Evaluation, Question
from app.db.session import get_db

load_dotenv(find_dotenv(), override=True)

router = APIRouter()


# -------------------------
# Helpers
# -------------------------

def _ensure_evaluation_exists(db: Session, evaluation_id: str) -> Evaluation:
    ev = db.get(Evaluation, evaluation_id)
    if not ev:
        raise HTTPException(status_code=404, detail=f"Evaluation not found: {evaluation_id}")
    return ev


def _clean_text_for_prompt(text: str, max_chars: int = 20_000) -> str:
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:max_chars]


def _parse_json_form_field(value: Optional[str], default):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _get_llm():
    chosen_model = os.getenv("LLM_MODEL", "gpt-4.1-mini")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required for real LLM calls.")

    return init_chat_model(
        chosen_model,
        model_provider="openai",
        api_key=api_key,
        temperature=0,
    )


async def _llm_generate_items(prompt: str) -> List[Dict[str, Any]]:
    """
    Calls your LangChain chat model and expects STRICT JSON with schema:
      {"items":[{dimension,text,answer_type,weight,active},...]}
    """
    llm = _get_llm()
    resp = await llm.ainvoke(prompt)
    content = getattr(resp, "content", resp)
    if not isinstance(content, str):
        content = str(content)

    s = content.strip()
    if s.startswith("```"):
        s = s.strip("`").strip()
        lines = s.splitlines()
        if lines and lines[0].strip().lower() == "json":
            s = "\n".join(lines[1:]).strip()

    data = json.loads(s)
    items = data.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValueError("LLM returned no items.")
    return items


def _fallback_extract_questions(source_text: str, n: int, allowed: List[str], hints: List[str]) -> List[Dict[str, Any]]:
    """
    Deterministic fallback: makes 'real-ish' questions from sentences,
    so demo works even if LLM output format fails.
    """
    base_dimension = hints[0] if hints else "Board Composition"

    def pick_dimension(seed: str) -> str:
        s = seed.lower()
        if "risk" in s or "cyber" in s or "compliance" in s:
            return "Risk Oversight"
        if "strategy" in s or "performance" in s or "kpi" in s:
            return "Strategy Oversight"
        if "culture" in s or "minutes" in s or "meeting" in s:
            return "Board Culture & Dynamics"
        return base_dimension

    candidates = [x.strip() for x in re.split(r"[.\n]+", source_text) if len(x.strip()) >= 40]
    if not candidates:
        candidates = [
            "The board receives timely, relevant and accurate information to enable effective oversight.",
            "The board demonstrates sufficient independence and diversity to challenge management decisions.",
            "The board actively oversees key enterprise risks, including regulatory and emerging risks.",
            "The board monitors execution against strategy using clear KPIs and effective governance processes.",
            "Board discussions encourage constructive challenge, candour, and healthy debate.",
        ]

    a_type = "rating" if "rating" in allowed else (allowed[0] if allowed else "rating")

    items: List[Dict[str, Any]] = []
    for i in range(n):
        seed = candidates[i % len(candidates)]
        items.append(
            {
                "dimension": pick_dimension(seed),
                "text": f"To what extent does the board ensure that: {seed}?",
                "answer_type": a_type,
                "weight": 1,
                "active": True,
            }
        )
    return items


# -------------------------
# Schemas
# -------------------------

class CreateQuestionRequest(BaseModel):
    template_code: str = "DEFAULT"
    version: int = 1
    dimension: str
    text: str
    answer_type: str = "rating"   # rating | yesno | comment
    weight: int = 1
    active: bool = True


class UpdateQuestionRequest(BaseModel):
    dimension: Optional[str] = None
    text: Optional[str] = None
    answer_type: Optional[str] = None
    weight: Optional[int] = None
    active: Optional[bool] = None


class BulkQuestionItem(BaseModel):
    dimension: str = Field(default="General", min_length=1)
    text: str = Field(min_length=1)
    answer_type: str = Field(default="rating")  # rating|yesno|comment
    weight: int = Field(default=1, ge=1, le=100)
    active: bool = Field(default=True)


class BulkCreateQuestionsRequest(BaseModel):
    template_code: str = Field(default="DEFAULT", min_length=1)
    version: int = Field(default=1, ge=1, le=10_000)
    items: List[BulkQuestionItem] = Field(default_factory=list)


class CloneInstrumentRequest(BaseModel):
    template_code: str = Field(default="DEFAULT", min_length=1)
    from_version: int = Field(default=1, ge=1, le=10_000)
    to_version: int = Field(..., ge=1, le=10_000)


# -------------------------
# List questions (single canonical endpoint)
# -------------------------

@router.get("/evaluations/{evaluation_id}/questions")
def list_questions(
    evaluation_id: str,
    template_code: str = "DEFAULT",
    version: int = 1,
    active_only: bool = False,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _ensure_evaluation_exists(db, evaluation_id)

    tc = (template_code or "DEFAULT").strip()
    v = int(version or 1)

    stmt = (
        select(Question)
        .where(Question.template_code == tc)
        .where(Question.version == v)
        .order_by(Question.dimension.asc(), Question.created_at.asc())
    )
    if active_only:
        stmt = stmt.where(Question.active == 1)

    rows = db.execute(stmt).scalars().all()

    return {
        "evaluation_id": evaluation_id,
        "template_code": tc,
        "version": v,
        "active_only": active_only,
        "count": len(rows),
        "items": [
            {
                "question_id": str(q.id),
                "template_code": q.template_code,
                "version": q.version,
                "dimension": q.dimension,
                "text": q.text,
                "answer_type": q.answer_type,
                "weight": q.weight,
                "active": bool(q.active),
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            for q in rows
        ],
    }


# -------------------------
# Create single question (idempotent on template+version+text)
# -------------------------

@router.post("/evaluations/{evaluation_id}/questions")
def create_question(evaluation_id: str, payload: CreateQuestionRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    ev = _ensure_evaluation_exists(db, evaluation_id)

    tc = (payload.template_code or ev.instrument_template_code or "DEFAULT").strip()
    v = int(payload.version or ev.instrument_version or 1)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Question text is required.")

    existing = db.execute(
        select(Question).where(
            Question.template_code == tc,
            Question.version == v,
            Question.text == text,
        )
    ).scalar_one_or_none()

    if existing:
        return {"created": False, "question_id": str(existing.id), "message": "Already exists."}

    q = Question(
        template_code=tc,
        version=v,
        dimension=(payload.dimension or "General").strip() or "General",
        text=text,
        answer_type=(payload.answer_type or "rating").strip().lower(),
        weight=int(payload.weight or 1),
        active=1 if payload.active else 0,
    )
    db.add(q)
    try:
        db.commit()
        db.refresh(q)
    except IntegrityError:
        db.rollback()
        # race condition: treat as idempotent
        existing2 = db.execute(
            select(Question).where(
                Question.template_code == tc,
                Question.version == v,
                Question.text == text,
            )
        ).scalar_one_or_none()
        if existing2:
            return {"created": False, "question_id": str(existing2.id), "message": "Already exists."}
        raise HTTPException(status_code=409, detail="Question already exists.")

    return {"created": True, "question_id": str(q.id), "message": "Question created."}


# -------------------------
# Bulk create (fixes React Add Selected)
# -------------------------

@router.post("/evaluations/{evaluation_id}/questions/bulk")
def bulk_create_questions(
    evaluation_id: str,
    payload: BulkCreateQuestionsRequest,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    ev = _ensure_evaluation_exists(db, evaluation_id)

    tc = (payload.template_code or ev.instrument_template_code or "DEFAULT").strip()
    v = int(payload.version or ev.instrument_version or 1)

    if not payload.items:
        raise HTTPException(status_code=400, detail="items cannot be empty")

    created = 0
    skipped_existing = 0
    errors: List[Dict[str, Any]] = []

    for idx, item in enumerate(payload.items):
        text = (item.text or "").strip()
        if not text:
            errors.append({"index": idx, "error": "empty text"})
            continue

        existing = db.execute(
            select(Question).where(
                Question.template_code == tc,
                Question.version == v,
                Question.text == text,
            )
        ).scalar_one_or_none()

        if existing:
            skipped_existing += 1
            continue

        q = Question(
            template_code=tc,
            version=v,
            dimension=(item.dimension or "General").strip() or "General",
            text=text,
            answer_type=(item.answer_type or "rating").strip().lower(),
            weight=int(item.weight or 1),
            active=1 if item.active else 0,
        )
        db.add(q)
        try:
            db.flush()
            created += 1
        except IntegrityError:
            db.rollback()
            skipped_existing += 1
        except Exception as e:
            db.rollback()
            errors.append({"index": idx, "error": str(e)})

    db.commit()

    return {
        "evaluation_id": evaluation_id,
        "template_code": tc,
        "version": v,
        "created": created,
        "skipped_existing": skipped_existing,
        "errors": errors,
    }


# -------------------------
# Update question (single PATCH endpoint)
# -------------------------

@router.patch("/questions/{question_id}")
def update_question(question_id: str, payload: UpdateQuestionRequest, db: Session = Depends(get_db)) -> Dict[str, Any]:
    try:
        q_uuid = uuid.UUID(question_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid question_id (must be UUID)")

    q = db.get(Question, q_uuid)
    if not q:
        raise HTTPException(status_code=404, detail="Question not found.")

    if payload.dimension is not None:
        q.dimension = payload.dimension.strip()
    if payload.text is not None:
        q.text = payload.text.strip()
    if payload.answer_type is not None:
        q.answer_type = payload.answer_type.strip().lower()
    if payload.weight is not None:
        q.weight = int(payload.weight)
    if payload.active is not None:
        q.active = 1 if payload.active else 0

    db.commit()
    db.refresh(q)

    return {
        "updated": True,
        "question_id": str(q.id),
        "template_code": q.template_code,
        "version": q.version,
        "dimension": q.dimension,
        "text": q.text,
        "answer_type": q.answer_type,
        "weight": q.weight,
        "active": bool(q.active),
    }


# -------------------------
# Seed default questions (moved here)
# -------------------------

@router.post("/evaluations/{evaluation_id}/seed/questions")
def seed_questions(
    evaluation_id: str,
    template_code: str = "DEFAULT",
    version: int = 1,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    _ensure_evaluation_exists(db, evaluation_id)

    default_questions: List[Dict[str, Any]] = [
        {"dimension": "Board Composition", "text": "The board has an appropriate mix of skills, experience, and independence to oversee the organisation effectively.", "answer_type": "rating", "weight": 1},
        {"dimension": "Board Composition", "text": "Board succession planning is proactive and aligned to future strategic needs.", "answer_type": "rating", "weight": 1},
        {"dimension": "Risk Oversight", "text": "The board provides effective oversight of key enterprise risks (financial, operational, regulatory).", "answer_type": "rating", "weight": 1},
        {"dimension": "Risk Oversight", "text": "The board actively oversees emerging risks, including technology and cyber risks.", "answer_type": "rating", "weight": 1},
        {"dimension": "Strategy Oversight", "text": "The board provides robust challenge and guidance on strategy, performance, and value creation.", "answer_type": "rating", "weight": 1},
        {"dimension": "Strategy Oversight", "text": "The board monitors execution against strategy using clear KPIs and timely reporting.", "answer_type": "rating", "weight": 1},
        {"dimension": "Board Culture & Dynamics", "text": "Board discussions encourage constructive challenge, candour, and healthy debate.", "answer_type": "rating", "weight": 1},
        {"dimension": "Board Culture & Dynamics", "text": "Board papers and meeting minutes are well-structured and support effective decision-making.", "answer_type": "rating", "weight": 1},
        {"dimension": "Open Comments", "text": "What are the board’s top 3 governance strengths?", "answer_type": "comment", "weight": 1},
        {"dimension": "Open Comments", "text": "What are the board’s top 3 governance weaknesses / improvement areas?", "answer_type": "comment", "weight": 1},
    ]

    existing = db.execute(
        select(Question.text).where(
            Question.template_code == template_code,
            Question.version == version,
        )
    ).scalars().all()

    existing_texts = set((x or "").strip() for x in existing)

    created = 0
    for q in default_questions:
        if q["text"].strip() in existing_texts:
            continue
        db.add(
            Question(
                template_code=template_code,
                version=version,
                dimension=q["dimension"],
                text=q["text"],
                answer_type=q["answer_type"],
                weight=int(q.get("weight", 1)),
                active=1,
            )
        )
        created += 1

    db.commit()

    total = db.execute(
        select(func.count(Question.id)).where(
            Question.template_code == template_code,
            Question.version == version,
        )
    ).scalar_one()

    return {
        "evaluation_id": evaluation_id,
        "template_code": template_code,
        "version": version,
        "created": created,
        "total_questions_now": int(total or 0),
    }


# -------------------------
# AI generate questionnaire drafts (moved here)
# -------------------------

@router.post("/evaluations/{evaluation_id}/questionnaire/generate")
async def questionnaire_generate(
    evaluation_id: str,
    file: Optional[UploadFile] = File(default=None),
    n_questions: Optional[int] = Form(default=None),
    template_code: Optional[str] = Form(default=None),
    version: Optional[int] = Form(default=None),
    allowed_answer_types: Optional[str] = Form(default=None),
    dimension_hints: Optional[str] = Form(default=None),
    body: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db),
):
    _ensure_evaluation_exists(db, evaluation_id)

    # JSON mode
    if file is None:
        if not body:
            raise HTTPException(status_code=400, detail="Provide multipart file or JSON body with source_text.")
        source_text = (body.get("source_text") or "").strip()
        if not source_text:
            raise HTTPException(status_code=400, detail="source_text is required.")
        n = int(body.get("n_questions") or 10)
        tc = str(body.get("template_code") or "DEFAULT").strip()
        ver = int(body.get("version") or 1)
        allowed = body.get("allowed_answer_types") or ["rating", "yesno", "comment"]
        hints = body.get("dimension_hints") or []
    else:
        filename = (file.filename or "").lower()
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        if filename.endswith(".txt") or filename.endswith(".md") or filename.endswith(".markdown"):
            source_text = raw.decode("utf-8", errors="ignore").strip()
        else:
            raise HTTPException(status_code=501, detail="File extraction not implemented yet. Upload .txt for now.")

        n = int(n_questions or 10)
        tc = str(template_code or "DEFAULT").strip()
        ver = int(version or 1)
        allowed = _parse_json_form_field(allowed_answer_types, ["rating", "yesno", "comment"])
        hints = _parse_json_form_field(dimension_hints, [])

    n = max(1, min(int(n), 50))
    source_text = _clean_text_for_prompt(source_text)

    dim_hint_str = ", ".join(hints) if hints else "Board Composition, Risk Oversight, Strategy Oversight, Board Culture & Dynamics, Open Comments"

    prompt = f"""
You are an expert board evaluation consultant.

Generate {n} questionnaire questions from the SOURCE TEXT.
Rules:
- Output MUST be valid JSON only (no markdown, no commentary).
- Output schema:
  {{
    "items": [
      {{
        "dimension": "string",
        "text": "string",
        "answer_type": "rating|yesno|comment",
        "weight": 1,
        "active": true
      }}
    ]
  }}
- Keep questions specific and grounded in the SOURCE TEXT.
- Use these dimension hints where possible: {dim_hint_str}
- Allowed answer types: {allowed}

SOURCE TEXT:
{source_text}
""".strip()

    try:
        items = await _llm_generate_items(prompt)
    except Exception:
        items = _fallback_extract_questions(source_text, n=n, allowed=allowed, hints=hints)

    return {"items": items, "meta": {"evaluation_id": evaluation_id, "template_code": tc, "version": ver}}

