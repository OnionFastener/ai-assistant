"""Deterministic executor tests: comment/transition/assign/push_branch/create_pr.

push_branch/create_pr run against a real mock remote (per-repo bare repo in the
workspace) so the patch-apply + push + PR ordering is exercised end to end.
"""
import pytest

from assistant.config import settings
from assistant.db import SessionLocal
from assistant.executor import ExecContext, execute_plan, preview_action
from assistant.integrations import gitutil
from assistant.integrations.github import MockGitHubClient
from assistant.integrations.jira import MockJiraClient
from assistant.models import Action, ActionPlan, Run, Ticket


@pytest.fixture()
def plan_factory():
    sessions = []

    def build(ticket_key="DEMO-1", path_id="bug-fix", actions=None):
        db = SessionLocal()
        sessions.append(db)
        run = Run(trigger="manual", status="completed")
        db.add(run)
        db.commit()
        ticket = Ticket(run_id=run.id, key=ticket_key, summary="s", description="d", stage="awaiting_approval")
        db.add(ticket)
        db.commit()
        plan = ActionPlan(ticket_id=ticket.id, run_id=run.id, path_id=path_id,
                          summary="s", narrative="n", review_status="approved")
        db.add(plan)
        for i, a in enumerate(actions or []):
            plan.actions.append(Action(seq=i, kind=a[0], params=a[1], preview=preview_action(a[0], a[1])))
        db.commit()
        # Force-load relationships now so handlers work after the session detaches.
        db.refresh(plan)
        _ = plan.ticket
        _ = plan.actions
        return plan
    return build


@pytest.fixture()
def ctx(tmp_path):
    c = ExecContext(jira=MockJiraClient(), github=MockGitHubClient(repo="owner/demo-repo-a"),
                    settings=settings, workspace=tmp_path)
    return c


def _mock_remote(ctx, run_id, repo):
    return gitutil.setup_mock_repo(ctx.workspace / f"run-{run_id}" / "_remote", gitutil.remote_name(repo))


# ---- simple handlers ----


def test_comment_with_empty_body_fails(plan_factory, ctx):
    plan = plan_factory(actions=[("comment", {"body": "   "})])
    status, _ = execute_plan(ctx, plan, {"comment"})
    assert status == "failed"
    assert plan.actions[0].exec_status == "failed"
    assert "non-empty" in plan.actions[0].exec_result


def test_comment_adds_footer_to_mock_jira(plan_factory, ctx):
    plan = plan_factory(actions=[("comment", {"body": "Your fix is ready"})])
    status, _ = execute_plan(ctx, plan, {"comment"})
    assert status == "executed"
    assert plan.actions[0].exec_status == "ok"
    assert ctx.jira.comments[0][0] == "DEMO-1"
    assert ctx.jira.comments[0][1].startswith("Your fix is ready")
    assert "_Posted by AI assistant" in ctx.jira.comments[0][1]


def test_transition_requires_to(plan_factory, ctx):
    plan = plan_factory(actions=[("transition", {"to": ""})])
    execute_plan(ctx, plan, {"transition"})
    assert plan.actions[0].exec_status == "failed"


def test_transition_resolves_available_status(plan_factory, ctx):
    plan = plan_factory(actions=[("transition", {"to": "In Review"})])
    execute_plan(ctx, plan, {"transition"})
    assert plan.actions[0].exec_status == "ok"
    assert ("DEMO-1", "In Review") in ctx.jira.transitions


def test_assign_defaults_me_to_account(plan_factory, ctx):
    plan = plan_factory(actions=[("assign", {"assignee": "me"})])
    execute_plan(ctx, plan, {"assign"})
    assert plan.actions[0].exec_status == "ok"
    assert ("DEMO-1", ctx.jira.account_id) in ctx.jira.assignees


def test_unknown_kind_is_not_implemented(plan_factory, ctx):
    plan = plan_factory(actions=[("comment", {"body": "ok"}), ("defragment", {})])
    execute_plan(ctx, plan, {"comment", "defragment"})
    assert plan.actions[1].exec_status == "failed"
    assert "no handler" in plan.actions[1].exec_result


# ---- allowlist handling ----


def test_action_not_in_allowed_set_fails(plan_factory, ctx):
    plan = plan_factory(actions=[("assign", {"assignee": "x"})])
    status, _ = execute_plan(ctx, plan, {"comment"})
    assert status == "failed"
    assert plan.actions[0].exec_status == "failed"
    assert "not allowed" in plan.actions[0].exec_result


def test_disabled_action_is_skipped(plan_factory, ctx):
    plan = plan_factory(actions=[("comment", {"body": "x"}), ("comment", {"body": "y"})])
    plan.actions[1].enabled = False
    execute_plan(ctx, plan, {"comment"})
    assert plan.actions[0].exec_status == "ok"
    assert plan.actions[1].exec_status == "skipped"


def test_critical_failure_stops_pipeline(plan_factory, ctx):
    plan = plan_factory(actions=[
        ("comment", {"body": "first"}),
        ("push_branch", {"branch_name": "fix/x", "patch": "not-a-patch", "commit_msg": "m"}),
        ("comment", {"body": "third"}),
    ])
    _mock_remote(ctx, plan.run_id, "owner/demo-repo-a")
    status, _ = execute_plan(ctx, plan, {"comment", "push_branch"})
    assert status == "failed"
    assert plan.actions[1].exec_status == "failed"   # patch apply fails → critical
    assert plan.actions[2].exec_status == "pending"  # pipeline stopped


