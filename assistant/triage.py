"""AI triage: classify a Jira ticket into a configured path.

Real mode invokes `opencode run --agent triage`; mock mode uses a deterministic
keyword classifier so the whole pipeline can be exercised without a model.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from . import opencode_runner as op
from .paths import TriagePath
from .schemas import TriageResult

log = logging.getLogger("assistant.triage")

TICKET_CONTEXT_FIELDS = ["key", "project", "summary", "issue_type", "status_name", "description", "labels"]


def build_ticket_context(ticket: dict, links: list[dict]) -> dict:
    """Compact context handed to the triage agent."""
    ctx = {k: ticket.get(k) for k in TICKET_CONTEXT_FIELDS}
    ctx["labels"] = ticket.get("labels") or []
    ctx["links"] = []
    for l in links:
        entry = {"kind": l["kind"], "source": l["source"], "title": l.get("title", ""),
                 "state": l.get("pr_state", ""), "url": l.get("url", "")}
        if l.get("sha"):
            entry["sha"] = l["sha"]
        ctx["links"].append(entry)
    return ctx


def build_triage_prompt(ticket_ctx: dict, paths: list[TriagePath]) -> str:
    lines = [
        "You are a Jira triage assistant. Classify the ticket below into exactly one path.",
        "",
        "Available paths (id — name — criteria):",
    ]
    enabled = [p for p in paths if p.enabled and p.valid]
    for p in enabled:
        lines.append(f"\n### {p.id} ({p.name})\n{p.instruct[:3000]}")
    lines.append("\n\nTICKET CONTEXT (JSON):")
    lines.append(json.dumps(ticket_ctx, ensure_ascii=False, default=str))
    lines.append(
        "\nRespond with ONLY a JSON object, no markdown, of the form: "
        '{"path_id": string, "confidence": number 0..1, "reason": string,'
        ' "need_my_input": bool, "candidate_paths": [{"path_id": string, "score": number}]}'
        " Use need_my_input=true when the ticket needs a human decision."
    )
    return "\n".join(lines)


def run_triage(
    settings,
    run_dir: Path,
    ticket_ctx: dict,
    paths: list[TriagePath],
) -> TriageResult:
    prompt = build_triage_prompt(ticket_ctx, paths)
    if settings.mock:
        return _mock_triage(ticket_ctx, paths)

    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        text = op.run_agent(prompt, agent="triage", cwd=run_dir, model=settings.model_triage)
        return _parse(text)
    except Exception as e:  # noqa: BLE001
        # The agent/pipeline is down (opencode missing, no provider auth, timeout…).
        # Route to the human instead of silently keyword-classifying live tickets.
        log.warning("triage agent unavailable; escalating to need-my-input: %s", e)
        return TriageResult(
            path_id="need-my-input",
            confidence=0.0,
            reason=(
                f"Triage agent is unavailable right now ({type(e).__name__}). "
                "Manual review required."
            ),
            need_my_input=True,
        )


def _parse(text: str) -> TriageResult:
    try:
        return TriageResult.model_validate_json(text)
    except (ValidationError, ValueError):
        pass
    try:
        return TriageResult.model_validate(json.loads(text))
    except Exception:  # noqa: BLE001
        raise ValueError(f"Triage output not valid JSON: {text[:300]}") from None


# ---- deterministic fallback (mock mode) ----

_BUG_WORDS = ("error", "exception", "crash", "traceback", "stack trace", "wrong", "false", "fails",
              "broken", "bug", "cannot", "doesn't work", "not working", "regression", "typeerror")
_FEATURE_WORDS = ("feature", "enhance", "export", "support", "ability to", "add ", "new ", "improve")
_DECISION_WORDS = ("decision", "we should", "drop support", "policy", "strategy", "deprecat")

_TYPE_BOOST = {"bug-fix": 1.0, "new-feature": 0.4}


def _mock_triage(ticket_ctx: dict, paths: list[TriagePath]) -> TriageResult:
    text = f"{ticket_ctx.get('summary','')} {ticket_ctx.get('description','')}".lower()
    desc = ticket_ctx.get("description", "")
    needs_info = 0.5 if len(desc) < 60 else 0.0
    needs_info += _score(text, ("no repro", "no steps", "missing", "not provided", "sometimes",
                                "unclear", "vague", "random"))
    cands = [
        ("bug-fix", _score(text, _BUG_WORDS)),
        ("new-feature", _score(text, _FEATURE_WORDS)),
        ("need-more-info", needs_info),
        ("need-my-input", _score(text, _DECISION_WORDS) + (1.0 if "?" in text else 0.0)),
    ]
    issue_type = str(ticket_ctx.get("issue_type") or "").lower()
    if any(s in issue_type for s in ("bug", "defect", "incident", "problem")):
        cands[0] = ("bug-fix", cands[0][1] + _TYPE_BOOST["bug-fix"])
    elif any(s in issue_type for s in ("story", "feature", "enhancement", "epic")):
        cands[1] = ("new-feature", cands[1][1] + _TYPE_BOOST["new-feature"])
    enabled = {p.id for p in paths if p.enabled and p.valid}
    avail = [(pid, s) for pid, s in cands if pid in enabled]
    if not avail:
        return TriageResult(path_id="need-my-input", confidence=1.0,
                            reason="No enabled path available; routed to human.")
    avail.sort(key=lambda x: -x[1])
    top, score = avail[0]
    reason = f"mock keyword triage; top score {score:.2f}"
    return TriageResult(
        path_id=top,
        confidence=min(1.0, 0.4 + score),
        reason=reason,
        need_my_input=(top == "need-my-input"),
        candidate_paths=[{"path_id": pid, "score": min(1.0, sc)} for pid, sc in avail[:3]],
    )


def _score(text: str, words: tuple[str, ...]) -> float:
    hits = sum(1 for w in words if w in text)
    return min(1.0, hits * 0.33)