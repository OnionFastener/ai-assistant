"""Pipeline orchestrator: schedule(/manual) trigger → fetch → context → triage → plan."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import executor
from .config import settings
from .db import SessionLocal
from .integrations import build_jira, configured_repos, search_context
from .models import Action, ActionPlan, Link, Run, RunLog, Ticket
from .paths import get_path, load_paths
from .triage import build_ticket_context, run_triage

log = logging.getLogger("assistant.runner")


def create_run(trigger: str = "manual", jql_label: str = "") -> int:
    db = SessionLocal()
    try:
        run = Run(trigger=trigger, status="queued", jql_label=jql_label)
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _log(db, run_id: int, message: str, level: str = "info", key: str = "") -> None:
    db.add(RunLog(run_id=run_id, level=level, message=message, ticket_key=key))
    db.commit()
    log.log({"debug": 10, "info": 20, "warn": 30, "error": 40}.get(level, 20), "%s %s", run_id, message)


def process_run(run_id: int, jql_override: str | None = None) -> None:
    """Run the pipeline in a background thread. Never raises."""
    db = SessionLocal()
    try:
        _process(db, run_id, jql_override)
    except Exception as e:  # noqa: BLE001
        try:
            run = db.get(Run, run_id)
            if run and run.status in ("queued", "fetching"):
                run.status = "failed"
                run.error = str(e)
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
            _log(db, run_id, f"run crashed: {e}", level="error")
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def _process(db, run_id: int, jql_override: str | None) -> None:
    run = db.get(Run, run_id)
    paths = load_paths(settings.paths_dir)
    jira = build_jira(settings)

    import shutil
    shutil.rmtree(settings.workspace / f"run-{run_id}", ignore_errors=True)
    run.status = "fetching"
    db.commit()
    _log(db, run_id, "started")

    queries = ([{"name": "override", "jql": jql_override}] if jql_override
               else settings.jql_queries or [])
    if not queries:
        raise RuntimeError("No JQL queries configured (config/settings.json)")

    tickets_by_key: dict[str, dict] = {}
    for q in queries:
        label, jql = q.get("name", "?"), q.get("jql", "")
        if not jql:
            continue
        _log(db, run_id, f"searching JQL '{label}'")
        try:
            found = jira.search(jql, max_results=settings.max_tickets_per_run)
        except Exception as e:  # noqa: BLE001
            _log(db, run_id, f"JQL '{label}' failed: {e}", level="error")
            run.status = "failed"
            run.error = f"JQL '{label}': {e}"
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            return
        for t in found:
            tickets_by_key.setdefault(t["key"], t)

    limited = list(tickets_by_key.values())[: settings.max_tickets_per_run]
    if not limited:
        _log(db, run_id, "no tickets matched")
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return
    _log(db, run_id, f"{len(limited)} tickets matched")

    tickets = []
    for t in limited:
        row = Ticket(run_id=run_id, key=t["key"], project=t.get("project", ""),
                     summary=t.get("summary", ""), description=t.get("description", ""),
                     issue_type=t.get("issue_type", ""), status_name=t.get("status_name", ""),
                     stage="incoming")
        db.add(row)
        tickets.append(row)
    db.commit()

    # --- context assembly: linked commits & PRs ---
    run.status = "triaging"
    db.commit()
    for row in tickets:
        links: list[dict] = []
        try:
            links += jira.get_devinfo(row.key, settings.jira_devinfo_field)
        except Exception as e:  # noqa: BLE001
            _log(db, run_id, f"{row.key}: devinfo failed: {e}", level="warn", key=row.key)
        try:
            links += search_context(settings, row.key)
        except Exception as e:  # noqa: BLE001
            _log(db, run_id, f"{row.key}: github context failed: {e}", level="warn", key=row.key)
        for l in links:
            db.add(Link(ticket_id=row.id, kind=l.get("kind", ""), source=l.get("source", ""),
                        url=l.get("url", ""), repo=l.get("repo", ""), title=l.get("title", ""),
                        sha=l.get("sha", ""), pr_state=l.get("pr_state", ""), meta=l.get("meta", {})))
    db.commit()

    repos = configured_repos(settings)
    from .repo_resolve import resolve_repo

    for row in tickets:
        row.repo = resolve_repo(
            row.key, row.project, row.summary, row.description,
            [l.to_dict() for l in row.links], repos,
            repo_map=settings.repo_map, project_map=settings.github_project_map,
        )
    db.commit()

    # --- triage each ticket + build a plan ---
    partial = False
    for row in tickets:
        try:
            ctx = build_ticket_context(row.to_dict(), [l.to_dict() for l in row.links])
            result = run_triage(settings, settings.workspace / f"run-{run_id}", ctx, paths)
            row.stage = "triaged"
            row.triage_path_id = result.path_id
            row.triage_reason = result.reason
            row.triage_confidence = result.confidence
            row.need_my_input = result.need_my_input
            db.commit()
            _make_plan(db, run_id, row, result, paths)
        except Exception as e:  # noqa: BLE001
            row.stage = "failed"
            row.error = str(e)
            partial = True
            _log(db, run_id, f"{row.key}: triage failed: {e}", level="error", key=row.key)
            db.commit()

    run.status = "partial" if partial else "completed"
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    _log(db, run_id, f"finished ({run.status})")


def _make_plan(db, run_id: int, row: Ticket, result, paths) -> None:
    path = get_path(paths, result.path_id)
    if path and path.required_backend == "github":
        _make_code_plan(db, run_id, row, result, path)
    else:
        _make_chat_plan(db, run_id, row, result, path)


def _make_code_plan(db, run_id: int, row: Ticket, result, path) -> None:
    """Code path: action agent works in a sandbox clone, patch is captured for review."""
    from .action_agent import run_for_ticket

    ctx = build_ticket_context(row.to_dict(), [l.to_dict() for l in row.links])
    plan_input = run_for_ticket(run_id, row.key, ctx, path, repo=row.repo)
    plan = ActionPlan(ticket_id=row.id, run_id=run_id, path_id=result.path_id,
                      summary=plan_input.summary or row.summary, narrative=plan_input.narrative)
    for i, a in enumerate(plan_input.actions):
        preview = a.preview or executor.preview_action(a.kind, a.params)
        plan.actions.append(Action(seq=i, kind=a.kind, params=a.params, preview=preview))
    db.add(plan)
    row.stage = "awaiting_approval"
    db.commit()


def _make_chat_plan(db, run_id: int, row: Ticket, result, path) -> None:
    draft = _draft_comment(row, result)
    defaults = (path.default_actions if path else []) or [{"kind": "comment", "params": {"body": draft}}]
    plan = ActionPlan(ticket_id=row.id, run_id=run_id, path_id=result.path_id,
                      summary=row.summary, narrative=result.reason)
    for i, act in enumerate(defaults):
        kind = act.get("kind", "comment")
        params = dict(act.get("params", {}))
        if "body" in params and not params["body"]:
            params["body"] = draft
        plan.actions.append(Action(seq=i, kind=kind, params=params,
                                   preview=executor.preview_action(kind, params)))
    db.add(plan)
    row.stage = "awaiting_approval"
    db.commit()


def _draft_comment(row: Ticket, result) -> str:
    lines = [f"AI triage: **{result.path_id}** (confidence {result.confidence:.0%})."]
    if result.reason:
        lines.append("")
        lines.append(result.reason)
    if result.need_my_input:
        lines.append("")
        lines.append("A decision from a human is needed before this ticket moves forward.")
    return "\n".join(lines)