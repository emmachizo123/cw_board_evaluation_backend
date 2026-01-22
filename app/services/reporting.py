"""
app.services.reporting
----------------------
AI report generation pipeline (MVP).

Responsibilities:
1) Collect/assemble analytics (now DB-backed if db is provided; demo fallback otherwise)
2) Build lightweight context (regulator snippets + C&W playbook snippets)
3) Call LLM to generate structured report sections
4) Return report_id + summary_json (API persists to Postgres)

Later upgrades:
- Real regulatory RAG retrieval
- Sector/regulator-specific prompt templates
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage

# NEW: dynamic analytics
from app.services.analytics import build_analytics

try:
    # Optional import to avoid hard dependency if called without DB
    from sqlalchemy.orm import Session
except Exception:  # pragma: no cover
    Session = Any  # type: ignore


def _now_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def llm_sanity_check() -> Dict[str, Any]:
    """
    Quick check: can we call the configured LLM successfully?
    Returns minimal data to confirm call works.
    """
    chosen_model = os.getenv("LLM_MODEL", "gpt-4.1-mini")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required for real LLM calls.")

    llm = init_chat_model(
        chosen_model,
        model_provider="openai",
        api_key=api_key,
        temperature=0,
    )
    resp = llm.invoke([HumanMessage(content="Reply with exactly: pong")])
    return {"ping": (getattr(resp, "content", "") or "").strip()}


def _demo_analytics(evaluation_id: str, include_trends: bool) -> Dict[str, Any]:
    """
    MVP seeded analytics fallback.
    Used only if db is not passed into generate_report_for_evaluation().
    """
    analytics: Dict[str, Any] = {
        "evaluation": {
            "id": evaluation_id,
            "tenant_name": "Demo Client Plc",
            "year": 2025,
            "sector": "insurance",
            "regulators": ["NAICOM", "FRC"],
        },
        "response_stats": {
            "invited": 12,
            "responded": 10,
            "completion_rate": 0.8333,
        },
        "metrics": {
            "overall_score": 78.3,
            "dimensions": [
                {"name": "Board Composition", "score": 82.0},
                {"name": "Risk Oversight", "score": 70.5},
                {"name": "Strategy Oversight", "score": 76.0},
                {"name": "Board Culture & Dynamics", "score": 80.0},
            ],
        },
        "comments": {
            "strengths": [
                "Board meetings are well-structured and adequately documented.",
                "Independent directors demonstrate robust challenge and debate.",
                "Committees provide clear oversight and escalate matters appropriately.",
            ],
            "weaknesses": [
                "Limited board-level oversight of emerging technology and cyber risks.",
                "CEO performance evaluation and succession planning are not fully formalized.",
                "Risk appetite statements are not consistently reviewed and documented annually.",
            ],
        },
    }

    if include_trends:
        analytics["trends"] = {
            "available": True,
            "years": [
                {"year": 2023, "overall_score": 72.0},
                {"year": 2024, "overall_score": 75.0},
                {"year": 2025, "overall_score": 78.3},
            ],
        }
    else:
        analytics["trends"] = {"available": False, "years": []}

    return analytics


def _mvp_context(regulators: List[str], include_compliance: bool) -> Dict[str, Any]:
    """
    MVP context: regulator snippets + C&W playbook snippets.
    Later this becomes a real RAG retrieval over uploaded frameworks.
    """
    regulatory_snippets: List[Dict[str, Any]] = []
    if include_compliance:
        for r in regulators:
            regulatory_snippets.append(
                {
                    "framework_code": r,
                    "reference_code": "N/A",
                    "theme": "General Governance",
                    "text": (
                        f"{r}: Boards should demonstrate evidence-based evaluation, "
                        "documentation, and action plans with clear accountability and timelines."
                    ),
                }
            )

    return {
        "regulatory_snippets": regulatory_snippets,
        "cw_playbook_snippets": [
            {
                "source": "C&W Board Evaluation Playbook",
                "text": (
                    "Action plans should be prioritised into quick wins (0–90 days) and "
                    "structural improvements (3–12 months), with clear owners, timelines, "
                    "and measurable outcomes."
                ),
            },
            {
                "source": "C&W Methodology Notes",
                "text": "Recommendations should map to evaluation dimensions and be phrased as governance actions, not generic advice.",
            },
        ],
    }


def _generate_sections_with_llm(
    analytics: Dict[str, Any],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Call LLM and return structured sections.
    """
    chosen_model = os.getenv("LLM_MODEL", "gpt-4.1-mini")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required for real LLM calls.")

    llm = init_chat_model(
        chosen_model,
        model_provider="openai",
        api_key=api_key,
        temperature=0,
    )

    system = SystemMessage(
        content=(
            "You are an expert corporate governance consultant writing a board evaluation report.\n"
            "Return STRICT JSON only (no markdown) with this schema:\n"
            "{\n"
            '  "executive_summary": {\n'
            '    "overall_message": string,\n'
            '    "key_strengths": [string, ...],\n'
            '    "key_weaknesses": [string, ...],\n'
            '    "outlook": string\n'
            "  },\n"
            '  "recommendations": {\n'
            '    "recommendations": [\n'
            "      {\n"
            '        "theme": string,\n'
            '        "priority": "quick_win"|"near_term"|"structural",\n'
            '        "action": string,\n'
            '        "owner_suggestion": string,\n'
            '        "timeline": string,\n'
            '        "success_metric": string\n'
            "      }\n"
            "    ]\n"
            "  }\n"
            "}\n"
            "Ground every claim in the provided analytics and context."
        )
    )

    human = HumanMessage(
        content=(
            "ANALYTICS:\n"
            f"{analytics}\n\n"
            "CONTEXT:\n"
            f"{context}\n\n"
            "Generate the JSON now."
        )
    )

    resp = llm.invoke([system, human])
    text = (getattr(resp, "content", "") or "").strip()

    import json
    try:
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"LLM returned non-JSON output. First 400 chars: {text[:400]}") from exc


def generate_report_for_evaluation(
    evaluation_id: str,
    user_id: str,
    include_trends: bool = True,
    include_compliance: bool = True,
    return_summary_json: bool = False,
    db: Session | None = None,  # ✅ NEW: optional DB session (won’t break existing callers)
):
    """
    Generate report payload for an evaluation.

    If db is provided, analytics are computed dynamically from DB.
    If db is not provided, analytics fall back to seeded demo analytics (MVP compatibility).

    Returns
    -------
    - report_id (str) if return_summary_json=False
    - (report_id, summary_json) if return_summary_json=True
    """
    report_id = str(uuid4())

    # ✅ Use DB analytics if available, otherwise fallback
    if db is not None:
        analytics = build_analytics(db=db, evaluation_id=evaluation_id, include_trends=include_trends)
    else:
        analytics = _demo_analytics(evaluation_id, include_trends=include_trends)

    regulators = (analytics.get("evaluation", {}) or {}).get("regulators", []) or []
    context = _mvp_context(regulators=regulators, include_compliance=include_compliance)

    sections = _generate_sections_with_llm(analytics=analytics, context=context)

    summary_json: Dict[str, Any] = {
        "analytics": analytics,
        "context": context,
        "sections": sections,
        "meta": {
            "evaluation_id": evaluation_id,
            "report_id": report_id,
            "generated_utc": _now_utc_str(),
            "created_by": user_id,
        },
    }

    if return_summary_json:
        return report_id, summary_json

    return report_id
