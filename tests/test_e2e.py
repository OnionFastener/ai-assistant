"""Full mock E2E (formerly /tmp/opencode-asst/e2e.py + e2e_v2.py).

Pipeline: manual run → fetch → context → repo resolve → triage → plan → edit →
approve → deterministic execute → branch pushed to mock remote → PR recorded.
No network, no model, real local git remotes.
"""
from pathlib import Path

from assistant.db import SessionLocal
from assistant.integrations import gitutil
from assistant.models import ActionPlan
from conftest import wait_until


def _run_and_wait(authed):
    c = authed["client"]
    run_id = c.post("/api/runs", json={}, headers={"X-CSRF": authed["csrf"]}).json()["run_id"]
    assert wait_until(
        lambda: c.get("/api/runs").json()[0]["status"] in ("completed", "partial", "failed"))
    run = c.get("/api/runs").json()[0]
    assert run["id"] == run_id
    return run_id


def test_chat_flow_edit_approve_reject(authed):
    """The e2e.py flow: 5 approvals, edit bug-fix, approve edited, reject another."""
    c = authed["client"]
    run_id = _run_and_wait(authed)

    approvals = c.get("/api/approvals").json()
    assert len(approvals) == 5

    # diff endpoint works
    pid = approvals[0]["plan"]["id"]
    assert c.get(f"/api/approvals/{pid}/diff").status_code == 200

    # edit bug-fix: change comment body + disable last action
    bug = next(a for a in approvals if a["plan"]["path_id"] == "bug-fix")
    acts = bug["plan"]["actions"]
    acts[0]["params"]["body"] = acts[0]["params"]["body"] + "\n\n(edited by user)"
    acts[-1]["enabled"] = False
    dbg = c.get(f"/api/approvals/{bug['plan']['id']}").json()["plan"]
    r = c.put(f"/api/approvals/{bug['plan']['id']}",
              json={"summary": dbg["summary"], "narrative": dbg["narrative"], "actions": acts},
              headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200
    edited = r.json()["plan"]["actions"]
    assert edited[0]["params"]["body"].endswith("(edited by user)")
    assert edited[-1]["enabled"] is False

    # approve edited plan + reject another
    assert c.post(f"/api/approvals/{bug['plan']['id']}/approve",
                  headers={"X-CSRF": authed["csrf"]}).status_code == 200
    other = approvals[1]["plan"]["id"]
    assert c.post(f"/api/approvals/{other}/reject", headers={"X-CSRF": authed["csrf"]}).status_code == 200

    # csrf guard
    third = approvals[2]["plan"]["id"]
    assert c.post(f"/api/approvals/{third}/reject").status_code == 403

    # wait for execution
    assert wait_until(lambda: _status(bug["plan"]["id"]) in ("executed", "failed"))

    db = SessionLocal()
    plan = db.get(ActionPlan, bug["plan"]["id"])
    assert plan.review_status in ("executed", "failed")
    assert plan.actions[0].exec_status == "ok"
    assert plan.actions[-1].exec_status == "skipped"
    assert plan.error in ("", None)
    db.close()

    # config get/put + paths list + create
    cfg = c.get("/api/config").json()
    cfg["schedule"]["enabled"] = False
    assert c.put("/api/config", json=cfg, headers={"X-CSRF": authed["csrf"]}).status_code == 200

    paths = c.get("/api/paths").json()
    body = {k: paths[0][k] for k in ("id", "name", "enabled", "allowed_actions",
                                     "required_backend", "work", "approval",
                                     "default_actions", "instruct")}
    assert c.put(f"/api/paths/{body['id']}", json=body, headers={"X-CSRF": authed["csrf"]}).status_code == 200
    cr = c.post("/api/paths", json={"id": "e2e-route", "name": "E2E", "enabled": True,
                                    "allowed_actions": ["comment"]},
                headers={"X-CSRF": authed["csrf"]})
    assert cr.status_code == 200

    # run detail
    detail = c.get(f"/api/runs/{run_id}").json()
    keys = [t["key"] for t in detail["tickets"]]
    assert keys == ["DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4", "DEMO-5"]


def test_bugfix_sandbox_push_to_pr(authed, monkeypatch):
    """The e2e_v2 flow: bug-fix plan patch really reaches the mock remote + PR."""
    from assistant.config import settings

    monkeypatch.setattr(settings, "workspace", Path(settings.workspace))
    c = authed["client"]
    run_id = _run_and_wait(authed)

    approvals = c.get("/api/approvals").json()
    bug = next(a for a in approvals if a["plan"]["path_id"] == "bug-fix")
    acts = bug["plan"]["actions"]
    kinds = [a["kind"] for a in acts]
    assert kinds == ["comment", "push_branch", "create_pr", "transition"], kinds

    push = acts[1]
    patch = push["params"]["patch"]
    assert patch.startswith("diff --git")
    assert push["preview"].startswith("diff --git")

    # resolved repo corroboration
    detail = c.get(f"/api/runs/{run_id}").json()
    t1 = next(t for t in detail["tickets"] if t["key"] == "DEMO-1")
    assert t1["repo"] == "owner/demo-repo-a"

    # approve and wait for execution
    assert c.post(f"/api/approvals/{bug['plan']['id']}/approve",
                  headers={"X-CSRF": authed["csrf"]}).status_code == 200
    assert wait_until(lambda: _status(bug["plan"]["id"]) in ("executed", "failed"))

    db = SessionLocal()
    plan = db.get(ActionPlan, bug["plan"]["id"])
    assert plan.review_status == "executed", plan.error
    assert all(a.exec_status == "ok" for a in plan.actions)
    db.close()

    # branch really pushed to the mock remote
    remote = Path(settings.workspace) / f"run-{run_id}" / "_remote" / "demo-repo-a.git"
    refs = gitutil.run_git(remote.parent, "ls-remote", str(remote), "refs/heads/*")
    assert "refs/heads/fix/demo-1-1" in refs, refs

    # PR creation recorded in the executed action
    db = SessionLocal()
    plan = db.get(ActionPlan, bug["plan"]["id"])
    cr = next(a for a in plan.actions if a.kind == "create_pr")
    assert "#1" in cr.exec_result
    assert "demo-repo-a" in cr.exec_result
    db.close()


def _status(plan_id):
    db = SessionLocal()
    p = db.get(ActionPlan, plan_id)
    status = p.review_status if p else None
    db.close()
    return status