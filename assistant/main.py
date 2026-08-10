"""FastAPI app: REST API consumed by the web console."""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, executor, runner, scheduler
from .config import settings
from .db import SessionLocal, get_session, init_db
from .models import Action, ActionPlan, Run, Ticket
from .paths import VALID_ACTIONS, get_path, load_paths
from .schemas import (ConfigIn, EditPlanIn, LoginIn, ManualRunIn, PathIn)

log = logging.getLogger("assistant")

WEB_DIR = Path(settings.settings_path).resolve().parent.parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start_scheduler()
    log.info("assistant up (mock=%s)", settings.mock)
    yield
    scheduler.stop_scheduler()


app = FastAPI(title="AI Assistant", lifespan=lifespan)


# ---------- auth ----------

@app.post("/api/login", include_in_schema=False)
def login(payload: LoginIn):
    if not auth.verify_password(payload.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = auth.create_session()
    response = JSONResponse({"ok": True, "csrf": token})
    auth.set_session_cookie(response, token)
    return response


@app.post("/api/logout")
def logout(request: Request):
    response = JSONResponse({"ok": True})
    auth.destroy_session(request, response)
    return response


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True, "mock": settings.mock,
            "warnings": settings.token_hint()}


@app.get("/api/session", include_in_schema=False)
def session_state(request: Request):
    return {"authed": auth.user_authed(request)}


class _Mutation:
    async def __call__(self, request: Request):
        auth.require_user(request)
        auth.csrf_guard(request)


_require_mutation = Depends(_Mutation())


def _require_user(request: Request):
    auth.require_user(request)


# ---------- runs ----------

@app.post("/api/runs", dependencies=[_require_mutation])
def manual_run(payload: ManualRunIn):
    jql = (payload.jql or "").strip() or None
    run_id = runner.create_run(trigger="manual", jql_label="override" if jql else "config")
    threading.Thread(target=runner.process_run, args=(run_id, jql), daemon=True).start()
    return {"run_id": run_id, "status": "queued"}


@app.get("/api/runs")
def list_runs(request: Request, db: Session = Depends(get_session)):
    _require_user(request)
    runs = db.execute(select(Run).order_by(Run.id.desc()).limit(50)).scalars().all()
    return [{
        "id": r.id, "trigger": r.trigger, "status": r.status, "jql_label": r.jql_label,
        "error": r.error,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "ticket_count": len(r.tickets),
        "pending_plans": sum(1 for t in r.tickets for p in t.plans if p.review_status == "pending"),
    } for r in runs]


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int, request: Request, db: Session = Depends(get_session)):
    _require_user(request)
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return {
        "id": run.id, "trigger": run.trigger, "status": run.status, "jql_label": run.jql_label,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "tickets": [t.to_dict(include="full") for t in run.tickets],
    }


@app.get("/api/runs/{run_id}/tickets")
def run_tickets(run_id: int, request: Request, db: Session = Depends(get_session)):
    _require_user(request)
    tickets = db.execute(select(Ticket).where(Ticket.run_id == run_id)).scalars().all()
    return [t.to_dict(include="plan") for t in tickets]


# ---------- approvals ----------

@app.get("/api/approvals")
def list_approvals(request: Request, db: Session = Depends(get_session)):
    _require_user(request)
    plans = db.execute(
        select(ActionPlan).where(ActionPlan.review_status == "pending")
        .order_by(ActionPlan.id.desc())
    ).scalars().all()
    out = []
    for p in plans:
        t = p.ticket
        out.append({
            "plan": p.to_dict(),
            "ticket": {
                "id": t.id, "key": t.key, "summary": t.summary, "status_name": t.status_name,
                "triage_reason": t.triage_reason, "triage_confidence": t.triage_confidence,
                "need_my_input": t.need_my_input, "links": [l.to_dict() for l in t.links],
            },
            "run_id": p.run_id,
        })
    return out


