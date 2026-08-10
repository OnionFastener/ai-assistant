"""Deterministic action executor.

Only ever runs on an approved plan. Each action kind maps to one small handler; handlers
never call a model. Code actions (push_branch / create_pr) replay the exact patch that was
previewed — patch hash is re-verified at apply time, closing the review→execute TOCTOU gap.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from .integrations import gitutil
from .integrations.jira import JiraClient, MockJiraClient

log = logging.getLogger("assistant.executor")

CRITICAL_KINDS = {"push_branch", "create_pr"}
IMPLEMENTED = ("comment", "transition", "assign", "push_branch", "create_pr")

_HANDLERS: dict[str, callable] = {}


def register(kind: str):
    def deco(fn):
        _HANDLERS[kind] = fn
        return fn
    return deco


@dataclass
class ExecContext:
    jira: JiraClient | MockJiraClient
    github: object
    settings: object
    workspace: Path

    def exec_dir(self, plan_id: int, seq: int) -> Path:
        d = self.workspace / f"exec-{plan_id}-{seq}"
        shutil.rmtree(d, ignore_errors=True)
        return d


@register("comment")
def _comment(ctx: ExecContext, plan, action) -> list[str]:
    body = str(action.params.get("body", "")).strip()
    if not body:
        raise ValueError("comment requires non-empty 'body'")
    footer = f"\n\n---\n_Posted by AI assistant (approved). Run {plan.run_id}, ticket {plan.ticket.key}._"
    ctx.jira.add_comment(plan.ticket.key, body + footer)
    return ["comment added"]


@register("transition")
def _transition(ctx: ExecContext, plan, action) -> list[str]:
    to = str(action.params.get("to", "")).strip()
    if not to:
        raise ValueError("transition requires 'to'")
    name = ctx.jira.transition(plan.ticket.key, to)
    return [f"transitioned to '{name}'"]


@register("assign")
def _assign(ctx: ExecContext, plan, action) -> list[str]:
    assignee = str(action.params.get("assignee", "")).strip()
    if not assignee:
        raise ValueError("assign requires 'assignee'")
    if assignee.lower() in ("me", "self", "current"):
        assignee = ctx.jira.account_id
    ctx.jira.assign(plan.ticket.key, assignee)
    return [f"assigned to {assignee}"]


def _push_repo_dir(ctx: ExecContext, plan, params: dict) -> tuple[Path, str]:
    """Return (dest, repo) cloning the repo an action targets (per-plan 'repo' or default)."""
    repo = str(params.get("repo", "")).strip()
    dest = ctx.exec_dir(plan.id, 0)
    if ctx.settings.mock:
        name = gitutil.remote_name(repo or ctx.settings.github_repo)
        remote = ctx.workspace / f"run-{plan.run_id}" / "_remote" / f"{name}.git"
        if not remote.exists():
            raise RuntimeError(f"mock remote for '{name}' missing — cannot execute push")
        gitutil.clone_local(remote, dest)
    else:
        gitutil.clone_repo(ctx.settings, dest, repo=repo or None)
    return dest, repo


@register("push_branch")
def _push_branch(ctx: ExecContext, plan, action) -> list[str]:
    params = action.params
    branch = str(params.get("branch_name", "")).strip()
    message = str(params.get("commit_msg", "")).strip()
    patch = params.get("patch", "")
    if not branch:
        raise ValueError("push_branch requires 'branch_name'")
    if not patch:
        raise ValueError("push_branch requires the captured 'patch'")

    dest, repo = _push_repo_dir(ctx, plan, params)
    try:
        gitutil.apply_patch(dest, patch)                      # hash-verified vs preview
        sha = gitutil.commit_all(dest, message or f"Fix {plan.ticket.key}")
        gitutil.push_branch(dest, branch)
    finally:
        shutil.rmtree(dest, ignore_errors=True)
    if hasattr(ctx.github, "pushed"):
        ctx.github.pushed.append((repo, plan.ticket.key, branch))
    return [f"pushed {repo or 'default'} branch '{branch}' @ {sha}"]


@register("create_pr")
def _create_pr(ctx: ExecContext, plan, action) -> list[str]:
    from .integrations import build_github

    params = action.params
    head = str(params.get("head", "") or params.get("branch_name", "")).strip()
    base = str(params.get("target_branch", "") or params.get("base", "")).strip()
    title = str(params.get("title", "")).strip()
    body = str(params.get("body", ""))
    repo = str(params.get("repo", "") or ctx.settings.github_repo).strip()
    if not (head and base and title):
        raise ValueError("create_pr requires head (branch), target_branch and title")
    if not repo:
        raise ValueError("create_pr requires a GitHub repo (set repos or ASST_GITHUB_REPOS)")
    if ctx.settings.mock:
        _ensure_branch_pushed(ctx, plan, head, repo)
    gh = build_github(ctx.settings, repo)
    pr = gh.create_pr(head=head, base=base, title=title, body=body)
    return [f"opened PR #{pr.get('number')} {pr.get('html_url')} ({repo})"]


def _ensure_branch_pushed(ctx: ExecContext, plan, head: str, repo: str = "") -> None:
    """Cheap guard so create_pr can't run before the branch push (mock only)."""
    name = gitutil.remote_name(repo or ctx.settings.github_repo)
    remote = ctx.workspace / f"run-{plan.run_id}" / "_remote" / f"{name}.git"
    refs = gitutil.run_git(remote.parent, "ls-remote", str(remote), f"refs/heads/{head}")
    if head not in refs:
        raise ValueError(f"head branch '{head}' was not pushed (create_pr depends on push_branch)")


def preview_action(kind: str, params: dict) -> str:
    if kind == "comment":
        return str(params.get("body", "")).strip()
    if kind == "transition":
        return f"Change status to '{params.get('to', '')}'"
    if kind == "assign":
        return f"Assign to {params.get('assignee', '')}"
    if kind == "push_branch":
        patch = params.get("patch", "")
        return f"Push branch '{params.get('branch_name', '')}' (commit: {params.get('commit_msg', '')})\n\n" + patch
    if kind == "create_pr":
        return f"PR: {params.get('title', '')} ({params.get('head', '')} → {params.get('target_branch', '')})\n\n{params.get('body', '')}"
    return f"{kind}: {params}"


def execute_plan(ctx: ExecContext, plan, allowed_actions: set[str]) -> tuple[str, list[str]]:
    """Run enabled actions in seq order. Returns (final_status, results)."""
    results: list[str] = []
    status = "executed"
    for action in plan.actions:
        if not action.enabled:
            action.exec_status = "skipped"
            continue
        if action.kind not in allowed_actions:
            action.exec_status = "failed"
            action.exec_result = f"action '{action.kind}' not allowed by path"
            results.append(f"SKIP {action.kind}: not allowed by path")
            status = "failed"
            continue
        try:
            handler = _HANDLERS.get(action.kind)
            if handler is None:
                raise NotImplementedError(f"no handler for '{action.kind}'")
            outcome = handler(ctx, plan, action)
            action.exec_status = "ok"
            action.exec_result = "; ".join(outcome)
            results.append(f"OK {action.kind}: {'; '.join(outcome)}")
        except NotImplementedError as e:
            action.exec_status = "failed"
            action.exec_result = str(e)
            results.append(f"FAIL {action.kind}: {e}")
            if action.kind in CRITICAL_KINDS:
                status = "failed"
                break
        except Exception as e:  # noqa: BLE001
            action.exec_status = "failed"
            action.exec_result = str(e)
            results.append(f"FAIL {action.kind}: {e}")
            if action.kind in CRITICAL_KINDS:
                status = "failed"
                break
    return status, results