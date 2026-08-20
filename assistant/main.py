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

from . import auth, executor, runner, scheduler, opencode_runner
from .config import settings
from .db import SessionLocal, get_session, init_db
from .integrations import gitutil
from .models import Action, ActionPlan, PatchWorker, Run, RunLog, Ticket
from .paths import VALID_ACTIONS, get_path, load_paths
from .schemas import (ConfigIn, EditPlanIn, LoginIn, ManualRunIn, PathIn)
from .triage_config import load_triage_config, save_triage_config
from .action_config import load_action_config, save_action_config

log = logging.getLogger("assistant")

WEB_DIR = Path(settings.settings_path).resolve().parent.parent / "web" / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.mock and settings.admin_password == "change-me":
        raise RuntimeError("Set ASST_ADMIN_PASSWORD to a non-default value before starting live mode")
    init_db()
    scheduler.start_scheduler()
    log.info("assistant up (mock=%s)", settings.mock)
    yield
    scheduler.stop_scheduler()


app = FastAPI(title="AI Assistant", lifespan=lifespan)


# ---------- auth ----------

@app.post("/api/login", include_in_schema=False)
def login(payload: LoginIn, request: Request):
    client = request.client.host if request.client else "unknown"
    if not auth.login_allowed(client):
        raise HTTPException(status_code=429, detail="Too many login attempts; try again later")
    if not auth.verify_password(payload.password):
        auth.record_login_failure(client)
        raise HTTPException(status_code=401, detail="Wrong password")
    auth.clear_login_failures(client)
    token = auth.create_session()
    response = JSONResponse({"ok": True, "csrf": auth.csrf_token_from_session(token)})
    auth.set_session_cookie(response, token)
    return response


@app.post("/api/logout")
def logout(request: Request):
    auth.require_user(request)
    auth.csrf_guard(request)
    response = JSONResponse({"ok": True})
    auth.destroy_session(request, response)
    return response


@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True, "mock": settings.mock,
            "warnings": settings.token_hint()}


@app.get("/api/session", include_in_schema=False)
def session_state(request: Request):
    if not auth.user_authed(request):
        return {"authed": False}
    return {"authed": True, "csrf": auth.csrf_token(request)}


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
    sources = set(payload.sources) if payload.sources is not None else None
    threading.Thread(target=runner.process_run, args=(run_id, jql, sources), daemon=True).start()
    return {"run_id": run_id, "status": "queued"}


