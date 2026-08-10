"""ORM models."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual|scheduled
    status: Mapped[str] = mapped_column(String(20), default="queued")
    jql_label: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="run")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    key: Mapped[str] = mapped_column(String(60), index=True)
    project: Mapped[str] = mapped_column(String(60), default="")
    repo: Mapped[str] = mapped_column(String(200), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    issue_type: Mapped[str] = mapped_column(String(60), default="")
    status_name: Mapped[str] = mapped_column(String(60), default="")
    stage: Mapped[str] = mapped_column(String(30), default="incoming")
    triage_path_id: Mapped[str] = mapped_column(String(60), default="")
    triage_reason: Mapped[str] = mapped_column(Text, default="")
    triage_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    need_my_input: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[Run] = relationship(back_populates="tickets")
    links: Mapped[list["Link"]] = relationship(back_populates="ticket")
    plans: Mapped[list["ActionPlan"]] = relationship(back_populates="ticket")

    def to_dict(self, include: str = "basic") -> dict:
        d = {
            "id": self.id,
            "key": self.key,
            "project": self.project,
            "summary": self.summary,
            "description": self.description,
            "issue_type": self.issue_type,
            "status_name": self.status_name,
            "stage": self.stage,
            "triage_path_id": self.triage_path_id,
            "triage_reason": self.triage_reason,
            "triage_confidence": round(self.triage_confidence, 2),
            "need_my_input": self.need_my_input,
            "error": self.error,
            "run_id": self.run_id,
        }
        if include in ("triage", "full", "plan"):
            d["repo"] = self.repo
        if include in ("full", "plan"):
            d["links"] = [l.to_dict() for l in self.links]
            d["plans"] = [p.to_dict() for p in self.plans]
        return d


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    kind: Mapped[str] = mapped_column(String(20))         # commit|pr|devinfo
    source: Mapped[str] = mapped_column(String(20))       # GitHub|Jira-dev
    url: Mapped[str] = mapped_column(Text, default="")
    repo: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(Text, default="")
    sha: Mapped[str] = mapped_column(String(60), default="")
    pr_state: Mapped[str] = mapped_column(String(20), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    ticket: Mapped[Ticket] = relationship(back_populates="links")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "url": self.url,
            "repo": self.repo,
            "title": self.title,
            "sha": self.sha,
            "pr_state": self.pr_state,
            "meta": self.meta,
        }


class ActionPlan(Base):
    __tablename__ = "action_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    path_id: Mapped[str] = mapped_column(String(60), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    narrative: Mapped[str] = mapped_column(Text, default="")
    review_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|executed|failed|superseded
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    ticket: Mapped[Ticket] = relationship(back_populates="plans")
    actions: Mapped[list["Action"]] = relationship(
        back_populates="plan", order_by="Action.seq", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "run_id": self.run_id,
            "path_id": self.path_id,
            "summary": self.summary,
            "narrative": self.narrative,
            "review_status": self.review_status,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
            "actions": [a.to_dict() for a in self.actions],
        }


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("action_plans.id"))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(40))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    preview: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exec_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|ok|failed|skipped
    exec_result: Mapped[str] = mapped_column(Text, default="")

    plan: Mapped[ActionPlan] = relationship(back_populates="actions")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seq": self.seq,
            "kind": self.kind,
            "params": self.params,
            "preview": self.preview,
            "enabled": self.enabled,
            "exec_status": self.exec_status,
            "exec_result": self.exec_result,
        }


class RunLog(Base):
    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    ticket_key: Mapped[str] = mapped_column(String(60), default="")
    level: Mapped[str] = mapped_column(String(10), default="info")
    ts: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    message: Mapped[str] = mapped_column(Text, default="")