"""
app.services.analytics
----------------------
Dynamic analytics computed from stored participants + responses + questions.

This is the foundation for:
- real evidence-based reporting
- audit trails
- trends and benchmarking
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Evaluation, Participant, Question, Response, Report


def build_analytics(db: Session, evaluation_id: str, include_trends: bool = True) -> Dict[str, Any]:
    """
    Build analytics for an evaluation based on DB responses.

    Returns a dict shaped similarly to what the report generator already expects.
    """
    evaluation = db.get(Evaluation, evaluation_id)
    if not evaluation:
        raise ValueError(f"Evaluation not found: {evaluation_id}")

    # ---- response stats ----
    invited = db.execute(
        select(Participant).where(Participant.evaluation_id == evaluation_id)
    ).scalars().all()
    invited_count = len(invited)
    responded_count = sum(1 for p in invited if (p.responded_at is not None or p.status == "responded"))
    completion_rate = (responded_count / invited_count) if invited_count else 0.0

    # ---- compute dimension scores ----
    # We compute weighted average score per dimension using rating questions only.
    rows = db.execute(
        select(Question.dimension, Question.weight, Response.score)
        .join(Response, Response.question_id == Question.id)
        .where(Response.evaluation_id == evaluation_id)
        .where(Response.score.is_not(None))
    ).all()

    # dimension -> (weighted_sum, weight_total, count)
    agg: Dict[str, Dict[str, float]] = {}
    for dim, weight, score in rows:
        w = float(weight or 1)
        s = float(score or 0)
        if dim not in agg:
            agg[dim] = {"wsum": 0.0, "wtotal": 0.0, "count": 0.0}
        agg[dim]["wsum"] += s * w
        agg[dim]["wtotal"] += w
        agg[dim]["count"] += 1.0

    # Convert to a 0–100 score scale for reporting (assuming rating 1–5)
    # If you change the rating scale later, update this normalization.
    dimensions: List[Dict[str, Any]] = []
    for dim, a in agg.items():
        avg_1_to_5 = (a["wsum"] / a["wtotal"]) if a["wtotal"] else 0.0
        score_0_to_100 = round((avg_1_to_5 / 5.0) * 100.0, 1)
        dimensions.append({"name": dim, "score": score_0_to_100})

    # overall is the weighted average across all dimensions (by responses)
    if rows:
        overall_avg_1_to_5 = sum((float(score) * float(weight or 1)) for _, weight, score in rows) / sum(
            float(weight or 1) for _, weight, _ in rows
        )
        overall_score = round((overall_avg_1_to_5 / 5.0) * 100.0, 1)
    else:
        overall_score = 0.0

    # ---- strengths / weaknesses (simple heuristic) ----
    dims_sorted = sorted(dimensions, key=lambda d: d["score"], reverse=True)
    strengths = []
    weaknesses = []

    # Take top 2 and bottom 2 dimension narratives (MVP)
    for d in dims_sorted[:2]:
        strengths.append(f"Strong performance in {d['name']} (score {d['score']}).")
    for d in sorted(dimensions, key=lambda d: d["score"])[:2]:
        weaknesses.append(f"Improvement needed in {d['name']} (score {d['score']}).")

    # ---- trends (MVP pragmatic approach) ----
    trends = {"available": False, "years": []}
    if include_trends:
        trends = _build_trends_from_reports(db, evaluation)

    return {
        "evaluation": {
            "id": evaluation.id,
            "tenant_name": evaluation.tenant_name,
            "year": evaluation.year,
            "sector": evaluation.sector,
            "regulators": (evaluation.regulators or {}).get("items", []),
        },
        "response_stats": {
            "invited": invited_count,
            "responded": responded_count,
            "completion_rate": round(completion_rate, 4),
        },
        "metrics": {
            "overall_score": overall_score,
            "dimensions": dims_sorted,
        },
        "trends": trends,
        "comments": {
            "strengths": strengths,
            "weaknesses": weaknesses,
        },
    }


def _build_trends_from_reports(db: Session, evaluation: Evaluation) -> Dict[str, Any]:
    """
    MVP trends: use past reports for the same tenant_name across years.

    Why: trends require historical scoring; reports already store computed analytics.
    Later: compute trends directly from responses once historical responses exist.
    """
    # find all evaluations for same tenant
    evals = db.execute(
        select(Evaluation)
        .where(Evaluation.tenant_name == evaluation.tenant_name)
        .order_by(Evaluation.year.asc())
    ).scalars().all()

    year_scores: List[Dict[str, Any]] = []
    for ev in evals:
        # fetch latest report for that evaluation
        rep = db.execute(
            select(Report)
            .where(Report.evaluation_id == ev.id)
            .order_by(desc(Report.created_at))
            .limit(1)
        ).scalars().first()

        if not rep:
            continue

        try:
            score = (
                (rep.summary_json or {})
                .get("analytics", {})
                .get("metrics", {})
                .get("overall_score", None)
            )
        except Exception:
            score = None

        if score is None:
            continue

        year_scores.append({"year": ev.year, "overall_score": score})

    return {
        "available": len(year_scores) >= 2,
        "years": year_scores,
    }
