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
from collections.abc import Callable

from pydantic import ValidationError

from . import opencode_runner as op
from .config import settings
from .integrations import gitutil
from .schemas import ActionPlanInput

log = logging.getLogger("assistant.action_agent")

DEFAULT_ACTION_TIMEOUT = 300
ACTION_RETRY_TIMEOUT = 45
MAX_PLAN_FORMAT_RETRIES = 1


def run_for_ticket(run_id: int, ticket_key: str, ticket_ctx: dict, path, repo: str = "", on_agent_started: Callable[[int, str], None] | None = None) -> ActionPlanInput:
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
        preflight = _repo_preflight(sandbox, ticket_ctx)
        prompt = _build_prompt(ticket_ctx, behavior, allowed, wrapper, preflight)
        timeout = _action_timeout(path)
        text = op.run_agent(prompt, agent="action-worker", cwd=str(sandbox),
                            model=settings.model_action, timeout=timeout, on_started=on_agent_started)
        plan_json = _code_plan_from_agent_summary(ticket_ctx, text)

    plan = _normalize_plan(plan_json, run_id, ticket_key, sandbox, github_repo=repo)
    return plan


def _action_timeout(path) -> int:
    try:
        timeout = int((path.work or {}).get("action_timeout_seconds", DEFAULT_ACTION_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_ACTION_TIMEOUT
    return max(30, min(timeout, 3600))


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
            gitutil.clone_cached_repo(settings, workspace_root / "_repo_cache", repo_dir, repo=repo or None)
        (ticket_dir / "context.json").write_text(
            json.dumps({"key": ticket_key, "repo": repo}, indent=2))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"sandbox prep failed: {e}") from e
    return repo_dir


def _repo_preflight(repo: Path, ticket_ctx: dict) -> str:
    manifests = [name for name in ("pyproject.toml", "pytest.ini", "package.json", "go.mod", "Cargo.toml", "Makefile") if (repo / name).exists()]
    test_hint = ""
    if "pyproject.toml" in manifests or "pytest.ini" in manifests:
        test_hint = "pytest <relevant-test-file-or-node>"
    elif "package.json" in manifests:
        test_hint = "npm test -- <relevant-test-file-or-pattern>"
    elif "go.mod" in manifests:
        test_hint = "go test ./<relevant-package>"
    elif "Cargo.toml" in manifests:
        test_hint = "cargo test <relevant-test-name>"

    text = "\n".join(str(ticket_ctx.get(field) or "") for field in ("summary", "description"))
    candidates = re.findall(r"(?<![\w-])([A-Za-z0-9_./-]+\.(?:py|js|ts|tsx|go|rs|java|rb|php|cs|json|toml|ya?ml|md))(?![\w-])", text)
    referenced = []
    for candidate in candidates:
        candidate = candidate.lstrip("./")
        if candidate and (repo / candidate).is_file() and candidate not in referenced:
            referenced.append(candidate)
    lines = ["REPOSITORY PREFLIGHT (already checked):", f"- manifests: {', '.join(manifests) or 'none detected'}"]
    if test_hint:
        lines.append(f"- likely focused test command: {test_hint}")
    if referenced:
        lines.append(f"- ticket-referenced files present: {', '.join(referenced[:12])}")
    return "\n".join(lines)


def _build_prompt(ticket_ctx: dict, behavior: str, allowed: str, wrapper: str = "", preflight: str = "") -> str:
    return (
        (wrapper + "\n\n" if wrapper else "") +
        "You are routed a Jira ticket for the path whose instructions follow.\n\n"
        f"PATH BEHAVIOR (behavior.md / instruct.md):\n{behavior}\n\n"
        f"Your allowed_actions: [{allowed}]\n\n"
        + (preflight + "\n\n" if preflight else "")
        + "TICKET CONTEXT (JSON):\n" + json.dumps(ticket_ctx, ensure_ascii=False, default=str) +
        "\n\nWork efficiently: inspect the repository root and the files named or implied by the "
        "ticket first. Do not install dependencies, start services, use the network, or run a full "
        "test suite. Run at most two focused tests that cover the changed behavior. If no focused "
        "test can run, state why in the narrative.\n\nYou are in the repository sandbox. REPRODUCE "
        "the issue, make the minimal fix, add/adjust a regression test, and run the relevant tests. "
        "Leave your changes in the working tree (do NOT commit, push, or open a PR — those happen "
        "only after human approval).\n\n"
        "Finish with a concise plain-text handoff for the reviewer: changed files, root cause or "
        "implementation note, and focused tests run (or why none could run). Do not emit JSON and "
        "do not propose actions; the application builds the review plan deterministically from the diff."
    )


def _code_plan_from_agent_summary(ticket_ctx: dict, text: str) -> dict:
    summary = str(ticket_ctx.get("summary") or ticket_ctx.get("key") or "Prepare requested change").strip()
    narrative = (text or "The agent completed its implementation pass; review the captured diff and run verification before approval.").strip()[:6000]
    return {
        "summary": summary,
        "narrative": narrative,
        "actions": [
            {"kind": "comment", "params": {"body": f"AI patch prepared for review.\n\n{narrative}"}, "preview": ""},
            {"kind": "push_branch", "params": {"commit_msg": summary}, "preview": ""},
            {"kind": "create_pr", "params": {"title": summary, "body": narrative}, "preview": ""},
        ],
    }


def _parse_json_retry(text: str, prompt: str, agent: str, cwd: Path, on_agent_started: Callable[[int, str], None] | None = None) -> dict:
    data = _parse_plan_json(text)
    if data is not None:
        return data
    contract = (
        'Respond with ONLY ONE JSON object, no markdown fences, no prose, matching the exact '
        'contract from the original prompt: '
        '{"summary": string, "narrative": string, "actions": ['
        '{"kind": string, "params": {object}, "preview": string}]}.'
    )
    for _ in range(MAX_PLAN_FORMAT_RETRIES):
        text2 = op.run_agent(f"{contract}\n\n{prompt}", agent=agent, cwd=str(cwd),
                             model=settings.model_action, timeout=ACTION_RETRY_TIMEOUT,
                             on_started=on_agent_started)
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
