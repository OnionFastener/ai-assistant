"""API + auth + CSRF coverage via FastAPI TestClient (in-process, no server)."""
from conftest import wait_until


# ---- auth ----

def test_login_wrong_password(client):
    r = client.post("/api/login", json={"password": "nope"})
    assert r.status_code == 401


def test_login_sets_session_cookie_and_csrf(client):
    r = client.post("/api/login", json={"password": "mock-assistant"})
    assert r.status_code == 200
    assert "asst_session" in r.cookies
    assert r.json()["csrf"]


def test_health_is_public_and_mock(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["mock"] is True


def test_unauthenticated_api_returns_401(client):
    assert client.get("/api/runs").status_code == 401
    assert client.get("/api/approvals").status_code == 401
    assert client.get("/api/config").status_code == 401


def test_mutation_without_csrf_is_403(authed):
    c = authed["client"]
    r = c.post("/api/runs", json={})
    assert r.status_code == 403


def test_mutation_with_csrf_succeeds(authed):
    c = authed["client"]
    r = c.post("/api/paths", json={"id": "csrf-ok", "name": "CSRF", "enabled": True,
                                   "allowed_actions": ["comment"]},
               headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200
    assert any(p["id"] == "csrf-ok" for p in r.json())


def test_logout_invalidates_session(authed):
    c = authed["client"]
    r = c.post("/api/logout", headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200
    assert c.get("/api/runs").status_code == 401


def test_session_endpoint(client):
    assert client.get("/api/session").json() == {"authed": False}


# ---- runs + approvals happy path (mock pipeline) ----


def test_stop_active_run_preserves_existing_approvals(authed):
    from assistant.db import SessionLocal
    from assistant.models import ActionPlan, Run, Ticket

    db = SessionLocal()
    run = Run(status="triaging")
    db.add(run)
    db.commit()
    ticket = Ticket(run_id=run.id, key="STOP-1", summary="Finished ticket", stage="awaiting_approval")
    db.add(ticket)
    db.commit()
    db.add(ActionPlan(ticket_id=ticket.id, run_id=run.id, summary="Ready plan", review_status="pending"))
    db.commit()
    db.close()

    response = authed["client"].post(f"/api/runs/{run.id}/stop", headers={"X-CSRF": authed["csrf"]})
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    assert response.json()["pending_plans"] == 1
def _start_and_wait(authed, jql=None, expected=5):
    c = authed["client"]
    body = {"jql": jql} if jql else {}
    run_id = c.post("/api/runs", json=body, headers={"X-CSRF": authed["csrf"]}).json()["run_id"]
    assert wait_until(lambda: c.get("/api/runs").json()[0]["status"] in ("completed", "partial", "failed"))
    return run_id


def test_manual_run_mock_completes_with_approvals(authed):
    c = authed["client"]
    run_id = _start_and_wait(authed)
    run = next(r for r in c.get("/api/runs").json() if r["id"] == run_id)
    assert run["status"] in ("completed", "partial")
    assert run["pending_plans"] == 5
    approvals = c.get("/api/approvals").json()
    assert len(approvals) == 5
    assert any(a["plan"]["path_id"] == "bug-fix" for a in approvals)
    assert any(a["plan"]["path_id"] == "new-feature" for a in approvals)
    assert any(a["plan"]["path_id"] == "need-my-input" for a in approvals)


def test_plan_diff_endpoint_returns_files(authed):
    c = authed["client"]
    _start_and_wait(authed)
    approvals = c.get("/api/approvals").json()
    pid = approvals[0]["plan"]["id"]
    r = c.get(f"/api/approvals/{pid}/diff")
    assert r.status_code == 200
    assert "files" in r.json()


def test_edit_plan_keeps_patch_in_tact(authed):
    """Editing a bug-fix plan (comment body / PR title) must not drop the patch."""
    c = authed["client"]
    _start_and_wait(authed)
    bug = next(a for a in c.get("/api/approvals").json() if a["plan"]["path_id"] == "bug-fix")
    acts = bug["plan"]["actions"]
    patch = next(a for a in acts if a["kind"] == "push_branch")["params"]["patch"]
    assert patch.startswith("diff --git")

    acts[0]["params"]["body"] = "Edited by user."
    dbg = c.get(f"/api/approvals/{bug['plan']['id']}").json()["plan"]
    body = {"summary": dbg["summary"], "narrative": dbg["narrative"], "actions": acts}
    r = c.put(f"/api/approvals/{bug['plan']['id']}", json=body, headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200, r.text
    saved = [a for a in r.json()["plan"]["actions"] if a["kind"] == "push_branch"][0]
    assert saved["params"]["patch"] == patch
    assert saved["params"]["patch_sha"] == saved["params"].get("patch_sha")


def test_edit_rejects_disallowed_action(authed):
    c = authed["client"]
    _start_and_wait(authed)
    bug = next(a for a in c.get("/api/approvals").json() if a["plan"]["path_id"] == "bug-fix")
    dbg = c.get(f"/api/approvals/{bug['plan']['id']}").json()["plan"]
    acts = dbg["actions"] + [{"kind": "edit_ticket", "params": {"field": "labels"}, "enabled": True,
                              "preview": "", "seq": len(dbg["actions"])}]
    body = {"summary": dbg["summary"], "narrative": dbg["narrative"], "actions": acts}
    r = c.put(f"/api/approvals/{bug['plan']['id']}", json=body, headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 422


def test_approve_runs_and_reject_is_guard(authed):
    """Approve one plan (executes), then double-approve must 409."""
    from assistant.db import SessionLocal
    from assistant.models import ActionPlan

    c = authed["client"]
    _start_and_wait(authed)
    approvals = c.get("/api/approvals").json()
    target = next(a for a in approvals if a["plan"]["path_id"] == "bug-fix")["plan"]

    r = c.post(f"/api/approvals/{target['id']}/approve", headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200
    r2 = c.post(f"/api/approvals/{target['id']}/approve", headers={"X-CSRF": authed["csrf"]})
    assert r2.status_code == 409

    assert wait_until(lambda: _plan_status(target["id"]) in ("executed", "failed"))
    db = SessionLocal()
    p = db.get(ActionPlan, target["id"])
    assert p.review_status in ("executed", "failed")
    assert all(a.exec_status == "ok" for a in p.actions), [(a.kind, a.exec_status, a.exec_result) for a in p.actions]
    db.close()


def _plan_status(plan_id):
    from assistant.db import SessionLocal
    from assistant.models import ActionPlan

    db = SessionLocal()
    p = db.get(ActionPlan, plan_id)
    status = p.review_status if p else None
    db.close()
    return status


# ---- config / paths / repo-map ----

def test_config_roundtrip(authed):
    c = authed["client"]
    cfg = c.get("/api/config").json()
    assert isinstance(cfg["jql_queries"], list)
    assert cfg["mock"] is True
    assert "token_set" in cfg["github"]
    cfg["schedule"]["enabled"] = False
    r = c.put("/api/config", json=cfg, headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200
    assert r.json()["schedule"]["enabled"] is False


def test_paths_list_and_create_kebab_validation(authed):
    c = authed["client"]
    paths = c.get("/api/paths").json()
    ids = [p["id"] for p in paths]
    assert "bug-fix" in ids and "need-my-input" in ids

    bad = c.post("/api/paths", json={"id": "Bad_Path", "name": "n", "enabled": True,
                                     "allowed_actions": ["comment"]},
                 headers={"X-CSRF": authed["csrf"]})
    assert bad.status_code == 422

    ok = c.post("/api/paths", json={"id": "test-route", "name": "Test", "enabled": True,
                                    "allowed_actions": ["comment"]},
                headers={"X-CSRF": authed["csrf"]})
    assert ok.status_code == 200
    assert any(p["id"] == "test-route" for p in ok.json())


def test_repo_map_roundtrip(authed):
    c = authed["client"]
    initial = c.get("/api/repo-map").json()
    body = dict(initial)
    body["DEMO-1"] = "owner/demo-repo-a"
    r = c.put("/api/repo-map", json=body, headers={"X-CSRF": authed["csrf"]})
    assert r.status_code == 200
    assert c.get("/api/repo-map").json()["DEMO-1"] == "owner/demo-repo-a"


def test_run_detail_and_tickets(authed):
    c = authed["client"]
    run_id = _start_and_wait(authed)
    detail = c.get(f"/api/runs/{run_id}").json()
    assert detail["status"] in ("completed", "partial")
    keys = [t["key"] for t in detail["tickets"]]
    assert "DEMO-1" in keys
    t1 = next(t for t in detail["tickets"] if t["key"] == "DEMO-1")
    assert t1["repo"] == "owner/demo-repo-a"
    assert t1["plans"] and t1["plans"][0]["review_status"] == "pending"
