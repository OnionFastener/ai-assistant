"""Path action agent: produces an Action Plan for a routed ticket.

For code paths this works in a sandbox git clone, then the working-tree changes are
captured deterministically (`git diff`) into the plan so the executor can replay the
exact approved patch later. Mock mode simulates an agent fix so the whole sandbox +
diff + push flow runs with no model.
"""
from __future__ import annotations
import re

import json
import logging
import shutil
from pathlib import Path

from pydantic import ValidationError

from . import opencode_runner as op
from .config import settings
from .integrations import gitutil
from .schemas import ActionPlanInput

log = logging.getLogger("assistant.action_agent")


def run_for_ticket(run_id: int, ticket_key: str, ticket_ctx: dict, path, repo: str = "") -> ActionPlanInput:
    """Main entry. Returns a full action plan (mock or real)."""
    from .action_config import load_action_config

    workspace_root = settings.workspace / f"run-{run_id}"
    sandbox = prepare_sandbox(run_id, ticket_key, workspace_root, repo)
    behavior = (path.behavior or "")[:4000]
    allowed = ", ".join(path.allowed_actions or [])

    if settings.mock:
        plan_json = _mock_fix(run_id, ticket_key, ticket_ctx, workspace_root, sandbox)
    else:
        wrapper = load_action_config().instruct or ""
        prompt = _build_prompt(ticket_ctx, behavior, allowed, wrapper)
        text = op.run_agent(prompt, agent="action-worker", cwd=str(sandbox),
                            model=settings.model_action)
        plan_json = _parse_json_retry(text, prompt, "action-worker", sandbox)

    plan = _normalize_plan(plan_json, run_id, ticket_key, sandbox, github_repo=repo)
    return plan


def prepare_sandbox(run_id: int, ticket_key: str, workspace_root: Path, repo: str = "") -> Path:
    ticket_dir = workspace_root / ticket_key
    repo_dir = ticket_dir / "repo"
    shutil.rmtree(repo_dir, ignore_errors=True)
    try:
        if settings.mock:
            remote_root = workspace_root / "_remote"
            gitutil.setup_mock_repo(remote_root, gitutil.remote_name(repo))
            gitutil.clone_local(remote_root / f"{gitutil.remote_name(repo)}.git", repo_dir)
        else:
            gitutil.clone_repo(settings, repo_dir, repo=repo or None)
        (ticket_dir / "context.json").write_text(
            json.dumps({"key": ticket_key, "repo": repo}, indent=2))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"sandbox prep failed: {e}") from e
    return repo_dir


def _build_prompt(ticket_ctx: dict, behavior: str, allowed: str, wrapper: str = "") -> str:
    return (
        (wrapper + "\n\n" if wrapper else "") +
        "You are routed a Jira ticket for the path whose instructions follow.\n\n"
        f"PATH BEHAVIOR (behavior.md / instruct.md):\n{behavior}\n\n"
        f"Your allowed_actions: [{allowed}]\n\n"
        "TICKET CONTEXT (JSON):\n" + json.dumps(ticket_ctx, ensure_ascii=False, default=str) +
        "\n\nYou are in the repository sandbox. REPRODUCE the issue, make the minimal fix, add/"
        "adjust a regression test, and run the relevant tests. Leave your changes in the working "
        "tree (do NOT commit, push, or open a PR — those happen only after human approval).\n\n"
        "Emit EXACTLY one JSON object, no markdown fences, no prose, no narration, no planning "
        "commentary. Do a final sentence like 'here is the plan' only if unavoidable — the JSON "
        "is the LAST text in your response and is parseable on its own:\n"
        '{"summary": string, "narrative": string, "actions": ['
        '{"kind": string, "params": {object}, "preview": string}]}\n'
        "For a bug fix include actions: comment (body), push_branch (base, branch_name like "
        "'fix/<key-slug>', commit_msg), create_pr (title, body, target_branch), and optionally "
        "transition (to) / assign (assignee). Every params value must be complete and final."
    )


def _parse_json_retry(text: str, prompt: str, agent: str, cwd: Path) -> dict:
    data = _parse_plan_json(text)
    if data is not None:
        return data
    contract = (
        'Respond with ONLY ONE JSON object, no markdown fences, no prose, matching the exact '
        'contract from the original prompt: '
        '{"summary": string, "narrative": string, "actions": ['
        '{"kind": string, "params": {object}, "preview": string}]}.'
    )
    for _ in range(3):
        text2 = op.run_agent(f"{contract}\n\n{prompt}", agent=agent, cwd=str(cwd),
                             model=settings.model_action)
        data = _parse_plan_json(text2)
        if data is not None:
            return data
    raise ValueError(f"action-agent returned unparsable output: {text[:300]}")