@app.get("/api/approvals/{plan_id}/diff")
def plan_diff(plan_id: int, request: Request, db: Session = Depends(get_session)):
    _require_user(request)
    plan = db.get(ActionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return {"plan_id": plan.id,
            "files": [{"seq": a.seq, "kind": a.kind, "preview": a.preview} for a in plan.actions]}


@app.get("/api/approvals/{plan_id}")
def get_plan(plan_id: int, request: Request, db: Session = Depends(get_session)):
    _require_user(request)
    plan = db.get(ActionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    return {"plan": plan.to_dict()}


@app.post("/api/approvals/{plan_id}/approve", dependencies=[_require_mutation])
def approve(plan_id: int, db: Session = Depends(get_session)):
    plan = db.get(ActionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    if plan.review_status not in ("pending",):
        raise HTTPException(409, f"Plan already {plan.review_status}")
    plan.review_status = "approved"
    plan.approved_at = datetime.now(timezone.utc)
    db.commit()
    threading.Thread(target=_execute_approved, args=(plan_id,), daemon=True).start()
    return {"ok": True, "plan_id": plan_id}


@app.post("/api/approvals/{plan_id}/reject", dependencies=[_require_mutation])
def reject(plan_id: int, db: Session = Depends(get_session)):
    plan = db.get(ActionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    if plan.review_status != "pending":
        raise HTTPException(409, f"Plan already {plan.review_status}")
    plan.review_status = "rejected"
    db.commit()
    return {"ok": True, "plan_id": plan_id}


@app.put("/api/approvals/{plan_id}", dependencies=[_require_mutation])
def edit_plan(plan_id: int, payload: EditPlanIn, db: Session = Depends(get_session)):
    plan = db.get(ActionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    if plan.review_status != "pending":
        raise HTTPException(409, f"Plan already {plan.review_status}")

    paths = load_paths(settings.paths_dir)
    allowed = get_path(paths, plan.path_id)
    allowed_set = set(allowed.allowed_actions if allowed else {"comment"})

    if payload.summary is not None:
        plan.summary = payload.summary
    if payload.narrative is not None:
        plan.narrative = payload.narrative

    if payload.actions is not None:
        if not payload.actions:
            raise HTTPException(422, "Plan must keep at least one action")
        for a in payload.actions:
            if a.kind not in allowed_set:
                raise HTTPException(422, f"action '{a.kind}' not allowed by path '{plan.path_id}'")
        plan.actions.clear()
        for i, a in enumerate(payload.actions):
            plan.actions.append(Action(seq=i, kind=a.kind, params=a.params,
                                       preview=executor.preview_action(a.kind, a.params),
                                       enabled=a.enabled))
    db.commit()
    return {"ok": True, "plan": plan.to_dict()}


def _execute_approved(plan_id: int) -> None:
    db = SessionLocal()
    try:
        from .executor import ExecContext, execute_plan
        from .integrations import build_github, build_jira
        from .paths import get_path as gp, load_paths as lp
        plan = db.get(ActionPlan, plan_id)
        if not plan:
            return
        jira = build_jira(settings)
        github = build_github(settings)
        paths = lp(settings.paths_dir)
        path = gp(paths, plan.path_id)
        allowed = set(path.allowed_actions if path else {"comment"})
        ctx = ExecContext(jira=jira, github=github, settings=settings,
                          workspace=settings.workspace)
        status, results = execute_plan(ctx, plan, allowed)
        plan.review_status = status if status == "executed" else "failed"
        plan.error = "; ".join(r for r in results if r.startswith("FAIL"))
        plan.executed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as e:  # noqa: BLE001
        try:
            plan = db.get(ActionPlan, plan_id)
            if plan:
                plan.review_status = "failed"
                plan.error = str(e)
                plan.executed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


# ---------- config ----------

def _config_dict() -> dict:
    return {
        "jql_queries": settings.jql_queries,
        "schedule": {"enabled": settings.schedule_enabled, "hour": settings.schedule_hour,
                     "minute": settings.schedule_minute},
        "jira": {"base_url": settings.jira_base_url, "email": settings.jira_email,
                 "account_id": settings.jira_account_id, "devinfo_field": settings.jira_devinfo_field},
        "github": {"repo": settings.github_repo, "repos": settings.github_repos,
                   "project_repo_map": settings.github_project_map, "host": settings.github_host,
                   "ssh_url": settings.github_ssh_url, "token_set": bool(settings.github_token)},
        "models": {"triage": settings.model_triage, "action": settings.model_action},
        "run": {"max_tickets_per_run": settings.max_tickets_per_run},
        "mock": settings.mock,
    }


@app.get("/api/config")
def get_config(request: Request):
    _require_user(request)
    return _config_dict()


@app.put("/api/config", dependencies=[_require_mutation])
def put_config(payload: ConfigIn):
    settings.jql_queries = payload.jql_queries
    sch = payload.schedule
    settings.schedule_enabled = bool(sch.get("enabled", settings.schedule_enabled))
    settings.schedule_hour = int(sch.get("hour", settings.schedule_hour))
    settings.schedule_minute = int(sch.get("minute", settings.schedule_minute))
    settings.jira_base_url = payload.jira.get("base_url", settings.jira_base_url).rstrip("/")
    settings.jira_email = payload.jira.get("email", settings.jira_email)
    settings.jira_account_id = payload.jira.get("account_id", settings.jira_account_id)
    settings.jira_devinfo_field = payload.jira.get("devinfo_field", settings.jira_devinfo_field)
    settings.github_repo = payload.github.get("repo", settings.github_repo)
    repos = payload.github.get("repos", settings.github_repos)
    settings.github_repos = [r.strip() for r in repos if str(r).strip()] if repos else []
    if not settings.github_repos and settings.github_repo:
        settings.github_repos = [settings.github_repo]
    pj = payload.github.get("project_repo_map", settings.github_project_map)
    settings.github_project_map = {str(k): str(v) for k, v in (pj or {}).items() if str(k) and str(v)}
    settings.github_host = payload.github.get("host", settings.github_host)
    settings.github_ssh_url = payload.github.get("ssh_url", settings.github_ssh_url)
    settings.model_triage = payload.models.get("triage", settings.model_triage)
    settings.model_action = payload.models.get("action", settings.model_action)
    settings.max_tickets_per_run = int(payload.run.get("max_tickets_per_run", settings.max_tickets_per_run))

    settings.settings_path.write_text(json.dumps({
        "jql_queries": settings.jql_queries,
        "schedule": {"enabled": settings.schedule_enabled, "hour": settings.schedule_hour,
                     "minute": settings.schedule_minute},
        "jira": {"base_url": settings.jira_base_url, "email": settings.jira_email,
                 "account_id": settings.jira_account_id, "devinfo_field": settings.jira_devinfo_field},
        "github": {"repo": settings.github_repo, "repos": settings.github_repos,
                   "project_repo_map": settings.github_project_map, "host": settings.github_host,
                   "ssh_url": settings.github_ssh_url},
        "models": {"triage": settings.model_triage, "action": settings.model_action},
        "run": {"max_tickets_per_run": settings.max_tickets_per_run},
    }, indent=2))
    scheduler.reschedule()
    return _config_dict()


@app.get("/api/repo-map")
def get_repo_map(request: Request):
    _require_user(request)
    return settings.repo_map


@app.put("/api/repo-map", dependencies=[_require_mutation])
def put_repo_map(payload: dict):
    from .config import REPO_MAP_PATH

    clean = {str(k): str(v) for k, v in (payload or {}).items() if str(k) and str(v)}
    settings.repo_map = clean
    REPO_MAP_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    return settings.repo_map


# ---------- paths ----------

@app.get("/api/paths")
def list_paths(request: Request):
    _require_user(request)
    return [p.to_dict() for p in load_paths(settings.paths_dir)]


@app.put("/api/paths/{path_id}", dependencies=[_require_mutation])
def put_path(path_id: str, payload: PathIn):
    folder = settings.paths_dir / path_id
    if not folder.exists() or not (folder / "schema.json").exists():
        raise HTTPException(404, "Path not found")
    schema = {
        "name": payload.name or path_id, "enabled": payload.enabled,
        "allowed_actions": [a for a in payload.allowed_actions if a in VALID_ACTIONS],
        "required_backend": payload.required_backend, "work": payload.work,
        "approval": payload.approval, "default_actions": payload.default_actions,
    }
    (folder / "schema.json").write_text(json.dumps(schema, indent=2))
    if payload.instruct:
        (folder / "instruct.md").write_text(payload.instruct)
    return list_paths_fn()


@app.post("/api/paths", dependencies=[_require_mutation])
def create_path(payload: PathIn):
    path_id = payload.id or ""
    import re
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", path_id):
        raise HTTPException(422, "path id must be kebab-case (a-z0-9)")
    folder = settings.paths_dir / path_id
    if folder.exists():
        raise HTTPException(409, "Path already exists")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "instruct.md").write_text(payload.instruct or _TEMPLATE_INSTRUCT)
    schema = {
        "name": payload.name or path_id, "enabled": payload.enabled,
        "allowed_actions": [a for a in payload.allowed_actions if a in VALID_ACTIONS],
        "required_backend": payload.required_backend, "work": payload.work,
        "approval": payload.approval, "default_actions": payload.default_actions,
    }
    (folder / "schema.json").write_text(json.dumps(schema, indent=2))
    return list_paths_fn()


def list_paths_fn():
    return [p.to_dict() for p in load_paths(settings.paths_dir)]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    f = WEB_DIR / "index.html"
    if not f.exists():
        raise HTTPException(404, "web/static/index.html not built yet")
    return f.read_text()


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


_TEMPLATE_INSTRUCT = """# {Path name}

## Purpose
When this path fits, in ~50 words.

## Triage criteria (bullet list)
- Strong signal: ...
- Weak signal: ...
- Anti-signal / when not to choose this path: ...

## Action guidance
How to approach the work once routed here.

## Output requirements
Any format notes, quality bar.
"""