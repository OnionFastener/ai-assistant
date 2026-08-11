"""Pydantic schemas: API DTOs + AI output contracts."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---- AI output contracts (strictly validated in triage/action layers) ----

class CandidatePath(BaseModel):
    path_id: str
    score: float = Field(ge=0, le=1)

class TriageResult(BaseModel):
    path_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    need_my_input: bool = False
    candidate_paths: list[CandidatePath] = []

class ActionProposal(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    preview: str = ""
    enabled: bool = True

class ActionPlanInput(BaseModel):
    summary: str = ""
    narrative: str = ""
    actions: list[ActionProposal] = Field(max_length=20)


# ---- API DTOs ----

class LoginIn(BaseModel):
    password: str

class ManualRunIn(BaseModel):
    jql: str | None = None

class EditPlanIn(BaseModel):
    summary: str | None = None
    narrative: str | None = None
    actions: list[ActionProposal] | None = None

class ConfigIn(BaseModel):
    jql_queries: list[dict] = Field(default_factory=list)
    schedule: dict = Field(default_factory=dict)
    jira: dict = Field(default_factory=dict)
    github: dict = Field(default_factory=dict)
    models: dict = Field(default_factory=dict)
    run: dict = Field(default_factory=dict)

class PathIn(BaseModel):
    id: str | None = None
    name: str | None = None
    enabled: bool = True
    allowed_actions: list[str] = Field(default_factory=list)
    required_backend: str | None = None
    work: dict = Field(default_factory=dict)
    approval: dict = Field(default_factory=dict)
    default_actions: list[dict] = Field(default_factory=list)
    instruct: str | None = None
    behavior: str | None = None

class ApproveResult(BaseModel):
    ok: bool
    plan_id: int
    message: str = ""
    action_results: list[dict] = Field(default_factory=list)