# ---- push + PR (mock remote) ----


def _mock_remote_for(ctx, run_id, repo):
    return _mock_remote(ctx, run_id, repo)


def _patch_for_run(ctx, run_id, repo):
    """Set up a mock remote at run-{run_id} and return a real diff against it."""
    remote = _mock_remote_for(ctx, run_id, repo)
    clone = ctx.workspace / f"patch-src-{run_id}"
    gitutil.clone_local(remote, clone)
    src = clone / "service.py"
    src.write_text(src.read_text().replace("shipping=0.0):", "shipping=0.0):\n    # guard\n"))
    return gitutil.stage_and_diff(clone)


def _push_plan_with_patch(ctx, plan_factory, repo, tamper=False):
    """Create the plan first, then generate the patch against its own run remote."""
    plan = plan_factory(actions=[
        ("push_branch", {"branch_name": "fix/demo-1", "commit_msg": "Fix DEMO-1",
                         "patch": "", "repo": repo}),
    ])
    patch = _patch_for_run(ctx, plan.run_id, repo)
    expected_sha = gitutil.patch_sha(patch)
    if tamper:
        patch = patch.replace("# guard", "# MALICIOUS")
    plan.actions[0].params["patch"] = patch
    plan.actions[0].params["patch_sha"] = expected_sha
    db = SessionLocal()
    db.commit()
    db.close()
    return plan


def test_push_branch_pushes_reviewed_patch(ctx, plan_factory):
    plan = _push_plan_with_patch(ctx, plan_factory, "owner/demo-repo-a")
    execute_plan(ctx, plan, {"push_branch"})
    assert plan.actions[0].exec_status == "ok", plan.actions[0].exec_result

    remote = ctx.workspace / f"run-{plan.run_id}" / "_remote" / "demo-repo-a.git"
    refs = gitutil.run_git(ctx.workspace, "ls-remote", str(remote), "refs/heads/*")
    assert "refs/heads/fix/demo-1" in refs
    assert ("owner/demo-repo-a", "DEMO-1", "fix/demo-1") in ctx.github.pushed


def test_tampered_patch_hash_rejected_and_critical(ctx, plan_factory):
    plan = _push_plan_with_patch(ctx, plan_factory, "owner/demo-repo-a", tamper=True)
    status, _ = execute_plan(ctx, plan, {"push_branch"})
    assert status == "failed"
    assert plan.actions[0].exec_status == "failed"
    assert "patch hash does not match" in plan.actions[0].exec_result


def test_create_pr_requires_pushed_branch(ctx, plan_factory):
    db = SessionLocal()
    plan = plan_factory(actions=[
        ("create_pr", {"head": "fix/never-pushed", "target_branch": "main",
                       "title": "PR", "body": "b", "repo": "owner/demo-repo-a"}),
    ])
    _mock_remote_for(ctx, plan.run_id, "owner/demo-repo-a")
    db.close()
    status, _ = execute_plan(ctx, plan, {"create_pr"})
    assert status == "failed"
    assert plan.actions[0].exec_status == "failed"
    assert "was not pushed" in plan.actions[0].exec_result


def test_push_then_pr_orders_correctly(ctx, plan_factory):
    plan = _push_plan_with_patch(ctx, plan_factory, "owner/demo-repo-a")
    plan.actions.append(Action(
        seq=1, kind="create_pr", enabled=True,
        params={"head": "fix/demo-1", "target_branch": "main", "title": "Fix DEMO-1",
                "body": "regression fix", "repo": "owner/demo-repo-a"},
        preview=preview_action("create_pr", {"title": "Fix DEMO-1", "head": "fix/demo-1",
                                             "target_branch": "main", "body": "regression fix"}),
    ))
    db = SessionLocal()
    db.commit()
    db.close()
    status, _ = execute_plan(ctx, plan, {"push_branch", "create_pr"})
    assert status == "executed", plan.actions[0].exec_result
    assert plan.actions[0].exec_status == "ok"
    assert plan.actions[1].exec_status == "ok"
    assert "PR #1" in plan.actions[1].exec_result


def test_preview_action_shapes():
    assert preview_action("comment", {"body": "hi"}) == "hi"
    assert preview_action("transition", {"to": "In Review"}) == "Change status to 'In Review'"
    assert preview_action("assign", {"assignee": "me"}) == "Assign to me"
    assert preview_action("push_branch", {"branch_name": "fix/x", "commit_msg": "m", "patch": "diff..."}) == \
        f"Push branch 'fix/x' (commit: m)\n\ndiff..."
    assert preview_action("create_pr", {"title": "T", "head": "h", "target_branch": "m", "body": "b"}) == \
        "PR: T (h → m)\n\nb"


def test_session_cleanup_outside_tx(plan_factory, ctx):
    plan = plan_factory(actions=[("comment", {"body": "x"})])
    execute_plan(ctx, plan, {"comment"})
    # double-execute must keep working (exec_result overwritten)
    execute_plan(ctx, plan, {"comment"})
    assert plan.actions[0].exec_result.endswith("comment added")