def _parse_plan_json(text: str) -> dict | None:
    if not text:
        return None
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
        candidates.append(text[start:])
    fallback: dict | None = None
    for cand in candidates:
        for cleaned in _json_variants(cand):
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                if "actions" in data:
                    return data
                fallback = fallback or data
    return fallback


def _json_variants(text: str) -> list[str]:
    variants = [text]
    for pat in ("```json", "```"):
        if pat in text:
            variants.append(text.split(pat, 1)[1].split("```", 1)[0])
    return variants


def _normalize_plan(plan_json: dict, run_id: int, ticket_key: str, repo: Path, github_repo: str = "") -> ActionPlanInput:
    try:
        plan = ActionPlanInput.model_validate(plan_json)
    except ValidationError as e:
        raise ValueError(f"invalid action plan: {e}") from e

    default_branch = "main"
    if not settings.mock:
        try:
            default_branch = gitutil.default_branch(settings, repo)
        except Exception:  # noqa: BLE001
            pass
    if ticket_key.startswith("GH:"):
        plan.actions = [a for a in plan.actions if a.kind not in ("transition", "assign")]

    has_code = any(a.kind in ("push_branch", "create_pr") for a in plan.actions)
    if has_code:
        patch = gitutil.stage_and_diff(repo)
        branch_name = _ensure_branch_name(plan, run_id, ticket_key)
        for a in plan.actions:
            if a.kind == "push_branch":
                p = dict(a.params)
                p["base"] = p.get("base") or default_branch
                p["branch_name"] = branch_name
                p["commit_msg"] = p.get("commit_msg") or f"Fix {ticket_key}: {plan.summary or 'see ticket'}"
                p["patch"] = patch
                p["patch_sha"] = gitutil.patch_sha(patch)
                if github_repo:
                    p["repo"] = github_repo
                a.params = p
                if not a.preview:
                    a.preview = patch
            elif a.kind == "create_pr":
                p = dict(a.params)
                p.setdefault("head", branch_name)
                p.setdefault("target_branch", default_branch)
                p["title"] = p.get("title") or f"Fix {ticket_key}"
                p["body"] = p.get("body") or plan.narrative or ""
                if github_repo:
                    p["repo"] = github_repo
                a.params = p
                if not a.preview:
                    a.preview = f"PR {p['title']} ({p['head']} → {p['target_branch']})\n\n{p['body']}"
    return plan


def _ensure_branch_name(plan: ActionPlanInput, run_id: int, ticket_key: str) -> str:
    if not ticket_key.startswith("GH:"):
        for action in plan.actions:
            if action.kind == "push_branch" and action.params.get("branch_name"):
                return action.params["branch_name"]
    return f"fix/{_branch_slug(ticket_key)}-{run_id}"


def _branch_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip(".-") or "ticket"


def _mock_fix(run_id: int, ticket_key: str, ticket_ctx: dict, workspace: Path, repo: Path) -> dict:
    """Deterministic stand-in for the model: append a fix to service.py, build the plan."""
    fix_line = f'        # {ticket_key}: guard float rounding (mock fix by AI assistant run {run_id})\n'
    src = (repo / "service.py").read_text()
    (repo / "service.py").write_text(src.replace("shipping=0.0):", f"shipping=0.0):\n{fix_line}"))
    summary = f"Fix {ticket_key}: free-shipping total regression"
    narrative = (f"Mock fix for {ticket_key}. Root cause: total() did not handle the shipping=0 "
                 "edge case, returning 0.0. Patch adds a guard and is captured as a reviewable diff.")
    body = (f"## Root cause\n`total()` ignored the subtotal when shipping was 0.\n\n"
            f"## Verification\nRun `python -m pytest` after the fix (mock run {run_id}).\n")
    return {
        "summary": summary,
        "narrative": narrative,
        "actions": [
            {"kind": "comment",
             "params": {"body": f"AI fix prepared (approval pending).\n\n{narrative}"},
             "preview": ""},
            {"kind": "push_branch",
             "params": {"branch_name": f"fix/{ticket_key.lower()}-{run_id}", "commit_msg": summary},
             "preview": ""},
            {"kind": "create_pr",
             "params": {"title": summary, "body": body, "target_branch": "main"},
             "preview": ""},
            {"kind": "transition", "params": {"to": "In Review"}, "preview": ""},
        ],
    }