@app.post("/api/runs/{run_id}/stop", dependencies=[_require_mutation])
def stop_run(run_id: int, db: Session = Depends(get_session)):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("queued", "fetching", "triaging", "stopping"):
        raise HTTPException(409, f"Run cannot be stopped from status '{run.status}'")
    run.status = "stopped"
    db.commit()
    return {"run_id": run.id, "status": run.status,
            "pending_plans": sum(1 for ticket in run.tickets for plan in ticket.plans if plan.review_status == "pending")}


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
        "logs": [{"ts": entry.ts.isoformat() if entry.ts else None, "level": entry.level,
                  "ticket_key": entry.ticket_key, "message": entry.message}
                 for entry in db.execute(select(RunLog).where(RunLog.run_id == run_id).order_by(RunLog.id)).scalars()],
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
        select(ActionPlan).where(ActionPlan.review_status.in_(("pending", "preparing")))
        .order_by(ActionPlan.id.desc())
    ).scalars().all()
    paths = {path.id: path for path in load_paths(settings.paths_dir)}
    out = []
    for p in plans:
        t = p.ticket
        plan_data = p.to_dict()
        path = paths.get(p.path_id)
        plan_data["is_code_proposal"] = bool(path and path.required_backend == "github" and not any(a.kind in ("push_branch", "create_pr") for a in p.actions))
        plan_data["patch_preparation_failed"] = bool(plan_data["is_code_proposal"] and p.error.startswith("Patch preparation"))
        plan_data["scope_review_required"] = any(a.params.get("scope_review_required") for a in p.actions)
        out.append({
            "plan": plan_data,
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
    path = get_path(load_paths(settings.paths_dir), plan.path_id)
    if path and path.required_backend == "github" and not any(a.kind in ("push_branch", "create_pr") for a in plan.actions):
        raise HTTPException(409, "Prepare a patch before approving this code proposal")
    plan.review_status = "approved"
    plan.approved_at = datetime.now(timezone.utc)
    db.commit()
    threading.Thread(target=_execute_approved, args=(plan_id,), daemon=True).start()
    return {"ok": True, "plan_id": plan_id}


@app.post("/api/approvals/{plan_id}/prepare-patch", dependencies=[_require_mutation])
def start_patch_preparation(plan_id: int, db: Session = Depends(get_session)):
    plan = db.get(ActionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found")
    path = get_path(load_paths(settings.paths_dir), plan.path_id)
    if not path or path.required_backend != "github":
        raise HTTPException(409, "This plan does not support patch preparation")
    if plan.review_status != "pending":
        raise HTTPException(409, f"Plan already {plan.review_status}")
    if any(a.kind in ("push_branch", "create_pr") for a in plan.actions):
        raise HTTPException(409, "Patch is already ready for review")
    if any(a.params.get("scope_review_required") for a in plan.actions):
        raise HTTPException(409, "Confirm the feature scope before preparing a patch")
    worker = db.scalar(select(PatchWorker).where(PatchWorker.ticket_key == plan.ticket.key))
    if worker and opencode_runner.process_running(worker.pid, worker.process_start):
        raise HTTPException(409, "A patch is already being prepared for this ticket")
    if worker:
        db.delete(worker)
    active_plan = db.scalar(
        select(ActionPlan).join(Ticket, ActionPlan.ticket_id == Ticket.id).where(
            Ticket.key == plan.ticket.key, ActionPlan.review_status == "preparing", ActionPlan.id != plan.id
        )
    )
    if active_plan:
        raise HTTPException(409, "A patch is already being prepared for this ticket")
    plan.review_status = "preparing"
    plan.error = "Preparing a reviewable patch…"
    db.add(PatchWorker(ticket_key=plan.ticket.key, plan_id=plan.id,
                       cwd=str(settings.workspace / f"run-{plan.run_id}" / plan.ticket.key / "repo")))
    db.commit()
    threading.Thread(target=runner.prepare_patch, args=(plan_id,), daemon=True).start()
    return {"ok": True, "plan_id": plan_id, "status": "preparing"}


@app.post("/api/approvals/{plan_id}/confirm-scope", dependencies=[_require_mutation])
def confirm_scope(plan_id: int, db: Session = Depends(get_session)):
    plan = db.get(ActionPlan, plan_id)
    if not plan or plan.review_status != "pending":
        raise HTTPException(409, "Scope review is not available")
    marked = [a for a in plan.actions if a.params.get("scope_review_required")]
    if not marked:
        raise HTTPException(409, "This proposal does not need scope confirmation")
    for action in marked:
        action.params = {k: v for k, v in action.params.items() if k != "scope_review_required"}
        action.preview = "Scope confirmed — patch preparation is available."
    db.commit()
    return {"ok": True, "plan_id": plan_id}


@app.post("/api/approvals/{plan_id}/cancel-patch", dependencies=[_require_mutation])
def cancel_patch_preparation(plan_id: int, db: Session = Depends(get_session)):
    plan = db.get(ActionPlan, plan_id)
    if not plan or plan.review_status != "preparing":
        raise HTTPException(409, "Patch preparation is not active")
    cwd = settings.workspace / f"run-{plan.run_id}" / plan.ticket.key / "repo"
    worker = db.scalar(select(PatchWorker).where(PatchWorker.plan_id == plan.id))
    cancelled = opencode_runner.cancel_agent(cwd)
    if worker:
        worker.cancel_requested = True
        cancelled = opencode_runner.terminate_process(worker.pid, worker.process_start) or cancelled
    plan.review_status = "pending"
    plan.error = "Patch preparation cancellation requested." if cancelled else "Patch preparation was no longer running."
    db.commit()
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
        for a in payload.actions:
            if a.kind == "push_branch":
                patch = str(a.params.get("patch", ""))
                patch_sha = str(a.params.get("patch_sha", ""))
                if not patch or not patch_sha or gitutil.patch_sha(patch) != patch_sha:
                    raise HTTPException(422, "push_branch patch hash does not match its captured patch")
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
        plan.error = "; ".join(r for r in results if r.startswith(("FAIL", "SKIP")))
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
        "sources": {"jira": {"enabled": settings.scan_jira}, "github_issues": {"enabled": settings.scan_github_issues, "repos": settings.github_issue_repos}},
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
    sources = payload.sources or {}
    settings.scan_jira = bool((sources.get("jira", {}) or {}).get("enabled", settings.scan_jira))
    github_issues = sources.get("github_issues", {}) or {}
    settings.scan_github_issues = bool(github_issues.get("enabled", settings.scan_github_issues))
    repos_for_issues = github_issues.get("repos", settings.github_issue_repos)
    settings.github_issue_repos = (["demo/mock-repo"] if settings.mock else [str(repo).strip() for repo in repos_for_issues if str(repo).strip()])
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
        "sources": {"jira": {"enabled": settings.scan_jira}, "github_issues": {"enabled": settings.scan_github_issues, "repos": settings.github_issue_repos}},
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
    if payload.behavior is not None:
        bf = folder / "behavior.md"
        if payload.behavior.strip():
            bf.write_text(payload.behavior)
        elif bf.exists():
            bf.unlink()
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


# ---------- triage config ----------

@app.get("/api/triage-config")
def get_triage_config(request: Request):
    _require_user(request)
    return load_triage_config().to_dict()


@app.put("/api/triage-config", dependencies=[_require_mutation])
def put_triage_config(payload: dict):
    fields = [str(f).strip() for f in (payload.get("context_fields") or []) if str(f).strip()]
    instruct = str(payload.get("instruct") or "").strip()
    cfg = load_triage_config()
    if fields:
        cfg.context_fields = fields
    classify = payload.get("classify")
    if isinstance(classify, dict) and classify:
        merged = {**cfg.classify, **{str(k): v for k, v in classify.items()}}
        if isinstance(merged.get("type_boost"), dict):
            merged["type_boost"] = {**(cfg.classify.get("type_boost") or {}),
                                    **merged["type_boost"]}
        cfg.classify = merged
    cfg.instruct = instruct
    save_triage_config(cfg)
    return cfg.to_dict()


@app.get("/api/action-config")
def get_action_config(request: Request):
    _require_user(request)
    return load_action_config().to_dict()


@app.put("/api/action-config", dependencies=[_require_mutation])
def put_action_config(payload: dict):
    cfg = load_action_config()
    cfg.instruct = str(payload.get("instruct") or "").strip()
    save_action_config(cfg)
    return cfg.to_dict()


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
