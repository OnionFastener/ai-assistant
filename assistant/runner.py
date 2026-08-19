"""Pipeline orchestrator: schedule(/manual) trigger → fetch → context → triage → plan."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select
from datetime import datetime, timezone

from . import executor
from .config import settings
from .db import SessionLocal
from .integrations import build_jira, configured_repos, search_context
from .models import Action, ActionPlan, Link, PatchWorker, Run, RunLog, Ticket
from .paths import get_path, load_paths
from .triage import build_ticket_context, run_triage
from .schemas import TriageResult

log = logging.getLogger("assistant.runner")

TRIAGE_WORKERS = 3
PLAN_WORKERS = 2


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



def _stop_requested(db, run, run_id: int) -> bool:
    db.refresh(run)
    if run.status not in ("stopping", "stopped"):
        return False
    run.status = "stopped"
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    _log(db, run_id, "stopped; completed tickets remain in approvals")
    return True
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
    jira = build_jira(settings) if settings.scan_jira else None

    import shutil
    shutil.rmtree(settings.workspace / f"run-{run_id}", ignore_errors=True)
    if _stop_requested(db, run, run_id):
        return
    run.status = "fetching"
    db.commit()
    _log(db, run_id, "started")

    from .issue_sources import fetch_tickets
    tickets_by_key = fetch_tickets(
        settings, jira, jql_override,
        lambda message: _log(db, run_id, message),
    )

    # Local dedupe on key from pending plans (DESIGN §6): a ticket already waiting
    # for review is not re-fetched, so repeated runs cannot pile up duplicate plans.
    keys = list(tickets_by_key)
    if keys:
        pending = (
            db.query(ActionPlan)
            .join(Ticket, ActionPlan.ticket_id == Ticket.id)
            .filter(Ticket.key.in_(keys), ActionPlan.review_status.in_(("pending", "preparing")))
            .all()
        )
        skipping = {p.ticket.key for p in pending}
        if skipping:
            tickets_by_key = {k: v for k, v in tickets_by_key.items() if k not in skipping}
            _log(db, run_id, f"skipped {len(skipping)} tickets already awaiting review")

    limited = list(tickets_by_key.values())[: settings.max_tickets_per_run]
    if not limited:
        _log(db, run_id, "no tickets matched")
        run.status = "completed"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        return
    _log(db, run_id, f"{len(limited)} tickets matched")
    if _stop_requested(db, run, run_id):
        return

    tickets = []
    for t in limited:
        row = Ticket(run_id=run_id, key=t["key"], project=t.get("project", ""), repo=t.get("repo", ""),
                     summary=t.get("summary", ""), description=t.get("description", ""),
                     issue_type=t.get("issue_type", ""), status_name=t.get("status_name", ""), stage="incoming")
        if t.get("url"):
            row.links.append(Link(kind="issue", source="GitHub", url=t["url"], repo=t.get("repo", ""), title=t.get("summary", ""), meta={"number": t.get("number"), "labels": t.get("labels", [])}))
        db.add(row)
        tickets.append(row)
    db.commit()

    run.status = "triaging"
    db.commit()
    for row in tickets:
        if row.key.startswith("GH:"):
            continue
        links: list[dict] = []
        try:
            links += jira.get_devinfo(row.key, settings.jira_devinfo_field)
        except Exception as e:  # noqa: BLE001
            _log(db, run_id, f"{row.key}: devinfo failed: {e}", level="warn", key=row.key)
        try:
            comments = jira.get_comments(row.key)
            if comments:
                section = "\n\n## Comments\n" + "\n\n".join(
                    f"({c.get('created', '')} {c.get('author', '')}): {c.get('body', '')}" for c in comments
                )
                row.description = (row.description + section).strip()
        except Exception as e:  # noqa: BLE001
            _log(db, run_id, f"{row.key}: comments failed: {e}", level="warn", key=row.key)
        try:
            links += search_context(settings, row.key)
        except Exception as e:  # noqa: BLE001
            _log(db, run_id, f"{row.key}: github context failed: {e}", level="warn", key=row.key)
        for link in links:
            db.add(Link(ticket_id=row.id, kind=link.get("kind", ""), source=link.get("source", ""),
                        url=link.get("url", ""), repo=link.get("repo", ""), title=link.get("title", ""),
                        sha=link.get("sha", ""), pr_state=link.get("pr_state", ""), meta=link.get("meta", {})))
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

    from .triage_config import load_triage_config

    triage_cfg = load_triage_config()
    partial = False
    contexts = {
        row.id: build_ticket_context(row.to_dict(), [l.to_dict() for l in row.links], triage_cfg)
        for row in tickets
    }
    _log(db, run_id, f"triaging {len(tickets)} tickets with up to {min(TRIAGE_WORKERS, len(tickets))} parallel agents")
    triage_results = []
    with ThreadPoolExecutor(max_workers=min(TRIAGE_WORKERS, len(tickets))) as pool:
        triages = {
            row.id: pool.submit(
                run_triage, settings, settings.workspace / f"run-{run_id}" / f"triage-{row.id}",
                contexts[row.id], paths, triage_cfg,
            )
            for row in tickets
        }
        for row in tickets:
            if _stop_requested(db, run, run_id):
                return
            try:
                result = triages[row.id].result()
                row.stage = "triaged"
                row.triage_path_id = result.path_id
                row.triage_reason = result.reason
                row.triage_confidence = result.confidence
                row.need_my_input = result.need_my_input
                db.commit()
                triage_results.append((row, result))
            except Exception as e:  # noqa: BLE001
                row.stage = "failed"
                row.error = str(e)
                partial = True
                _log(db, run_id, f"{row.key}: triage failed: {e}", level="error", key=row.key)
                db.commit()
    for row, result in triage_results:
        if _stop_requested(db, run, run_id):
            return
        path = get_path(paths, result.path_id)
        try:
            if path and path.required_backend == "github" and not settings.mock:
                _make_code_proposal(db, run_id, row, result, path)
            else:
                _make_plan(db, run_id, row, result, paths, triage_cfg)
        except Exception as e:  # noqa: BLE001
            row.stage = "failed"
            row.error = str(e)
            partial = True
            _log(db, run_id, f"{row.key}: plan failed: {e}", level="error", key=row.key)
            db.commit()
    run.status = "partial" if partial else "completed"
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    _log(db, run_id, f"finished ({run.status})")


def _make_plan(db, run_id: int, row: Ticket, result, paths, triage_cfg=None) -> None:
    _supersede_prior_pending(db, row.key)
    path = get_path(paths, result.path_id)
    if path and path.required_backend == "github":
        _make_code_plan(db, run_id, row, result, path, triage_cfg)
    else:
        _make_chat_plan(db, run_id, row, result, path)


def _supersede_prior_pending(db, ticket_key: str) -> None:
    """A new plan for a ticket invalidates older pending plans of the same ticket
    (one actionable plan per ticket at any time). DESIGN: marked `superseded`."""
    older = (
        db.query(ActionPlan)
        .join(Ticket, ActionPlan.ticket_id == Ticket.id)
        .filter(Ticket.key == ticket_key, ActionPlan.review_status == "pending")
        .all()
    )
    for plan in older:
        plan.review_status = "superseded"
    if older:
        db.commit()


def _make_code_plan(db, run_id: int, row: Ticket, result, path, triage_cfg=None) -> None:
    """Code path: action agent works in a sandbox clone, patch is captured for review."""
    plan_input = _build_code_plan(run_id, row, result, path, triage_cfg)
    _save_code_plan(db, run_id, row, result, plan_input)


def _feature_scope_review(summary: str, description: str, path_id: str) -> str:
    if path_id != "new-feature":
        return ""
    text = f"{summary}\n{description}".lower()
    acceptance = text.count("[ ]") + text.count("- [x]") + text.count("acceptance criteria")
    surfaces = sum(term in text for term in ("api", "cli", "integration", "configuration", "documentation", "security", "migration", "dashboard"))
    if acceptance >= 5 or (len(text) >= 1800 and surfaces >= 3):
        return (f"Scope review required: this feature spans {max(acceptance, 1)} acceptance criteria and "
                f"{surfaces} implementation surfaces. Confirm the scope before starting a patch preparation.")
    return ""


def _make_code_proposal(db, run_id: int, row: Ticket, result, path) -> None:
    _supersede_prior_pending(db, row.key)
    scope_review = _feature_scope_review(row.summary, row.description, result.path_id)
    narrative = scope_review or result.reason or "Review the ticket and prepare a patch if you want to proceed."
    plan = ActionPlan(ticket_id=row.id, run_id=run_id, path_id=result.path_id, summary=row.summary, narrative=narrative)
    body = (f"AI scope review: {narrative}" if scope_review else
            f"AI proposal: {narrative}\n\nSelect ‘Prepare patch’ to generate a reviewable diff.")
    plan.actions.append(Action(seq=0, kind="comment", params={"body": body, "scope_review_required": bool(scope_review)}, preview="Scope confirmation required before patch preparation." if scope_review else "Proposal only — no patch has been generated."))
    db.add(plan)
    row.stage = "awaiting_approval"
    db.commit()


def prepare_patch(plan_id: int) -> None:
    db = SessionLocal()
    try:
        proposal = db.get(ActionPlan, plan_id)
        if not proposal or proposal.review_status != "preparing":
            return
        row = proposal.ticket
        path = get_path(load_paths(settings.paths_dir), proposal.path_id)
        if not path or path.required_backend != "github":
            raise ValueError("This proposal does not require a code patch")
        result = TriageResult(path_id=proposal.path_id, confidence=row.triage_confidence, reason=row.triage_reason, need_my_input=row.need_my_input)
        def record_worker(pid: int, started: str) -> None:
            worker = db.scalar(select(PatchWorker).where(PatchWorker.plan_id == plan_id))
            if worker:
                worker.pid = pid
                worker.process_start = started
                db.commit()

        _log(db, proposal.run_id, f"{row.key}: patch preparation started", key=row.key)
        plan_input = _build_code_plan(proposal.run_id, row, result, path, on_agent_started=record_worker)
        db.refresh(proposal)
        if proposal.review_status != "preparing":
            return
        _save_code_plan(db, proposal.run_id, row, result, plan_input)
        proposal.review_status = "superseded"
        proposal.error = "Patch prepared as a separate review plan."
        db.commit()
        _log(db, proposal.run_id, f"{row.key}: patch ready for review", key=row.key)
    except Exception as e:  # noqa: BLE001
        proposal = db.get(ActionPlan, plan_id)
        if proposal:
            detail = str(e)
            proposal.review_status = "pending"
            proposal.error = ("Patch preparation timed out while the agent was producing its final review plan. "
                              "No patch was submitted; you can try again.") if "timeout_seconds=" in detail else (
                              "Patch preparation failed before a reviewable patch was produced. See run diagnostics for details.")
            db.commit()
            _log(db, proposal.run_id, f"{proposal.ticket.key}: patch preparation failed: {detail}", level="error", key=proposal.ticket.key)
    finally:
        try:
            worker = db.scalar(select(PatchWorker).where(PatchWorker.plan_id == plan_id))
            if worker:
                db.delete(worker)
                db.commit()
        except Exception:  # noqa: BLE001
            pass
        db.close()


def _build_code_plan(run_id: int, row: Ticket, result, path, triage_cfg=None, on_agent_started=None):
    from .action_agent import run_for_ticket

    ctx = build_ticket_context(row.to_dict(), [l.to_dict() for l in row.links], triage_cfg)
    return run_for_ticket(run_id, row.key, ctx, path, repo=row.repo, on_agent_started=on_agent_started)


def _save_code_plan(db, run_id: int, row: Ticket, result, plan_input) -> None:
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
