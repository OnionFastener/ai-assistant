"""Action agent: JSON plan parsing, normalization defaults, mock fix end-to-end."""
import pytest

from assistant.action_agent import (
    _ensure_branch_name,
    _mock_fix,
    _normalize_plan,
    _parse_plan_json,
    run_for_ticket,
)
from assistant.config import settings
from assistant.integrations import gitutil
from assistant.schemas import ActionPlanInput


def test_parse_plan_json_plain():
    assert _parse_plan_json('{"summary": "s"}') == {"summary": "s"}


def test_parse_plan_json_strips_fences():
    assert _parse_plan_json('```json\n{"summary": "s"}\n```') == {"summary": "s"}


def test_parse_plan_json_extracts_from_prose():
    assert _parse_plan_json('Here: {"summary": "s"} ok') == {"summary": "s"}


def test_parse_plan_json_extracts_action_plan_from_long_prose():
    """Regression: model narrated (the AA-2 failure) but embedded a JSON plan at the end."""
    prose = (
        "Chrome is available. Let me empirically verify how `zoom: 1.25` behaves "
        "for a full-width card so I pick a correct implementation. "
        'Final plan: ```json\n{"summary": "Fix zoom", "narrative": "why", '
        '"actions": [{"kind": "comment", "params": {"body": "hi"}, "preview": ""}]}\n```'
    )
    out = _parse_plan_json(prose)
    assert out and "actions" in out
    assert out["summary"] == "Fix zoom"


def test_parse_plan_json_invalid_returns_none():
    assert _parse_plan_json("not json") is None
    assert _parse_plan_json("") is None


def test_ensure_branch_name_uses_existing():
    plan = ActionPlanInput(summary="s", actions=[{"kind": "push_branch", "params": {"branch_name": "fix/custom"}}])
    assert _ensure_branch_name(plan, 7, "DEMO-1") == "fix/custom"


def test_ensure_branch_name_defaults():
    plan = ActionPlanInput(summary="s", actions=[])
    assert _ensure_branch_name(plan, 7, "DEMO-1") == "fix/demo-1-7"


def test_normalize_fills_push_branch_defaults(tmp_path):
    fixed = _fixed_repo(tmp_path)
    patch = gitutil.stage_and_diff(fixed)

    plan_in = ActionPlanInput(summary="Fix DEMO-1", actions=[
        {"kind": "push_branch", "params": {"branch_name": "fix/demo-1"}},
        {"kind": "create_pr", "params": {"title": "Fix DEMO-1"}},
    ])
    out = _normalize_plan(plan_in.model_dump(), 1, "DEMO-1", fixed, github_repo="owner/demo-repo-a")

    pb = next(a for a in out.actions if a.kind == "push_branch")
    assert pb.params["patch"] == patch
    assert pb.params["patch_sha"] == gitutil.patch_sha(patch)
    assert pb.params["base"] == "main"
    assert pb.params["repo"] == "owner/demo-repo-a"
    assert pb.preview.startswith("diff --git")

    pr = next(a for a in out.actions if a.kind == "create_pr")
    assert pr.params["head"] == "fix/demo-1"
    assert pr.params["target_branch"] == "main"
    assert pr.params["repo"] == "owner/demo-repo-a"


def test_normalize_keeps_existing_pr_fields(tmp_path):
    fixed = _fixed_repo(tmp_path)
    plan_in = ActionPlanInput(summary="S", actions=[
        {"kind": "push_branch", "params": {"branch_name": "fix/demo-1",
                                           "commit_msg": "custom msg"}},
        {"kind": "create_pr", "params": {"head": "fix/demo-1", "target_branch": "old-main",
                                         "title": "My title", "body": "My body"}},
    ])
    out = _normalize_plan(plan_in.model_dump(), 5, "DEMO-1", fixed, github_repo="a/b")
    pr = next(a for a in out.actions if a.kind == "create_pr")
    assert pr.params["target_branch"] == "old-main"
    assert pr.params["title"] == "My title"


def test_normalize_rejects_invalid_plan(tmp_path):
    plan_in = {"summary": "s", "actions": [{"kind": "push_branch", "params": "not-a-dict"}]}
    with pytest.raises(ValueError, match="invalid action plan"):
        _normalize_plan(plan_in, 1, "DEMO-1", tmp_path)


def test_run_for_ticket_mock_produces_full_plan(tmp_path, monkeypatch):
    """Mock path: sandbox → mock fix → diff-captured plan, no model, no network."""
    monkeypatch.setattr(settings, "workspace", tmp_path)
    from assistant.paths import load_paths

    path = next(p for p in load_paths(settings.paths_dir) if p.id == "bug-fix")
    repo = "owner/demo-repo-a"
    plan = run_for_ticket(42, "DEMO-1",
                          {"key": "DEMO-1", "summary": "crash", "description": "TypeError in total()"},
                          path, repo=repo)
    kinds = [a.kind for a in plan.actions]
    assert kinds == ["comment", "push_branch", "create_pr", "transition"]
    pb = next(a for a in plan.actions if a.kind == "push_branch")
    assert pb.params["branch_name"] == "fix/demo-1-42"
    assert pb.params["patch"].startswith("diff --git")
    assert pb.params["patch_sha"] == gitutil.patch_sha(pb.params["patch"])


def test_mock_fix_shape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.py").write_text("def total(subtotal, shipping=0.0):\n    return subtotal + shipping\n")
    out = _mock_fix(3, "DEMO-1", {"key": "DEMO-1"}, repo, repo)
    assert out["summary"].startswith("Fix DEMO-1")
    assert [a["kind"] for a in out["actions"]] == ["comment", "push_branch", "create_pr", "transition"]


def _fixed_repo(tmp_path):
    """A clone with a pending change eligible for stage_and_diff."""
    remote = gitutil.setup_mock_repo(tmp_path / "_remote", gitutil.remote_name("owner/demo-repo-a"))
    clone = tmp_path / "repo"
    gitutil.clone_local(remote, clone)
    src = clone / "service.py"
    src.write_text(src.read_text().replace("shipping=0.0):", "shipping=0.0):\n    # guard\n"))
    return clone