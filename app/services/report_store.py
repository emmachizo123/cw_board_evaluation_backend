"""
app.services.report_store
-------------------------
Postgres-backed persistence helpers for Evaluations and Reports (MVP).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.models import Evaluation, Report

from uuid import uuid4

from app.db.models import Report



def ensure_demo_evaluation_exists(db: Session, evaluation_id: str) -> Evaluation:
    """MVP: seed an evaluation row so report FK is valid."""
    ev = db.get(Evaluation, evaluation_id)
    if ev:
        return ev

    ev = Evaluation(
        id=evaluation_id,
        tenant_name="Demo Client Plc",
        sector="insurance",
        year=2025,
        regulators={"items": ["NAICOM", "FRC"]},
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def create_report(
    db: Session,
    evaluation_id: str,
    created_by: str,
    status: str,
    summary_json: dict,
) -> Report:
    report = Report(
        id=str(uuid4()),  # <-- string UUID, stored in varchar
        evaluation_id=evaluation_id,
        created_by=created_by,
        status=status,
        summary_json=summary_json,
    )
    db.add(report)
    db.commit()
    db.refresh(report)  # now refresh will query WHERE id = '...' (no ::UUID cast)
    return report


def get_report_by_id(db: Session, report_id: str) -> Optional[Report]:
    return db.get(Report, report_id)


def get_latest_report_for_evaluation(db: Session, evaluation_id: str) -> Optional[Report]:
    stmt = (
        select(Report)
        .where(Report.evaluation_id == evaluation_id)
        .order_by(desc(Report.created_at))
        .limit(1)
    )
    return db.execute(stmt).scalars().first()
